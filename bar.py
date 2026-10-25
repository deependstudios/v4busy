"""BUSY Bar HTTP client (standard library only).

Only ever talks to the single configured base URL; nothing from a webhook
payload can influence where a request goes. The access token is sent in the
X-API-Token header and is never logged.

Result classification (what the worker needs to decide):
  OK        200: the item is now on screen
  BUSY      bar refused because a higher-priority app (e.g. a BUSY/CUSTOM work
            session) owns the screen. Status 409 per the spec; a body that says
            "low priority" is also treated as BUSY. Keep the item and retry.
  REJECTED  400: our request was malformed. Retrying cannot help; drop the item.
  AUTH      401/403: token wrong or revoked. Keep the item, back off, log loudly.
  ERROR     anything else (5xx, timeout, unreachable). Keep the item and retry.

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, urlsplit


class Result(Enum):
    OK = "ok"
    BUSY = "busy"
    REJECTED = "rejected"
    AUTH = "auth"
    ERROR = "error"


@dataclass(frozen=True)
class BarResponse:
    result: Result
    detail: str = ""


def _clean(text: str, limit: int = 160) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text)[:limit]


class BarClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username \
                or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError("bar URL must look like http://192.168.0.85 (no path, "
                             "credentials, query or fragment)")
        if not token or any(ord(c) < 0x21 or ord(c) > 0x7E for c in token):
            raise ValueError("bar token must be non-empty printable ASCII with no spaces")
        self._base = f"{parts.scheme}://{parts.netloc}"
        self._token = token
        self._timeout = timeout

    # ---------------------------------------------------------------- transport
    def _request(self, method: str, path: str, body: dict | None,
                 raw: bytes | None = None) -> BarResponse:
        data = json.dumps(body).encode("utf-8") if body is not None else raw
        headers = {"Accept": "application/json", "X-API-Token": self._token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self._base + path, data=data, method=method,
                                     headers=headers)
        try:
            # No redirects are followed to other hosts: urllib would, so block them.
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=self._timeout) as resp:
                resp.read(4096)
                return BarResponse(Result.OK if 200 <= resp.status < 300 else Result.ERROR,
                                   f"HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(2048).decode("utf-8", "replace")
            except Exception:
                text = ""
            return self._classify(exc.code, text)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return BarResponse(Result.ERROR, _clean(f"{type(exc).__name__}: {exc}"))

    @staticmethod
    def _classify(status: int, body_text: str) -> BarResponse:
        try:
            parsed = json.loads(body_text)
            message = parsed.get("error", "") if isinstance(parsed, dict) else ""
        except ValueError:
            message = ""
        detail = _clean(f"HTTP {status} {message or body_text}")
        if status == 409 or "low priority" in (message or body_text).lower():
            return BarResponse(Result.BUSY, detail)
        if status in (400, 413, 415, 422):
            return BarResponse(Result.REJECTED, detail)
        if status in (401, 403):
            return BarResponse(Result.AUTH, detail)
        return BarResponse(Result.ERROR, detail)

    # ----------------------------------------------------------------- endpoints
    def draw(self, payload: dict) -> BarResponse:
        return self._request("POST", "/api/display/draw", payload)

    def upload_asset(self, app_name: str, remote_name: str, data: bytes) -> BarResponse:
        """Upload a file into the app's assets folder (overwrites the same name)."""
        path = (f"/api/assets/upload?application_name={quote(app_name, safe='')}"
                f"&file={quote(remote_name, safe='')}")
        return self._request("POST", path, None, raw=data)

    def play_sound(self, app_name: str, stock_path: str | None, asset_path: str | None
                   ) -> BarResponse:
        body = {"application_name": app_name}
        if stock_path:
            body["stock_path"] = stock_path
        elif asset_path:
            body["path"] = asset_path
        else:
            return BarResponse(Result.REJECTED, "no sound path configured")
        return self._request("POST", "/api/audio/play", body)

    def status(self) -> BarResponse:
        return self._request("GET", "/api/status", None)

    def version(self) -> BarResponse:
        return self._request("GET", "/api/version", None)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# ---------------------------------------------------------------------- layout
def estimate_dwell(title: str, *, base: int, max_dwell: int, char_px: float,
                   scroll_rate: int, scroll_start_delay_ms: int, view_px: int = 72,
                   margin: float = 2.0) -> int:
    """Seconds to leave an item up so a long title can scroll through once.

    Never less than `base`, never more than `max_dwell`. A title that fits in
    the 72 px window (or scroll_rate 0) just gets `base`.
    """
    overflow_px = len(title) * char_px - view_px
    if overflow_px <= 0 or scroll_rate <= 0:
        return max(1, base)
    needed = scroll_start_delay_ms / 1000.0 + overflow_px / (scroll_rate / 60.0) + margin
    return int(math.ceil(min(max(needed, base), max_dwell)))


SCREEN_W = 72
ICON_W = 16
ICON_GAP = 2   # blank pixels between the icon and the text


def text_origin(has_icon: bool) -> tuple[int, int]:
    """(x, width) of the text column: right of the icon, or the full width."""
    x = ICON_W + ICON_GAP if has_icon else 0
    return x, SCREEN_W - x


def build_draw_payload(title: str, price: str, dwell: int, *, app_name: str,
                       priority: int, scroll_rate: int, scroll_start_delay_ms: int,
                       scroll_repeat_delay_ms: int, led_color: str | None,
                       price_color: str = "#66CCFFFF", price_font: str = "small",
                       scroll_price: bool = False, icon_path: str | None = None) -> dict:
    """Two-row front-screen layout, optionally with a 16x16 icon on the left.

    Element order is title, price, then icon. The title always scrolls when it
    is too wide. The price line scrolls only when scroll_price is true; otherwise
    it carries no scroll settings at all (so it can never move).
    """
    text_x, text_w = text_origin(bool(icon_path))
    scroll = {"scroll_rate": scroll_rate, "scroll_start_delay": scroll_start_delay_ms,
              "scroll_repeat_delay": scroll_repeat_delay_ms}
    price_el = {
        "id": "price", "type": "text", "display": "front",
        "timeout": dwell, "text": price, "font": price_font,
        "color": price_color, "align": "bottom_left", "x": text_x, "y": 15,
    }
    if scroll_price:
        price_el.update(width=text_w, **scroll)
    elements = [
        {
            "id": "title", "type": "text", "display": "front",
            "timeout": dwell, "text": title, "font": "small",
            "color": "#FFFFFFFF", "align": "top_left", "x": text_x, "y": 0,
            "width": text_w, **scroll,
        },
        price_el,
    ]
    if icon_path:
        elements.append({
            "id": "icon", "type": "image", "display": "front", "timeout": dwell,
            "path": icon_path, "align": "top_left", "x": 0, "y": 0,
        })
    payload = {"application_name": app_name, "priority": priority, "elements": elements}
    if led_color:
        payload["led_notification_color"] = led_color
    return payload
