#!/usr/bin/env python3
"""RFY -> BUSY Bar receiver.

A hardened webhook endpoint for the V4 "Webhook V1" contract.
  * Standard Webhooks HMAC-SHA256 verification over the raw request body
  * timestamp freshness check
  * 8 KiB body limit, strict routing, generic error responses
  * Phase 2: rfy.discovered items are normalised (ASCII-safe title, "$NN.NN"
    price) and de-duplicated by eventId in a persistent SQLite store
  * Phase 3: accepted items go into a persistent FIFO queue (same SQLite file,
    same transaction as the dedupe record); a single worker shows them on the
    BUSY Bar one at a time. webhook.test also shows a short "test OK" message
    (never de-duplicated, so you can re-run tests).

Python 3.8+, standard library only. Binds to 127.0.0.1 by default.

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bar import BarClient, Result
from icon import IconManager
from normalize import normalize_item
from store import Store
from worker import DisplaySettings, DisplayWorker

LOG = logging.getLogger("rfy")

MAX_BODY_BYTES = 8 * 1024  # V4 documents an 8 KiB body limit
SUPPORTED_SCHEMA = 1


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def load_env_file(path: Path) -> dict:
    """Parse a simple KEY=VALUE file. Missing file -> empty dict."""
    values: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def parse_secret(raw: str) -> bytes:
    raw = raw.strip()
    if not raw:
        raise ConfigError("RFY_WEBHOOK_SECRET is not set")
    if not raw.startswith("whsec_"):
        raise ConfigError("RFY_WEBHOOK_SECRET must start with 'whsec_'")
    try:
        key = base64.b64decode(raw[len("whsec_"):], validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError("RFY_WEBHOOK_SECRET is not valid Base64 after 'whsec_'")
    if not key:
        raise ConfigError("RFY_WEBHOOK_SECRET decodes to an empty key")
    return key


def _int(env: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be a whole number")
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo} and {hi}")
    return value


def _float(env: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(env.get(key, str(default)))
    except ValueError:
        raise ConfigError(f"{key} must be a number")
    if not lo <= value <= hi:
        raise ConfigError(f"{key} must be between {lo:g} and {hi:g}")
    return value


def _color(env: dict, key: str, default: str) -> str:
    value = env.get(key, default).strip()
    if not re.fullmatch(r"#[a-fA-F0-9]{8}", value):
        raise ConfigError(f"{key} must look like #RRGGBBAA")
    return value


FONTS = ("tiny", "small", "normal", "condensed", "bold", "large", "extra_large", "global")


def _bool(env: dict, key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true or false")


class Config:
    def __init__(self, env: dict):
        try:
            self.host = env.get("RFY_HOST", "127.0.0.1")
            self.port = int(env.get("RFY_PORT", "8787"))
            self.path = env.get("RFY_PATH", "/webhook")
            self.tolerance = int(env.get("RFY_TIMESTAMP_TOLERANCE_SECONDS", "300"))
        except ValueError as exc:
            raise ConfigError(f"invalid numeric setting: {exc}")
        if not self.path.startswith("/"):
            raise ConfigError("RFY_PATH must start with '/'")
        self.log_level = env.get("RFY_LOG_LEVEL", "INFO").upper()
        try:
            self.dedupe_ttl_hours = int(env.get("RFY_DEDUPE_TTL_HOURS", "24"))
            self.dedupe_max_rows = int(env.get("RFY_DEDUPE_MAX_ROWS", "10000"))
        except ValueError as exc:
            raise ConfigError(f"invalid numeric setting: {exc}")
        if self.dedupe_ttl_hours < 1:
            # V4 retries for up to 15 minutes; the memory must outlast that window
            raise ConfigError("RFY_DEDUPE_TTL_HOURS must be at least 1")
        if self.dedupe_max_rows < 1000:
            raise ConfigError("RFY_DEDUPE_MAX_ROWS must be at least 1000")
        self.db_path = env.get("RFY_DB_PATH", "rfy_bar.db")
        self.secret = parse_secret(env.get("RFY_WEBHOOK_SECRET", ""))

        # ---- BUSY Bar connection
        self.bar_url = env.get("RFY_BAR_URL", "http://192.168.0.85").strip()
        self.bar_token = env.get("RFY_BAR_TOKEN", "").strip()
        if not self.bar_token:
            raise ConfigError("RFY_BAR_TOKEN is not set (create a named access token on the bar)")
        self.bar_timeout = _float(env, "RFY_BAR_TIMEOUT_SECONDS", 5, 1, 30)

        # ---- queue policy
        self.queue_max = _int(env, "RFY_QUEUE_MAX", 25, 1, 100)
        self.queue_expiry_seconds = _int(env, "RFY_QUEUE_EXPIRY_SECONDS", 1800, 60, 86400)

        # ---- display policy
        app = env.get("RFY_APP_NAME", "rfy_feed")
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,32}", app):
            raise ConfigError("RFY_APP_NAME must be 1-32 characters of letters, digits . _ -")
        dwell = _int(env, "RFY_DWELL_SECONDS", 15, 1, 600)
        backlog_dwell = _int(env, "RFY_BACKLOG_DWELL_SECONDS", 8, 1, 600)
        max_dwell = _int(env, "RFY_MAX_DWELL_SECONDS", 40, 1, 600)
        if not backlog_dwell <= dwell <= max_dwell:
            raise ConfigError("need RFY_BACKLOG_DWELL_SECONDS <= RFY_DWELL_SECONDS "
                              "<= RFY_MAX_DWELL_SECONDS")
        led = None
        if _bool(env, "RFY_LED_ENABLED", True):
            led = _color(env, "RFY_LED_COLOR", "#FF8800FF")
        taxfree_font = env.get("RFY_TAXFREE_FONT", "small").strip()
        if taxfree_font not in FONTS:
            raise ConfigError(f"RFY_TAXFREE_FONT must be one of: {', '.join(FONTS)}")
        self.gold_min_cents = _int(env, "RFY_GOLD_MIN_DOLLARS", 100, 1, 10_000_000) * 100
        self.icon_file = env.get("RFY_ICON_FILE", "v4_icon.png").strip()  # "" = no icon
        self.icon_path = None  # resolved against the env file's folder in load_config
        sound = _bool(env, "RFY_SOUND_ENABLED", False)
        stock = env.get("RFY_SOUND_STOCK_PATH", "").strip() or None
        asset = env.get("RFY_SOUND_ASSET_PATH", "").strip() or None
        if sound:
            if bool(stock) == bool(asset):
                raise ConfigError("with RFY_SOUND_ENABLED=true set exactly one of "
                                  "RFY_SOUND_STOCK_PATH or RFY_SOUND_ASSET_PATH")
            if stock and not re.fullmatch(r"[a-zA-Z0-9._/-]{1,256}", stock):
                raise ConfigError("RFY_SOUND_STOCK_PATH has invalid characters")
            if asset and not re.fullmatch(r"[a-zA-Z0-9._/-]{1,64}", asset):
                raise ConfigError("RFY_SOUND_ASSET_PATH has invalid characters")
        self.display = DisplaySettings(
            app_name=app,
            priority=_int(env, "RFY_PRIORITY", 50, 1, 100),
            dwell=dwell, backlog_dwell=backlog_dwell, max_dwell=max_dwell,
            backlog_threshold=_int(env, "RFY_BACKLOG_THRESHOLD", 5, 0, 100),
            gap=_float(env, "RFY_GAP_SECONDS", 1, 0, 60),
            char_px=_float(env, "RFY_CHAR_PX", 6, 1, 20),
            scroll_rate=_int(env, "RFY_SCROLL_RATE", 1000, 0, 100000),
            busy_retry=_float(env, "RFY_BUSY_RETRY_SECONDS", 10, 0.1, 600),
            led_color=led, sound_enabled=sound,
            sound_stock_path=stock, sound_asset_path=asset,
            color_normal=_color(env, "RFY_COLOR_NORMAL", "#66CCFFFF"),
            color_gold=_color(env, "RFY_COLOR_GOLD", "#FF8800FF"),
            color_taxfree=_color(env, "RFY_COLOR_TAXFREE", "#00FF00FF"),
            color_unknown=_color(env, "RFY_COLOR_UNKNOWN", "#FF0000FF"),
            taxfree_font=taxfree_font)


def load_config(here: Path) -> Config:
    env_path = Path(os.environ.get("RFY_ENV_FILE") or (here / "rfy_bar.env"))
    file_env = load_env_file(env_path)
    if file_env and os.name == "posix":
        try:
            if env_path.stat().st_mode & 0o077:
                print(f"WARNING: {env_path.name} is readable by other users; "
                      f"run: chmod 600 {env_path.name}", file=sys.stderr)
        except OSError:
            pass
    env = dict(os.environ)
    for key, val in file_env.items():
        env.setdefault(key, val)  # real environment variables win
    cfg = Config(env)
    if not Path(cfg.db_path).is_absolute():
        cfg.db_path = str(here / cfg.db_path)
    if cfg.icon_file:   # relative names live next to rfy_bar.env, so swapping is easy
        icon = Path(cfg.icon_file)
        cfg.icon_path = str(icon if icon.is_absolute() else env_path.resolve().parent / icon)
    return cfg


# --------------------------------------------------------------------------
# Signature verification (Standard Webhooks, v1 = HMAC-SHA256)
# --------------------------------------------------------------------------
def compute_signature(key: bytes, msg_id: str, timestamp: str, body: bytes) -> str:
    """Return 'v1,<base64>' for the given message parts."""
    signed = msg_id.encode("ascii") + b"." + timestamp.encode("ascii") + b"." + body
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify_signature(key: bytes, msg_id: str, timestamp: str, body: bytes,
                     sig_header: str) -> bool:
    """True if any 'v1,<sig>' entry in the header matches. Constant-time compare."""
    try:
        expected = compute_signature(key, msg_id, timestamp, body)
    except UnicodeEncodeError:
        return False
    expected_raw = base64.b64decode(expected.split(",", 1)[1])
    matched = False
    for part in sig_header.split():
        version, _, b64 = part.partition(",")
        if version != "v1":
            continue
        try:
            candidate = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        if hmac.compare_digest(candidate, expected_raw):
            matched = True  # keep looping: no early exit on match
    return matched


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def safe(value, limit: int = 80) -> str:
    """Printable-ASCII, length-limited rendering for log lines (no log injection)."""
    text = str(value)
    cleaned = "".join(c if 32 <= ord(c) < 127 else "?" for c in text)
    return cleaned[:limit]


# --------------------------------------------------------------------------
# Event handlers (later phases plug into these)
# --------------------------------------------------------------------------
def handle_test(event: dict, server) -> None:
    LOG.info("webhook.test received (eventId=%s): endpoint, signing and routing OK",
             safe(event.get("eventId")))
    dropped = server.store.enqueue_test(event["eventId"], "V4 webhook test OK", "OK")
    if dropped:
        LOG.warning("queue full: dropped %d oldest item(s)", dropped)
    server.worker.notify()


def handle_discovered(event: dict, server) -> None:
    data = event.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        # Authenticated but structurally wrong; retrying cannot fix it, so ack + log.
        LOG.warning("ignored: rfy.discovered without data.item (eventId=%s)",
                    safe(event.get("eventId")))
        return
    event_id = event["eventId"]
    display = normalize_item(event_id, item, server.cfg.gold_min_cents)  # pure; computed first
    is_new, dropped = server.store.accept_discovery(event_id, display.title, display.price,
                                                    price_kind=display.price_kind)
    if not is_new:
        LOG.info("duplicate ignored (eventId=%s)", safe(event_id))
        return
    LOG.info("queued rfy.discovered (eventId=%s) title=%r price=%r",
             safe(event_id), display.title, display.price)
    if dropped:
        LOG.warning("queue full: dropped %d oldest item(s) to make room", dropped)
    server.worker.notify()


HANDLERS = {
    "webhook.test": handle_test,
    "rfy.discovered": handle_discovered,
}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "rfy-receiver"
    sys_version = ""
    timeout = 5  # seconds; V4's own deadline is 10 s

    def log_message(self, fmt, *args):  # route http.server chatter to DEBUG
        LOG.debug("http: " + fmt, *[safe(a, 120) for a in args])

    def _reply(self, status: int, code: str) -> None:
        payload = json.dumps({"status" if status < 400 else "error": code}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _method_not_allowed(self):
        self._reply(405, "method_not_allowed")

    do_GET = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

    def do_POST(self):
        cfg: Config = self.server.cfg
        try:
            self._handle_post(cfg)
        except Exception:  # never leak details; V4 will retry on 5xx
            LOG.exception("unhandled error while processing webhook")
            try:
                self._reply(500, "internal_error")
            except Exception:
                pass

    def _handle_post(self, cfg: Config) -> None:
        if self.path.split("?", 1)[0] != cfg.path:
            return self._reply(404, "not_found")

        length_raw = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,9}", length_raw):
            return self._reply(411, "length_required")
        length = int(length_raw)
        if length > MAX_BODY_BYTES:
            LOG.warning("rejected: body of %d bytes exceeds limit", length)
            return self._reply(413, "payload_too_large")
        body = self.rfile.read(length)
        if len(body) != length:
            return self._reply(400, "incomplete_body")

        msg_id = self.headers.get("webhook-id", "")
        timestamp = self.headers.get("webhook-timestamp", "")
        sig_header = self.headers.get("webhook-signature", "")
        if not (msg_id and timestamp and sig_header) or len(msg_id) > 128 \
                or not re.fullmatch(r"[0-9]{1,12}", timestamp):
            LOG.warning("rejected: missing or malformed webhook headers")
            return self._reply(401, "unauthorized")

        if not verify_signature(cfg.secret, msg_id, timestamp, body, sig_header):
            LOG.warning("rejected: signature mismatch (webhook-id=%s)", safe(msg_id))
            return self._reply(401, "unauthorized")

        skew = abs(time.time() - int(timestamp))
        if skew > cfg.tolerance:
            LOG.warning("rejected: timestamp outside tolerance (skew=%ds, webhook-id=%s)",
                        int(skew), safe(msg_id))
            return self._reply(401, "unauthorized")

        # ---- authenticated from here on ----
        try:
            event = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(400, "invalid_json")
        if not isinstance(event, dict):
            return self._reply(400, "invalid_json")

        if event.get("eventId") != msg_id:
            LOG.warning("rejected: eventId does not match webhook-id header")
            return self._reply(400, "id_mismatch")

        if event.get("schemaVersion") != SUPPORTED_SCHEMA:
            LOG.warning("ignored: unsupported schemaVersion %s (eventId=%s)",
                        safe(event.get("schemaVersion")), safe(msg_id))
            return self._reply(200, "ignored")

        handler = HANDLERS.get(event.get("event"))
        if handler is None:
            LOG.info("ignored: unrecognised event type %r (eventId=%s)",
                     safe(event.get("event")), safe(msg_id))
            return self._reply(200, "ignored")

        handler(event, self.server)
        return self._reply(200, "ok")


def main() -> int:
    here = Path(__file__).resolve().parent
    try:
        cfg = load_config(here)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(cfg.secret) != 32:
        LOG.warning("signing key is %d bytes; V4 documents a 32-byte key", len(cfg.secret))
    if cfg.host not in ("127.0.0.1", "::1", "localhost"):
        LOG.warning("binding to %s (not loopback): restrict this port with a firewall", cfg.host)

    try:  # validate bar settings BEFORE creating any files
        bar = BarClient(cfg.bar_url, cfg.bar_token, timeout=cfg.bar_timeout)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    store = Store(cfg.db_path, ttl_seconds=cfg.dedupe_ttl_hours * 3600,
                  max_rows=cfg.dedupe_max_rows, queue_max=cfg.queue_max,
                  queue_expiry_seconds=cfg.queue_expiry_seconds)
    LOG.info("RFY -> BUSY Bar receiver | Designed by N8JRD - Jim Davis, Vibe Coded with Claude")
    LOG.info("store ready: %s (%d event ids remembered, %d item(s) waiting to display)",
             cfg.db_path, store.count(), store.queue_len())

    check = bar.version()
    if check.result is Result.OK:
        LOG.info("BUSY Bar reachable at %s", cfg.bar_url)
    elif check.result is Result.AUTH:
        LOG.error("BUSY Bar rejected the token (%s): fix RFY_BAR_TOKEN; items will wait "
                  "in the queue", check.detail)
    else:
        LOG.warning("BUSY Bar not reachable right now (%s): items will wait in the queue "
                    "and display when it returns", check.detail)
    icon = IconManager(cfg.icon_path, cfg.display.app_name) if cfg.icon_path else None
    LOG.info(icon.describe() if icon else "icon disabled (RFY_ICON_FILE is empty)")
    worker = DisplayWorker(store, bar, cfg.display, icon=icon)
    worker.start()

    server = ThreadingHTTPServer((cfg.host, cfg.port), Handler)
    server.daemon_threads = True
    server.cfg = cfg
    server.store = store
    server.worker = worker
    LOG.info("listening on http://%s:%d%s", cfg.host, cfg.port, cfg.path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.server_close()
        worker.stop()
        worker.join(timeout=5)
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
