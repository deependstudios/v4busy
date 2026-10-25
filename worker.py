"""Display worker: shows queued items on the bar, strictly one at a time.

Why serial: a new draw immediately replaces whatever is on screen (verified on
the real bar), so the next item may only be sent after the current item's
timeout has elapsed. The bar clears elements itself, so no clear calls are made.

Per-iteration behaviour:
  OK        remove item from the queue, optional sound, wait dwell + gap
  BUSY      keep item at the head, retry every busy_retry seconds (work session)
  REJECTED  our request was malformed: log and drop the item
  AUTH/ERROR keep item, exponential backoff (capped); item still expires by age

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from bar import BarClient, Result, build_draw_payload, estimate_dwell, text_origin
from icon import IconManager
from store import QueueItem, Store

LOG = logging.getLogger("rfy.worker")


@dataclass
class DisplaySettings:
    app_name: str = "rfy_feed"
    priority: int = 50
    dwell: int = 15              # seconds an item stays up (normal)
    backlog_dwell: int = 8       # shorter dwell while many items wait
    backlog_threshold: int = 5   # "many" = more than this many waiting behind the current item
    max_dwell: int = 40          # ceiling when stretching dwell for long titles
    gap: float = 1.0             # blank-ish pause between items
    char_px: float = 6.0         # estimated pixel width per character (small font)
    scroll_rate: int = 1000      # pixels per minute (as tested on the bar)
    scroll_start_delay_ms: int = 1000
    scroll_repeat_delay_ms: int = 2500
    busy_retry: float = 10.0
    error_backoff_max: float = 60.0
    led_color: str | None = "#FF8800FF"
    # price-line colours by kind, and the font used for the never-scrolling TAX FREE line
    color_normal: str = "#66CCFFFF"    # light blue
    color_gold: str = "#FF8800FF"      # orange
    color_taxfree: str = "#00FF00FF"   # green
    color_unknown: str = "#FF0000FF"   # red
    taxfree_font: str = "small"
    sound_enabled: bool = False
    sound_stock_path: str | None = None
    sound_asset_path: str | None = None


def safe(value, limit: int = 80) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(value))[:limit]


class DisplayWorker(threading.Thread):
    def __init__(self, store: Store, bar: BarClient, settings: DisplaySettings,
                 icon: IconManager | None = None):
        super().__init__(name="display-worker", daemon=True)
        self._store = store
        self._bar = bar
        self._s = settings
        self._icon = icon
        self._halt = threading.Event()   # NOT "_stop": Thread uses that name internally
        self._wake = threading.Event()
        self._state = "idle"             # idle | ok | busy | error (for transition logging)

    # ------------------------------------------------------------------ control
    def notify(self) -> None:
        """Call after enqueueing so an idle worker reacts immediately."""
        self._wake.set()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()

    # --------------------------------------------------------------------- loop
    def run(self) -> None:
        errors = 0
        while not self._halt.is_set():
            try:
                errors = self._step(errors)
            except Exception:  # never let the worker thread die silently
                LOG.exception("unexpected error in display worker; continuing")
                self._halt.wait(5.0)

    def _step(self, errors: int) -> int:
        item, expired = self._store.next_item()
        if expired:
            LOG.warning("expired %d queued item(s) older than the age limit (never shown)",
                        expired)
        if item is None:
            self._wake.wait(2.0)
            self._wake.clear()
            return 0

        waiting = max(0, self._store.queue_len() - 1)
        icon_path = self._icon.ensure(self._bar) if self._icon else None
        _, text_w = text_origin(bool(icon_path))
        taxfree = item.price_kind == "taxfree"
        # TAX FREE lines never scroll; every other price line scrolls only if it is
        # estimated to be wider than the text column.
        scroll_price = (not taxfree) and len(item.price) * self._s.char_px > text_w
        dwell = self._dwell_for(item, waiting, text_w, scroll_price)
        payload = build_draw_payload(
            item.title, item.price, dwell, app_name=self._s.app_name,
            priority=self._s.priority, scroll_rate=self._s.scroll_rate,
            scroll_start_delay_ms=self._s.scroll_start_delay_ms,
            scroll_repeat_delay_ms=self._s.scroll_repeat_delay_ms,
            led_color=self._s.led_color,
            price_color=self._price_color(item.price_kind),
            price_font=self._s.taxfree_font if taxfree else "small",
            scroll_price=scroll_price, icon_path=icon_path)
        resp = self._bar.draw(payload)

        if resp.result is Result.OK:
            self._store.mark_done(item.id)
            self._transition("ok", "bar accepting draws")
            LOG.info("displayed %s eventId=%s title=%r price=%r dwell=%ds waiting=%d",
                     item.kind, safe(item.event_id), safe(item.title), safe(item.price),
                     dwell, waiting)
            self._maybe_sound()
            self._halt.wait(dwell + self._s.gap)
            return 0

        if resp.result is Result.BUSY:
            self._transition("busy", f"bar busy (likely a work session); holding "
                                     f"{waiting + 1} item(s), retrying every "
                                     f"{self._s.busy_retry:g}s [{resp.detail}]")
            self._halt.wait(self._s.busy_retry)
            return 0

        if resp.result is Result.REJECTED:
            LOG.error("bar rejected our request; dropping item eventId=%s title=%r [%s]",
                      safe(item.event_id), safe(item.title), resp.detail)
            self._store.mark_done(item.id)
            return 0

        # AUTH or ERROR: keep the item, back off
        self._store.bump_attempts(item.id)
        delay = min(self._s.busy_retry * (2 ** errors), self._s.error_backoff_max)
        level = logging.ERROR if resp.result is Result.AUTH else logging.WARNING
        label = "authentication failed (check RFY_BAR_TOKEN)" \
            if resp.result is Result.AUTH else "bar unreachable or erroring"
        if self._state != "error" or errors % 5 == 0:
            LOG.log(level, "%s [%s]; retrying in %gs (%d item(s) held)",
                    label, resp.detail, delay, waiting + 1)
        self._state = "error"
        self._halt.wait(delay)
        return errors + 1

    # ------------------------------------------------------------------ helpers
    def _price_color(self, kind: str) -> str:
        s = self._s
        return {"gold": s.color_gold, "taxfree": s.color_taxfree,
                "unknown": s.color_unknown}.get(kind, s.color_normal)

    def _dwell_for(self, item: QueueItem, waiting: int, text_w: int = 72,
                   scroll_price: bool = False) -> int:
        """Long enough for the longest scrolling line (title, or price if it scrolls)."""
        s = self._s
        backlog = waiting > s.backlog_threshold
        lo = s.backlog_dwell if backlog else s.dwell
        hi = max(lo, s.dwell if backlog else s.max_dwell)
        kw = dict(base=lo, max_dwell=hi, char_px=s.char_px, scroll_rate=s.scroll_rate,
                  scroll_start_delay_ms=s.scroll_start_delay_ms, view_px=text_w)
        dwell = estimate_dwell(item.title, **kw)
        if scroll_price:
            dwell = max(dwell, estimate_dwell(item.price, **kw))
        return dwell

    def _transition(self, new_state: str, message: str) -> None:
        if self._state != new_state:
            LOG.info("state: %s -> %s: %s", self._state, new_state, message)
            self._state = new_state

    def _maybe_sound(self) -> None:
        if not self._s.sound_enabled:
            return
        resp = self._bar.play_sound(self._s.app_name, self._s.sound_stock_path,
                                    self._s.sound_asset_path)
        if resp.result is not Result.OK:
            LOG.warning("sound not played [%s]", resp.detail)
