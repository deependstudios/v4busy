# RFY -> BUSY Bar receiver: setup

*Designed by N8JRD - Jim Davis, Vibe Coded with Claude*

Shows each V4 "Discovered RFY" item (title + ETV) on the BUSY Bar, one at a time.
Python 3.8+, no extra packages. Runs on the same PC as V4.

## Files
`receiver.py` (run this), `worker.py`, `bar.py`, `icon.py`, `normalize.py`, `store.py`,
`rfy_bar.env.example`, and your own `v4_icon.png` (16x16 PNG, optional).
Keep them all in one folder.

## 1. Bar access token
Create a named access token on the bar (POST `/api/access/tokens`, authenticated with
the bar's access key). Use that token, not the access key itself. If the access key has
ever been shared or pasted anywhere, rotate it.

## 2. Configure
1. Copy `rfy_bar.env.example` to `rfy_bar.env` (same folder as `receiver.py`).
2. Set the two required values:
   - `RFY_WEBHOOK_SECRET` = the signing secret V4 shows (starts with `whsec_`)
   - `RFY_BAR_TOKEN` = the named token from step 1
3. Check `RFY_BAR_URL` (default `http://192.168.0.85`).
4. Linux/macOS only: `chmod 600 rfy_bar.env`. Never share this file.

All other settings are optional and documented in the example file.

## 3. Icon (optional)
Put a 16x16 PNG named `v4_icon.png` next to `rfy_bar.env`. It can be swapped at any time
with no restart. If it is missing or invalid, text starts at the left edge.

## 4. Run
    python receiver.py

The log should show: store ready, "BUSY Bar reachable", and "icon file OK".
Stop with Ctrl+C. Queued items survive a restart. The receiver does not start
automatically; start it each time before using V4.

## 5. Point V4 at it
Set the V4 webhook URL to `http://127.0.0.1:8787/webhook` and paste the same signing
secret. Send V4's test event: the bar should show "V4 webhook test OK".

## What you will see
| Price line | Colour | Notes |
|---|---|---|
| ETV $0.00 | green, "$0.00 TAX FREE" | never scrolls |
| ETV $100.00 or more | orange, "$NNN.NN GOLD" | threshold: `RFY_GOLD_MIN_DOLLARS` |
| other ETV | light blue | |
| ETV unknown | red, "ETV n/a" | |

Items show 15 s each (8 s when more than 5 are waiting). The queue holds 25 items
(oldest dropped first); items older than 30 min are discarded. If a BUSY/CUSTOM work
session is running, items are held and shown when it ends. The LED blinks per item;
sound is off by default (`RFY_SOUND_ENABLED`).

## Troubleshooting
| Log message | Meaning / fix |
|---|---|
| `rejected the token` | Wrong `RFY_BAR_TOKEN`; items wait in the queue until fixed |
| `not reachable` | Check `RFY_BAR_URL` and that the bar is on; items wait |
| `bar busy` | A work session owns the screen; items are held, not lost |
| `icon file unusable` | `v4_icon.png` is not exactly 16x16 |
| Nothing arrives from V4 | URL/port must match; secrets must match; PC clock must be within 5 min |

Files created at runtime: `rfy_bar.db` (queue and de-duplication; safe to delete when stopped).
