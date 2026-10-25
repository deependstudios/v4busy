"""Icon handling: read v4_icon.png from disk, validate it, upload it to the bar.

The icon file lives next to rfy_bar.env so it can be swapped at any time. The
file is re-checked before every draw (one cheap stat call); when its size or
modification time changes it is re-validated and re-uploaded, with no restart.

ensure() NEVER blocks display. If the icon is missing, invalid, refused by the
bar, or the upload fails, it returns None and the caller draws without the icon
(text starts at the left edge). A failed upload is retried later.

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import logging
import os
import struct
import time

from bar import BarClient, Result

LOG = logging.getLogger("rfy.icon")

REMOTE_NAME = "v4_icon.png"        # name inside the bar's per-app assets folder
ICON_SIZE = (16, 16)
MAX_BYTES = 64 * 1024              # a 16x16 icon is ~1 KB; this is just a sanity cap
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
UPLOAD_RETRY_SECONDS = 30.0


def validate_png(data: bytes) -> str | None:
    """None if `data` is a PNG of exactly 16x16 pixels, else a plain-English problem."""
    if len(data) > MAX_BYTES:
        return f"file is {len(data)} bytes (limit {MAX_BYTES})"
    if len(data) < 33 or not data.startswith(PNG_MAGIC):
        return "not a PNG file"
    if data[12:16] != b"IHDR":
        return "damaged PNG header"
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != ICON_SIZE:
        return f"image is {width}x{height}, must be {ICON_SIZE[0]}x{ICON_SIZE[1]}"
    return None


class IconManager:
    def __init__(self, path: str, app_name: str):
        self._path = path
        self._app = app_name
        self._seen_sig = "unset"      # signature the validation below was done for
        self._data: bytes | None = None
        self._problem: str | None = None
        self._uploaded_sig = None
        self._retry_after = 0.0
        self._state = ""

    # ------------------------------------------------------------------ helpers
    def _signature(self):
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _set_state(self, state: str, level: int, message: str) -> None:
        if state != self._state:
            self._state = state
            LOG.log(level, message)

    def describe(self) -> str:
        """Local-only description for the startup log (no network)."""
        sig = self._signature()
        if sig is None:
            return f"icon file not found ({self._path}): items will show without an icon"
        try:
            with open(self._path, "rb") as fh:
                problem = validate_png(fh.read(MAX_BYTES + 1))
        except OSError as exc:
            problem = f"cannot read file: {exc}"
        if problem:
            return f"icon file unusable ({problem}): items will show without an icon"
        return f"icon file OK ({self._path})"

    # --------------------------------------------------------------------- main
    def ensure(self, bar: BarClient) -> str | None:
        """Return the bar-side asset name to draw, or None to draw without an icon."""
        sig = self._signature()
        if sig is None:
            self._seen_sig, self._data, self._problem, self._uploaded_sig = None, None, None, None
            self._set_state("missing", logging.WARNING,
                            f"icon file not found ({self._path}): showing items without an "
                            f"icon, text starts at the left edge")
            return None

        if sig != self._seen_sig:                       # new or changed file
            self._seen_sig = sig
            self._state = ""                            # so the outcome is logged again
            self._uploaded_sig = None
            self._retry_after = 0.0
            try:
                with open(self._path, "rb") as fh:
                    data = fh.read(MAX_BYTES + 1)
                self._problem = validate_png(data)
                self._data = None if self._problem else data
            except OSError as exc:
                self._problem, self._data = f"cannot read file: {exc}", None

        if self._problem:
            self._set_state("invalid", logging.ERROR,
                            f"icon file unusable ({self._problem}): showing items without an icon")
            return None

        if self._uploaded_sig == sig:
            return REMOTE_NAME

        if time.monotonic() < self._retry_after:
            return None
        resp = bar.upload_asset(self._app, REMOTE_NAME, self._data)
        if resp.result is Result.OK:
            self._uploaded_sig = sig
            self._set_state("active", logging.INFO, f"icon uploaded to the bar ({self._path})")
            return REMOTE_NAME
        if resp.result is Result.REJECTED:              # the bar refuses this file
            self._problem = f"bar refused the upload [{resp.detail}]"
            self._set_state("invalid", logging.ERROR,
                            f"icon file unusable ({self._problem}): showing items without "
                            f"an icon until the file changes")
            return None
        self._retry_after = time.monotonic() + UPLOAD_RETRY_SECONDS
        self._set_state("upload-failed", logging.WARNING,
                        f"icon upload failed [{resp.detail}]: showing items without an icon, "
                        f"will retry")
        return None
