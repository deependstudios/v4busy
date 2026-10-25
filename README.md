# RFY BUSY Bar Display

Show each item from V4's **Discovered RFY** webhook on a [BUSY Bar](https://busy.bar) as it arrives: a 16x16 icon, the item title, and its ETV colour-coded by value.

A small, dependency-free Python service. It receives V4's signed webhooks, queues the items, and shows them on the bar one at a time through the bar's local HTTP API.

*Designed by N8JRD - Jim Davis, Vibe Coded with Claude*

## Features

- **Verified webhooks.** Standard Webhooks HMAC-SHA256 signature check, timestamp freshness window, 8 KiB body limit, and duplicate suppression by `eventId`.
- **One item at a time.** A persistent queue (SQLite) means an unseen item is never overwritten. The queue survives restarts.
- **Colour-coded price line.**

  | ETV | Display | Colour |
  |---|---|---|
  | $0.00 | `$0.00 TAX FREE` (never scrolls) | green |
  | $100.00 or more | `$NNN.NN GOLD` | orange |
  | anything else | `$NN.NN` | light blue |
  | unknown | `ETV n/a` | red |

- **Icon.** A 16x16 `v4_icon.png` sits on the left with the text beside it. Swap the file any time without restarting. If it is missing, text starts at the left edge.
- **Scrolling titles.** Long titles scroll; dwell time stretches so they finish.
- **Work-session aware.** If a BUSY/CUSTOM work session blocks the screen, items are held and shown when it ends.
- **LED blink** on each item; optional **sound** (off by default).
- **Test event.** V4's `webhook.test` shows "V4 webhook test OK" on the bar.

## Requirements

- Python 3.8 or newer (standard library only, nothing to install)
- A BUSY Bar reachable on your network, with a **named access token**
- V4 on the same PC (the receiver listens on `127.0.0.1` by default)

## Quick start

1. Copy `rfy_bar.env.example` to `rfy_bar.env`.
2. Set the two required values:
   - `RFY_WEBHOOK_SECRET`: the signing secret V4 shows (starts with `whsec_`)
   - `RFY_BAR_TOKEN`: a named access token created on the bar
3. Set `RFY_BAR_URL` if your bar is not at `http://192.168.0.85`.
4. Optional: add a 16x16 `v4_icon.png` next to `rfy_bar.env`.
5. Run:

   ```
   python receiver.py
   ```

6. In V4, set the webhook URL to `http://127.0.0.1:8787/webhook` with the same secret, then send V4's test event.

See [`SETUP.md`](SETUP.md) for the full walkthrough and troubleshooting.

## Configuration

Everything is set in `rfy_bar.env` (or as environment variables, which take precedence). Only the first two are required.

| Setting | Default | Purpose |
|---|---|---|
| `RFY_WEBHOOK_SECRET` | | V4 signing secret |
| `RFY_BAR_TOKEN` | | Bar named access token |
| `RFY_BAR_URL` | `http://192.168.0.85` | Bar address |
| `RFY_HOST` / `RFY_PORT` / `RFY_PATH` | `127.0.0.1` / `8787` / `/webhook` | Listener |
| `RFY_DWELL_SECONDS` | `15` | Seconds per item |
| `RFY_BACKLOG_DWELL_SECONDS` / `RFY_BACKLOG_THRESHOLD` | `8` / `5` | Faster cycling when many items wait |
| `RFY_QUEUE_MAX` | `25` | Queue cap (oldest dropped first) |
| `RFY_QUEUE_EXPIRY_SECONDS` | `1800` | Discard items older than this |
| `RFY_GOLD_MIN_DOLLARS` | `100` | GOLD threshold |
| `RFY_COLOR_NORMAL` / `_GOLD` / `_TAXFREE` / `_UNKNOWN` | see example | Price colours (`#RRGGBBAA`) |
| `RFY_TAXFREE_FONT` | `small` | Font for the never-scrolling TAX FREE line |
| `RFY_LED_ENABLED` / `RFY_LED_COLOR` | `true` / `#FF8800FF` | LED blink |
| `RFY_SOUND_ENABLED` | `false` | Sound toggle (see the example file for path options) |

The example file documents every option.

## How it works

```
V4  --signed POST-->  receiver  -->  SQLite queue  -->  display worker  -->  BUSY Bar
                      verify,         dedupe +          one item at a        POST /api/display/draw
                      normalise       persistence       time, retries
```

- A new draw replaces the screen immediately, so the worker only sends the next item after the current one's timeout.
- Draws use priority 50 so they yield to BUSY work sessions; a refusal (HTTP 409) is treated as "busy", and the item is kept and retried.
- Titles are reduced to printable ASCII, since the bar's text API accepts nothing else.

## Security notes

- The receiver binds to loopback only by default. If you change `RFY_HOST`, restrict the port with a firewall.
- Requests without a valid signature or with a stale timestamp are rejected, and webhook payloads cannot influence where the service sends requests (only the configured bar URL is used).
- The bar token and webhook secret live only in `rfy_bar.env` and are never logged. Do not commit that file.
- Use a **named access token**, not the bar's access key, so it can be revoked on its own.

## Limitations

- Runs in the foreground; there is no autostart or service wrapper. Start it before using V4.
- Scroll timing uses an estimated character width (`RFY_CHAR_PX`, default 6) and may need tuning for your fonts.
- ASCII only: non-ASCII characters in titles are replaced.

## Suggested `.gitignore`

```
rfy_bar.env
rfy_bar.db*
__pycache__/
```

## Credits

Designed by N8JRD - Jim Davis, Vibe Coded with Claude.

## License

Released under the [MIT License](LICENSE).
