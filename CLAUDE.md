# CLAUDE.md — Clockmatrix (Pico meeting/message clock)

This file is read automatically by Claude Code at the start of every session
in this directory. It captures the architecture and design decisions for
this project specifically (independent of any other project's CLAUDE.md).

---

## Project purpose

A combination desk clock / meeting reminder / alarm / scrolling-message
display built on a Raspberry Pi Pico (MicroPython). A large LED matrix shows
the time and a scrolling ticker; a small OLED provides an interactive menu
(rotary encoder + button) for configuration, entirely off the main display.

---

## Hardware

| Component | Detail |
|---|---|
| MCU | Raspberry Pi Pico (RP2040), MicroPython |
| Matrix | 8x MAX7219 8x8 modules, arranged 2 rows x 4 cols (32x16 logical) — SPI |
| OLED | SSD1306 128x64, I2C addr 0x3C — all menus/UI |
| RTC | DS3231, I2C addr 0x68 — shares the OLED's I2C bus |
| Input | KY-040 rotary encoder + push button (enc_sw), separate back button (GP19) |
| Buzzer | Piezo, PWM-driven |

### Pin assignments (`clockmatrix.py` top of file)

| Function | GPIO |
|---|---|
| Matrix SPI SCK / MOSI / CS | 2 / 3 / 5 |
| I2C SDA / SCL (OLED + RTC) | 0 / 1 |
| Encoder A / B / SW | 16 / 17 / 18 |
| Back button | 19 |
| Buzzer | 15 |

The matrix panel **never** shows menus — only the clock, the scrolling
meeting/message ticker, and the alarm flash. All interactivity lives on the
OLED.

---

## Controls

| Action | Effect |
|---|---|
| Rotate | Navigate menu / adjust field being edited |
| Short press (encoder button) | Select / advance to next field |
| Long press (≥600ms, encoder button) | "Done" — jump straight to finishing the current screen (skip scrolling to `>` in the text picker, or stepping through remaining fields) |
| GP19 short press | Back / backspace |

Long-press detection lives in `_press_event()` — it fires the `'long'` event
once, immediately upon crossing the threshold while still held (not on
release), so the UI feels responsive.

The OLED blanks (`poweroff()`) after `OLED_SAVER_MS` (30 s) of no encoder/
button activity, to avoid burn-in — but only while sitting idle on the
`dash` screen; menus, edit/type screens, and the alarm flash never sleep. Any
rotation or button press wakes it instantly (`S.last_input` timestamp, checked
in `render_oled`).

---

## Source file map

```
clockmatrix/
├── CLAUDE.md            ← you are here
├── clockmatrix.py       ← the entire device firmware (single file)
├── push_to_clock.py     ← PC-side companion: pushes time/messages/alarms/
│                           calendar meetings over USB serial
└── wiring.svg           ← hardware wiring diagram
```

`clockmatrix.py` is organized top-to-bottom as: pin config → `Matrix` (MAX7219
driver) → `DS3231` (RTC driver) → compact 4x7 font for the matrix clock →
`State` (single global `S`) → peripherals/IRQ → serial command handler →
async tasks (clock/alarm/ui/serial) → UI state-transition functions
(`ui_select`, `ui_done`, `ui_back`, `ui_rotate`) → OLED renderers → matrix
renderer → `main()`.

---

## UI state machine

`S.ui` is one of: `dash | menu | edit | type | preset | bright | tclock`.

- **dash** — default screen: date/time, next meeting, next alarm.
- **menu** — scrollable list (`MENU_IDS` / `MENU_LABELS`): Set Time, Set
  Date, Add Alarm, Add Meeting, New Message, Quick Message, Brightness,
  Ticker Clock, Exit.
- **edit** — field-by-field editor driven by `EDIT_SPECS[target]` (a list of
  `(min, max)` per field): 2 fields (HH, MM) for time/alarm/meeting, 3 fields
  (DD, MM, YY) for date. Adding a meeting chains into `type` to capture its
  label afterward. Setting the date recomputes day-of-week via
  `day_of_week()` (Sakamoto's algorithm) rather than trusting user input.
- **type** — on-screen character picker (`PICK` list, includes `<`=backspace
  and `>`=done) for free-text messages and meeting labels.
- **preset** — scrollable list of canned messages (`PRESETS`) that get
  appended to `S.messages` directly, no typing required.
- **bright** — adjusts matrix brightness (0-15).
- **tclock** — cycles `TICKER_CLOCK_MODES` (`Off | Time | Time+Date |
  Time+Date+Day`), controlling whether/how the current date & time appear as
  a scrolling entry in the matrix ticker (`ticker_clock_str()`, prepended in
  `build_ticker()`), alongside meetings and messages.

`ui_select()` handles short-press transitions; `ui_done()` handles long-press
"jump to finish" for the same states, sharing commit logic via
`_finish_edit()` / `_finish_type()`.

---

## Persisted settings

Alarms, custom/quick messages, brightness, and the ticker-clock mode are
saved as JSON to `/settings.json` on the Pico's internal flash filesystem
(`save_settings()` / `load_settings()` in `clockmatrix.py`), and reloaded at
boot in `main()`. Meetings are **not** persisted — they're expected to be
re-synced daily from `push_to_clock.py` (calendar pull or `G`/`C` serial
commands), so surviving a reboot with stale meetings would be wrong.

---

## Matrix rendering (`Matrix.show()`)

The 8 MAX7219 modules are logically arranged as a 32x16 canvas but wired as
two daisy-chained bands of 4, each individually mounted upside-down (not
mirrored left-right). `show()` maps each module to its framebuffer region
(`x0`, `y`) and column bit order (`flip`) per row, then shifts all 8 bytes
out per SPI transaction. Both bands use the same left-to-right `x0` order
and `flip=False`; only `y` is inverted per band (`7-row` / `15-row`) to
account for the upside-down mounting.

**This mapping is board-wiring-dependent** and has already gone through two
rounds of tuning: first a whole-panel left-right mirror (fixed by mirroring
`x0` about the panel center), then a bottom-band-only reversal discovered via
the scrolling ticker — the bottom band had `x0` mirrored *and* `flip`
inverted, which combined into a horizontal mirror + reversed scroll direction
for anything drawn there (the clock digits, confined to the top band, never
exposed it). If modules are ever rewired or replaced, expect another round
of tuning here.

---

## Serial protocol (PC ↔ device)

`push_to_clock.py` talks to the Pico over USB-CDC serial using a tiny
line-based protocol handled by `handle_cmd()`:

| Prefix | Meaning |
|---|---|
| `Y<yy,mo,dd,dow,hh,mm,ss>` | Full datetime sync |
| `T<hh:mm:ss>` | Time-only sync |
| `M<text>` | Push a scrolling message |
| `A<hh:mm>` | Add an alarm |
| `G<hh:mm>\|<label>` | Add a meeting |
| `C` | Clear all meetings (used before a fresh calendar re-sync) |

`push_to_clock.py` also supports pulling today's events from Google Calendar
(`gcal`, with OAuth via `credentials.json`/`token.json`) or from a local
`.ics` file (`cal`), converting them into `G...` commands.

---

## Deploying to the device

The device's filesystem has **two copies** of the firmware: `main.py`
(auto-run at boot) and `clockmatrix.py`. They are not linked — pushing an
update to one and not the other leaves the device running stale code. To
deploy: `mpremote connect <port> fs cp clockmatrix.py :clockmatrix.py`, then
also `cp clockmatrix.py :main.py`, then `mpremote connect <port> soft-reset`.
