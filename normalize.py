"""Turn a webhook item into the exact strings the BUSY Bar will draw.

Pure functions, no I/O, never raise on bad input.

Rules (from the BUSY Bar OpenAPI + the V4 webhook contract):
  * The bar's text elements accept printable ASCII only and reject empty text,
    so titles are transliterated/stripped to ASCII with a non-empty fallback.
  * ETV is native Amazon USD in integer cents. null means "unknown", never zero;
    an observed $0 is shown as $0.00.

Designed by N8JRD - Jim Davis, Vibe Coded with Claude
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

FALLBACK_TITLE = "(untitled item)"
PRICE_UNAVAILABLE = "ETV n/a"

# Price "kinds" drive the colour on the bar (and whether the line may scroll).
KIND_NORMAL = "normal"     # light blue
KIND_GOLD = "gold"         # orange, "$NNN.NN GOLD"
KIND_TAXFREE = "taxfree"   # green, "$0.00 TAX FREE" (never scrolls)
KIND_UNKNOWN = "unknown"   # red, "ETV n/a"
DEFAULT_GOLD_MIN_CENTS = 10_000   # $100.00 and above is GOLD
MAX_TITLE_CHARS = 240  # the webhook bounds titles to 240 UTF-16 units

# Applied both before and after NFKD so compatibility decompositions
# (e.g. TM sign -> "TM", 1/2 -> "1<fraction slash>2") can't sneak back in.
_REPLACEMENTS = {
    "\u2018": "'", "\u2019": "'", "\u201A": "'", "\u2032": "'", "\u00B4": "'",
    "\u201C": '"', "\u201D": '"', "\u201E": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
    "\u2026": "...",
    "\u2022": "-", "\u00B7": "-",
    "\u00D7": "x",
    "\u00B0": " deg",
    "\u00B1": "+/-",
    "\u2044": "/",
    "\u2122": "", "\u00AE": "", "\u00A9": "",  # TM, (R), (C): dropped to save space
}
_TABLE = str.maketrans(_REPLACEMENTS)


def sanitize_title(raw) -> str:
    """Printable ASCII, single-spaced, at most 240 chars, never empty."""
    if not isinstance(raw, str):
        return FALLBACK_TITLE
    text = unicodedata.normalize("NFKD", raw.translate(_TABLE)).translate(_TABLE)
    out = []
    for ch in text:
        code = ord(ch)
        if ch.isspace():
            out.append(" ")
        elif 0x20 < code < 0x7F:
            out.append(ch)
        # everything else (accent marks, emoji, CJK, control chars) is dropped
    cleaned = " ".join("".join(out).split())[:MAX_TITLE_CHARS].rstrip()
    return cleaned or FALLBACK_TITLE


def format_price(etv) -> str:
    """'$1,234.56' from {'amountCents': int, 'currency': 'USD'}; else 'ETV n/a'."""
    if not isinstance(etv, dict):
        return PRICE_UNAVAILABLE
    cents = etv.get("amountCents")
    if type(cents) is not int or cents < 0 or etv.get("currency") != "USD":
        return PRICE_UNAVAILABLE  # bool, float, str, negative or non-USD: don't guess
    dollars, remainder = divmod(cents, 100)
    return f"${dollars:,}.{remainder:02d}"


def describe_price(etv, gold_min_cents: int = DEFAULT_GOLD_MIN_CENTS) -> tuple[str, str]:
    """(text, kind) for the price line.

    unknown/malformed -> ("ETV n/a", unknown)      shown red
    exactly $0.00     -> ("$0.00 TAX FREE", taxfree) shown green, never scrolls
    >= gold threshold -> ("$149.99 GOLD", gold)    shown orange
    anything else     -> ("$24.99", normal)        shown light blue
    """
    base = format_price(etv)
    if base == PRICE_UNAVAILABLE:
        return PRICE_UNAVAILABLE, KIND_UNKNOWN
    cents = etv["amountCents"]  # format_price already proved this is a valid int
    if cents == 0:
        return base + " TAX FREE", KIND_TAXFREE
    if cents >= gold_min_cents:
        return base + " GOLD", KIND_GOLD
    return base, KIND_NORMAL


@dataclass(frozen=True)
class DisplayItem:
    event_id: str
    title: str
    price: str
    price_kind: str = KIND_NORMAL


def normalize_item(event_id: str, item: dict,
                   gold_min_cents: int = DEFAULT_GOLD_MIN_CENTS) -> DisplayItem:
    price, kind = describe_price(item.get("etv"), gold_min_cents)
    return DisplayItem(
        event_id=event_id,
        title=sanitize_title(item.get("title")),
        price=price,
        price_kind=kind,
    )
