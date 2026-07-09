# Copyright (C) 2026 Erico Mendonca <erico.mendonca@gmail.com>
# Licensed under the GNU General Public License v3.0. See LICENSE for details.
#
# clockmatrix.py -- MicroPython for Raspberry Pi Pico
# Combination clock / meeting clock / alarm / message display.
#
# Hardware:
#   8x MAX7219 8x8 arranged 2 rows x 4 cols (32x16)  -> DISPLAY ONLY
#   SSD1306 128x64 OLED (I2C 0x3C)                   -> all menus / UI
#   DS3231 RTC (I2C 0x68) . rotary encoder + button . piezo buzzer
#
# The matrix panel never shows menus: it only shows the clock, the scrolling
# meeting/message ticker, and the alarm flash. Everything interactive lives on
# the OLED, which shares the DS3231's I2C bus (no extra pins).
#
# Dependency: copy the standard ssd1306.py to the Pico
#   (mpremote mip install ssd1306) so `import ssd1306` works.
#
# CONTROLS: rotate = navigate/edit . press = select . long-press = done
#           GP19 = back/backspace
#
# NOTE: MAX7219 module order + per-band rotation usually need one round of
# bench-tuning; if the image is scrambled/mirrored, tweak Matrix.show().

import uasyncio as asyncio
from machine import Pin, SPI, I2C, PWM
from ssd1306 import SSD1306_I2C
import framebuf, time, sys, select, ujson

# ----------------------------- Config / pins -----------------------------
SPI_ID, PIN_SCK, PIN_MOSI, PIN_CS = 0, 2, 3, 5
I2C_ID, PIN_SDA, PIN_SCL          = 0, 0, 1
PIN_ENC_A, PIN_ENC_B, PIN_ENC_SW  = 16, 17, 18
PIN_BTN, PIN_BUZZ                 = 19, 15
W, H, MODULES = 32, 16, 8
OLED_W, OLED_H = 128, 64

# ------------------- Matrix: 32x16 framebuffer -> chain -------------------
class Matrix:
    def __init__(self, spi, cs_pin):
        self.spi = spi
        self.cs  = Pin(cs_pin, Pin.OUT, value=1)
        self.buf = bytearray(W * H // 8)
        self.fb  = framebuf.FrameBuffer(self.buf, W, H, framebuf.MONO_HLSB)
        for reg, val in ((0x09, 0), (0x0A, 2), (0x0B, 7), (0x0C, 1), (0x0F, 0)):
            self._all(reg, val)
    def _all(self, reg, val):
        self.cs(0)
        for _ in range(MODULES):
            self.spi.write(bytes((reg, val)))
        self.cs(1)
    def brightness(self, n):
        self._all(0x0A, max(0, min(15, int(n))))
    def show(self):
        fb = self.fb
        for row in range(8):
            packet = bytearray()
            for m in range(MODULES - 1, -1, -1):
                if m < 4:                       # top band, upside down
                    x0, y, flip = 8 * m, 7 - row, False
                else:                           # bottom band, upside down
                    x0, y, flip = 8 * (m - 4), 15 - row, False
                b = 0
                for c in range(8):
                    if fb.pixel(x0 + c, y):
                        b |= 1 << ((7 - c) if flip else c)
                packet += bytes((0x01 + row, b))
            self.cs(0); self.spi.write(packet); self.cs(1)

# ------------------------------- DS3231 RTC ------------------------------
class DS3231:
    def __init__(self, i2c, addr=0x68):
        self.i2c, self.addr = i2c, addr
    @staticmethod
    def _b2d(v): return (v >> 4) * 10 + (v & 0x0F)
    @staticmethod
    def _d2b(v): return ((v // 10) << 4) | (v % 10)
    def full(self):
        d = self.i2c.readfrom_mem(self.addr, 0, 7)
        return (self._b2d(d[2] & 0x3F), self._b2d(d[1] & 0x7F), self._b2d(d[0] & 0x7F),
                d[3] & 0x07, self._b2d(d[4] & 0x3F), self._b2d(d[5] & 0x1F), self._b2d(d[6]))
    def set(self, h, mi, s=0, dow=1, dd=1, mo=1, yy=26):
        self.i2c.writeto_mem(self.addr, 0, bytes((
            self._d2b(s), self._d2b(mi), self._d2b(h),
            dow, self._d2b(dd), self._d2b(mo), self._d2b(yy))))

# --------------------------- Compact 4x7 font (matrix clock) -------------
FONT = {
    '0': (0x6,0x9,0x9,0x9,0x9,0x9,0x6), '1': (0x2,0x6,0x2,0x2,0x2,0x2,0x7),
    '2': (0x6,0x9,0x1,0x2,0x4,0x8,0xF), '3': (0xE,0x1,0x1,0x6,0x1,0x1,0xE),
    '4': (0x2,0x6,0xA,0xF,0x2,0x2,0x2), '5': (0xF,0x8,0xE,0x1,0x1,0x9,0x6),
    '6': (0x6,0x9,0x8,0xE,0x9,0x9,0x6), '7': (0xF,0x1,0x2,0x2,0x4,0x4,0x4),
    '8': (0x6,0x9,0x9,0x6,0x9,0x9,0x6), '9': (0x6,0x9,0x9,0x7,0x1,0x9,0x6),
    ':': (0x0,0x4,0x0,0x0,0x4,0x0,0x0), ' ': (0,0,0,0,0,0,0),
}
def draw_glyph(fb, ch, x, y):
    g = FONT.get(ch)
    if not g: return
    for r in range(7):
        for c in range(4):
            if g[r] & (1 << (3 - c)):
                fb.pixel(x + c, y + r, 1)
def draw_hhmm(fb, h, m, colon):
    s = "%02d%c%02d" % (h, ':' if colon else ' ', m)
    x = 4
    for ch in s:
        draw_glyph(fb, ch, x, 0); x += 5

DOW = ["", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MON = ["", "Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

def day_of_week(yy, mo, dd):
    """Sakamoto's algorithm; returns 1=Mon..7=Sun to match DOW[] indexing."""
    y = 2000 + yy
    t = (0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4)
    if mo < 3: y -= 1
    w = (y + y // 4 - y // 100 + y // 400 + t[mo - 1] + dd) % 7
    return w if w else 7

# --------------------------------- State ---------------------------------
class State:
    def __init__(self):
        self.h = self.m = self.s = 0
        self.dow = self.dd = self.mo = self.yy = 0
        self.messages = []
        self.alarms   = []            # [(h, m), ...]
        self.meetings = []            # [(h, m, label), ...]
        self.alarm_firing = False
        self.alarm_start = 0          # tick_ms the current alarm started ringing
        self.alarm_timeout = 60       # auto-stop after this many s (0 = never)
        self.brightness = 10          # fixed level for a lit room
        self.ticker_clock = 1         # index into TICKER_CLOCK_MODES
        self.scroll_x = W
        self.ui = 'dash'              # dash|menu|edit|type|preset|bright|tclock|atimeout
        self.menu_idx = 0
        self.edit = [0, 0]; self.edit_field = 0; self.edit_target = None
        self.buf = ""; self.pick = 0; self.type_target = None; self.pending = None
        self.preset_idx = 0
        self.last_input = time.ticks_ms()
        self.oled_on = True
        self.blip_until = 0       # buzzer "tick" feedback active until this tick_ms
S = State()

OLED_SAVER_MS = 30_000            # blank OLED after this much idle time
BLIP_MS       = 40                # length of the encoder feedback "tick"
BLIP_FREQ     = 3200              # distinct from the 2000 Hz alarm tone
ALARM_TIMEOUT_MAX  = 600          # cap the configurable auto-stop at 10 min
ALARM_TIMEOUT_STEP = 15           # adjust the auto-stop in 15 s increments

MENU_IDS    = ["time", "date", "alarm", "mtg", "msg", "preset", "bright", "tclock", "atimeout", "exit"]
MENU_LABELS = ["Set Time", "Set Date", "Add Alarm", "Add Meeting", "New Message",
               "Quick Message", "Brightness", "Ticker Clock", "Alarm Timeout", "Exit"]
PICK = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?-<>")  # '<'=bksp '>'=done
PRESETS = ["Back in 5 min", "In a meeting", "Lunch break", "Do not disturb",
           "BRB", "Gone for the day", "Happy Birthday!", "On a call"]

# how much of date/time the matrix ticker shows, cycled via the "Ticker Clock" menu
TICKER_CLOCK_MODES = ["Off", "Time", "Time+Date", "Time+Date+Day"]
def ticker_clock_str():
    mode = TICKER_CLOCK_MODES[S.ticker_clock]
    if mode == "Off": return ""
    t = "%02d:%02d" % (S.h, S.m)
    if mode == "Time": return t
    d = "%02d %s" % (S.dd, MON[S.mo] if 0 < S.mo < 13 else "")
    if mode == "Time+Date": return "%s %s" % (t, d)
    dow = DOW[S.dow] if 0 <= S.dow < 8 else ""
    return "%s %s %s" % (t, dow, d)

# fields per edit target: list of (min, max) inclusive, in edit[] order
EDIT_SPECS = {
    "time":  [(0, 23), (0, 59)],
    "alarm": [(0, 23), (0, 59)],
    "mtg":   [(0, 23), (0, 59)],
    "date":  [(1, 31), (1, 12), (0, 99)],   # dd, mo, yy
}

SETTINGS_FILE = "/settings.json"
def save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            ujson.dump({"alarms": S.alarms, "messages": S.messages,
                        "brightness": S.brightness,
                        "ticker_clock": S.ticker_clock,
                        "alarm_timeout": S.alarm_timeout}, f)
    except OSError:
        pass

def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            d = ujson.load(f)
        S.alarms       = [tuple(a) for a in d.get("alarms", [])]
        S.messages     = list(d.get("messages", []))
        S.brightness   = d.get("brightness", S.brightness)
        S.ticker_clock = d.get("ticker_clock", S.ticker_clock)
        S.alarm_timeout = d.get("alarm_timeout", S.alarm_timeout)
    except (OSError, ValueError):
        pass

# ------------------------------ Peripherals ------------------------------
enc_a  = Pin(PIN_ENC_A, Pin.IN, Pin.PULL_UP)
enc_b  = Pin(PIN_ENC_B, Pin.IN, Pin.PULL_UP)
enc_sw = Pin(PIN_ENC_SW, Pin.IN, Pin.PULL_UP)
btn    = Pin(PIN_BTN,   Pin.IN, Pin.PULL_UP)
buzz   = PWM(Pin(PIN_BUZZ)); buzz.duty_u16(0)
enc_delta = 0
def _enc_irq(p):
    global enc_delta
    enc_delta += 1 if enc_a.value() == enc_b.value() else -1
enc_a.irq(_enc_irq, Pin.IRQ_FALLING)
def beep(on, freq=2000):
    if on: buzz.freq(freq); buzz.duty_u16(20000)
    else:  buzz.duty_u16(0)

# --------------------------- Serial config API ---------------------------
def handle_cmd(rtc, line):
    if not line: return
    k, rest = line[0], line[1:]
    try:
        if   k == 'Y':                                   # full datetime sync
            yy, mo, dd, dw, hh, mm, ss = (int(x) for x in rest.split(','))
            rtc.set(hh, mm, ss, dw, dd, mo, yy)
        elif k == 'T':                                   # time only
            hh, mm, ss = (int(x) for x in rest.split(':')); rtc.set(hh, mm, ss)
        elif k == 'M': S.messages.append(rest)
        elif k == 'A':
            hh, mm = rest.split(':'); S.alarms.append((int(hh), int(mm)))
        elif k == 'G':
            t, _, lbl = rest.partition('|'); hh, mm = t.split(':')
            S.meetings.append((int(hh), int(mm), lbl))
        elif k == 'C': S.meetings.clear()                # clear for re-sync
    except Exception:
        pass

def next_alarm():
    """The alarm that will fire soonest from the current time, wrapping past
    midnight. Alarms are daily (HH:MM only), so this is what the user cares
    about -- and it always reflects an alarm you just added for later today."""
    if not S.alarms: return None
    now = S.h * 60 + S.m
    return min(S.alarms, key=lambda a: (a[0] * 60 + a[1] - now) % (24 * 60))

def fmt_timeout(sec):
    if sec == 0:  return "Until dismissed"
    if sec < 60:  return "%d s" % sec
    if sec % 60:  return "%d m %d s" % (sec // 60, sec % 60)
    return "%d min" % (sec // 60)

def build_ticker():
    parts = []
    c = ticker_clock_str()
    if c: parts.append(c)
    if S.meetings:
        nh, nm, lbl = min(S.meetings, key=lambda x: (x[0], x[1]))
        parts.append("%s %02d:%02d" % (lbl or "MTG", nh, nm))
    parts += S.messages
    return "   ".join(parts) if parts else ""

# ------------------------------ Async tasks ------------------------------
async def clock_task(rtc):
    while True:
        S.h, S.m, S.s, S.dow, S.dd, S.mo, S.yy = rtc.full()
        await asyncio.sleep_ms(250)

async def alarm_task():
    # Latch the alarm on when the clock first hits a matching minute, and leave
    # it ringing until the user dismisses it (any key -> S.alarm_firing = False).
    # `fired_key` keeps it from re-triggering later in the same minute after a
    # dismissal, while still allowing the same time to fire again tomorrow.
    fired_key = None
    while True:
        now_key = (S.h, S.m)
        match   = any(a[0] == S.h and a[1] == S.m for a in S.alarms)
        if match and S.s < 2 and fired_key != now_key:
            S.alarm_firing = True
            S.alarm_start = time.ticks_ms()
            fired_key = now_key
        elif not match:
            fired_key = None
        # Auto-stop after the configured timeout (0 = ring until dismissed).
        # fired_key still guards against re-firing within the same minute.
        if S.alarm_firing and S.alarm_timeout and \
           time.ticks_diff(time.ticks_ms(), S.alarm_start) >= S.alarm_timeout * 1000:
            S.alarm_firing = False
        await asyncio.sleep_ms(400)

async def serial_task(rtc):
    poll = select.poll(); poll.register(sys.stdin, select.POLLIN)
    while True:
        if poll.poll(0):
            handle_cmd(rtc, sys.stdin.readline().strip())
        await asyncio.sleep_ms(50)

# ------------------------------- UI logic --------------------------------
def _edge(pin, key, _last={}):
    v = pin.value()
    fell = (_last.get(key, 1) == 1 and v == 0)
    _last[key] = v
    return fell

LONG_PRESS_MS = 600
def _press_event(pin, key, _st={}):
    """Returns 'short' on release before the long-press threshold, or 'long'
    once held past it (fires immediately, while still held, for snappy UI)."""
    v = pin.value()
    st = _st.setdefault(key, {'was': 1, 'start': 0, 'longed': False})
    now = time.ticks_ms()
    ev = None
    if st['was'] == 1 and v == 0:                       # just pressed
        st['start'] = now; st['longed'] = False
    elif st['was'] == 0 and v == 0 and not st['longed']:  # held
        if time.ticks_diff(now, st['start']) >= LONG_PRESS_MS:
            st['longed'] = True; ev = 'long'
    elif st['was'] == 0 and v == 1:                     # released
        ev = None if st['longed'] else 'short'
    st['was'] = v
    return ev
def start_edit(target, *values):
    S.edit = list(values) if values else [0] * len(EDIT_SPECS[target])
    S.edit_field = 0; S.edit_target = target; S.ui = 'edit'
def start_type(target):
    S.buf = ""; S.pick = 0; S.type_target = target; S.ui = 'type'
def start_preset():
    S.preset_idx = 0; S.ui = 'preset'

def _finish_edit(rtc):
    if S.edit_target == 'time':
        h, m = S.edit
        rtc.set(h, m, 0, S.dow, S.dd, S.mo, S.yy)
    elif S.edit_target == 'date':
        dd, mo, yy = S.edit
        rtc.set(S.h, S.m, S.s, day_of_week(yy, mo, dd), dd, mo, yy)
    elif S.edit_target == 'alarm':
        h, m = S.edit
        S.alarms.append((h, m)); save_settings()
    elif S.edit_target == 'mtg':
        h, m = S.edit
        S.pending = (h, m); start_type('mtg_label'); return
    S.ui = 'dash'

def _finish_type():
    if S.type_target == 'msg' and S.buf:
        S.messages.append(S.buf); save_settings()
    elif S.type_target == 'mtg_label':
        h, m = S.pending; S.meetings.append((h, m, S.buf))
    S.ui = 'dash'

def ui_select(rtc):
    if S.ui == 'menu':
        it = MENU_IDS[S.menu_idx]
        if   it == "time":  start_edit('time', S.h, S.m)
        elif it == "date":  start_edit('date', S.dd, S.mo, S.yy)
        elif it == "alarm": start_edit('alarm')
        elif it == "mtg":   start_edit('mtg')
        elif it == "msg":   start_type('msg')
        elif it == "preset": start_preset()
        elif it == "bright": S.ui = 'bright'
        elif it == "tclock": S.ui = 'tclock'
        elif it == "atimeout": S.ui = 'atimeout'
        elif it == "exit":  S.ui = 'dash'
    elif S.ui == 'edit':
        if S.edit_field < len(EDIT_SPECS[S.edit_target]) - 1:
            S.edit_field += 1
        else:
            _finish_edit(rtc)
    elif S.ui == 'type':
        ch = PICK[S.pick]
        if   ch == '<': S.buf = S.buf[:-1]
        elif ch == '>': _finish_type()
        else: S.buf += ch
    elif S.ui == 'preset':
        S.messages.append(PRESETS[S.preset_idx]); save_settings(); S.ui = 'dash'
    elif S.ui == 'bright': save_settings(); S.ui = 'dash'
    elif S.ui == 'tclock': save_settings(); S.ui = 'dash'
    elif S.ui == 'atimeout': save_settings(); S.ui = 'dash'
    elif S.ui == 'dash':   S.ui = 'menu'

def ui_done(rtc):
    """Long-press on the encoder button: jump straight to 'done' for the
    current screen, without needing to scroll to '>' or step through fields."""
    if   S.ui == 'edit':          _finish_edit(rtc)
    elif S.ui == 'type':          _finish_type()
    elif S.ui == 'preset':        S.messages.append(PRESETS[S.preset_idx]); save_settings(); S.ui = 'dash'
    elif S.ui in ('bright', 'tclock', 'atimeout'): save_settings(); S.ui = 'dash'
    elif S.ui == 'menu':          S.ui = 'dash'

def ui_back():
    if   S.ui in ('menu', 'bright', 'tclock', 'atimeout'): S.ui = 'dash'
    elif S.ui == 'preset': S.ui = 'menu'
    elif S.ui == 'edit':
        if S.edit_field > 0: S.edit_field -= 1
        else: S.ui = 'menu'
    elif S.ui == 'type':
        if S.buf: S.buf = S.buf[:-1]
        else: S.ui = 'menu'
    else: S.ui = 'dash'

def ui_rotate(step):
    if S.alarm_firing: S.alarm_firing = False; return
    if   S.ui == 'menu': S.menu_idx = (S.menu_idx + step) % len(MENU_IDS)
    elif S.ui == 'edit':
        i = S.edit_field; lo, hi = EDIT_SPECS[S.edit_target][i]
        S.edit[i] = lo + (S.edit[i] - lo + step) % (hi - lo + 1)
    elif S.ui == 'type': S.pick = (S.pick + step) % len(PICK)
    elif S.ui == 'preset': S.preset_idx = (S.preset_idx + step) % len(PRESETS)
    elif S.ui == 'bright': S.brightness = max(0, min(15, S.brightness + step))
    elif S.ui == 'tclock': S.ticker_clock = (S.ticker_clock + step) % len(TICKER_CLOCK_MODES)
    elif S.ui == 'atimeout':
        S.alarm_timeout = max(0, min(ALARM_TIMEOUT_MAX,
                                     S.alarm_timeout + step * ALARM_TIMEOUT_STEP))

async def ui_task(rtc):
    global enc_delta
    while True:
        d = enc_delta; enc_delta = 0
        back = _edge(btn, 'bk')
        if d: ui_rotate(1 if d > 0 else -1)
        ev = _press_event(enc_sw, 'sw')
        if ev == 'short':
            if S.alarm_firing: S.alarm_firing = False
            else: ui_select(rtc)
        elif ev == 'long':
            if S.alarm_firing: S.alarm_firing = False
            else: ui_done(rtc)
        if back:
            if S.alarm_firing: S.alarm_firing = False
            else: ui_back()
        if d or ev or back:
            S.last_input = time.ticks_ms()
            S.blip_until = time.ticks_add(S.last_input, BLIP_MS)  # audible tick
        await asyncio.sleep_ms(30)

# --------------------------- OLED UI rendering ---------------------------
def _center_x(text, cw=8):
    return max(0, (OLED_W - len(text) * cw) // 2)

def oled_title(o, text):
    o.text(text, _center_x(text), 0)
    o.hline(0, 10, OLED_W, 1)

def _glyph_w(scale): return 5 * scale          # 4px glyph + 1px spacing, scaled
def _big_w(s, scale): return len(s) * _glyph_w(scale) - scale

def draw_big_glyph(o, ch, x, y, scale):
    g = FONT.get(ch)
    if not g: return
    for r in range(7):
        for c in range(4):
            if g[r] & (1 << (3 - c)):
                o.fill_rect(x + c * scale, y + r * scale, scale, scale, 1)

def draw_big_text(o, s, y, scale, x=None):
    if x is None: x = (OLED_W - _big_w(s, scale)) // 2
    for ch in s:
        draw_big_glyph(o, ch, x, y, scale)
        x += _glyph_w(scale)

def oled_dash(o):
    date_s = "%s %02d %s '%02d" % (DOW[S.dow] if 0 <= S.dow < 8 else "",
             S.dd, MON[S.mo] if 0 < S.mo < 13 else "", S.yy)
    o.text(date_s, _center_x(date_s), 0)
    o.hline(0, 9, OLED_W, 1)
    draw_big_text(o, "%02d%c%02d" % (S.h, ':' if S.s % 2 == 0 else ' ', S.m), 12, 3)
    o.hline(0, 35, OLED_W, 1)
    if S.meetings:
        nh, nm, lbl = min(S.meetings, key=lambda x: (x[0], x[1]))
        o.text("Next: %02d:%02d %s" % (nh, nm, lbl[:10]), 0, 38)
    else:
        o.text("No meetings", 0, 38)
    nxt = next_alarm()
    if nxt:
        ah, am = nxt
        more = (" +%d" % (len(S.alarms) - 1)) if len(S.alarms) > 1 else ""
        o.text("Alarm: %02d:%02d%s" % (ah, am, more), 0, 47)
    else:
        o.text("No alarm set", 0, 47)
    o.text("[press] menu", _center_x("[press] menu"), 56)

def oled_menu(o):
    oled_title(o, "MENU")
    top = max(0, min(S.menu_idx - 2, len(MENU_LABELS) - 5))
    for i in range(min(5, len(MENU_LABELS) - top)):
        idx = top + i
        cur = ">" if idx == S.menu_idx else " "
        o.text("%s %s" % (cur, MENU_LABELS[idx]), 0, 16 + i * 9)

def oled_edit(o, blink):
    title = {"time": "Set Time", "date": "Set Date", "alarm": "Add Alarm",
             "mtg": "Add Meeting"}[S.edit_target]
    oled_title(o, title)
    if S.edit_target == 'date':
        dd = "  " if (S.edit_field == 0 and not blink) else "%02d" % S.edit[0]
        mo = "  " if (S.edit_field == 1 and not blink) else "%02d" % S.edit[1]
        yy = "  " if (S.edit_field == 2 and not blink) else "%02d" % S.edit[2]
        o.text("%s / %s / %s" % (dd, mo, yy), 22, 26)
    else:
        hh = "  " if (S.edit_field == 0 and not blink) else "%02d" % S.edit[0]
        mm = "  " if (S.edit_field == 1 and not blink) else "%02d" % S.edit[1]
        o.text("%s : %s" % (hh, mm), 34, 26)
    o.text("press>  hold=done", 0, 56)

def oled_type(o):
    oled_title(o, "Message" if S.type_target == "msg" else "Mtg label")
    o.text(">" + S.buf[-15:], 0, 16)
    for d in range(-3, 4):                          # picker window
        ch = PICK[(S.pick + d) % len(PICK)]
        o.text(ch, 32 + (d + 3) * 8, 38)
    o.text("^", 56, 46)
    o.text("<bksp >/hold=done", 0, 56)

def oled_preset(o):
    oled_title(o, "Quick Message")
    top = max(0, min(S.preset_idx - 2, len(PRESETS) - 5))
    for i in range(min(5, len(PRESETS) - top)):
        idx = top + i
        cur = ">" if idx == S.preset_idx else " "
        o.text("%s %s" % (cur, PRESETS[idx][:19]), 0, 16 + i * 9)

def oled_bright(o):
    oled_title(o, "Brightness")
    o.text("%d / 15" % S.brightness, 0, 20)
    o.rect(0, 36, 122, 8, 1)
    o.fill_rect(1, 37, int(S.brightness / 15 * 120), 6, 1)
    o.text("rotate  press>", 0, 56)

def oled_tclock(o):
    oled_title(o, "Ticker Clock")
    mode = TICKER_CLOCK_MODES[S.ticker_clock]
    o.text(mode, _center_x(mode), 22)
    preview = ticker_clock_str() or "(none)"
    o.text(preview, _center_x(preview), 36)
    o.text("rotate  press>", 0, 56)

def oled_atimeout(o):
    oled_title(o, "Alarm Timeout")
    val = fmt_timeout(S.alarm_timeout)
    o.text(val, _center_x(val), 22)
    o.text("0 = ring until off", 0, 40)
    o.text("rotate  press>", 0, 56)

def oled_alarm(o, blink):
    if blink: o.fill(1); return
    t1, t2 = "*** ALARM ***", "any key = stop"
    o.text(t1, _center_x(t1), 20)
    o.text(t2, _center_x(t2), 40)

# ------------------------------- Renderers -------------------------------
async def render_matrix(disp):
    while True:
        disp.brightness(S.brightness)
        disp.fb.fill(0)
        blink = (time.ticks_ms() // 300) % 2
        if S.alarm_firing:
            if blink: disp.fb.fill(1)
            beep(blink)
        else:
            blipping = time.ticks_diff(S.blip_until, time.ticks_ms()) > 0
            beep(blipping, BLIP_FREQ)
            draw_hhmm(disp.fb, S.h, S.m, (S.s % 2) == 0)
            t = build_ticker()
            if t:
                disp.fb.text(t, S.scroll_x, 8)
                S.scroll_x -= 1
                if S.scroll_x < -8 * len(t): S.scroll_x = W
            else:
                S.scroll_x = W
        disp.show()
        await asyncio.sleep_ms(33)

async def render_oled(oled):
    while True:
        idle = time.ticks_diff(time.ticks_ms(), S.last_input)
        # Only the idle dash screen blanks -- an active menu/edit/alarm never sleeps.
        should_be_on = S.ui != 'dash' or S.alarm_firing or idle < OLED_SAVER_MS
        if should_be_on != S.oled_on:
            oled.poweron() if should_be_on else oled.poweroff()
            S.oled_on = should_be_on
        if S.oled_on:
            oled.fill(0)
            blink = (time.ticks_ms() // 350) % 2
            if   S.alarm_firing: oled_alarm(oled, blink)
            elif S.ui == 'dash': oled_dash(oled)
            elif S.ui == 'menu': oled_menu(oled)
            elif S.ui == 'edit': oled_edit(oled, blink)
            elif S.ui == 'type': oled_type(oled)
            elif S.ui == 'preset': oled_preset(oled)
            elif S.ui == 'bright': oled_bright(oled)
            elif S.ui == 'tclock': oled_tclock(oled)
            elif S.ui == 'atimeout': oled_atimeout(oled)
            oled.show()
        await asyncio.sleep_ms(60)

# --------------------------------- Main ----------------------------------
async def main():
    spi  = SPI(SPI_ID, baudrate=10_000_000, polarity=0, phase=0,
               sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
    i2c  = I2C(I2C_ID, sda=Pin(PIN_SDA), scl=Pin(PIN_SCL), freq=400_000)
    disp = Matrix(spi, PIN_CS)
    oled = SSD1306_I2C(OLED_W, OLED_H, i2c)
    rtc  = DS3231(i2c)
    load_settings()
    await asyncio.gather(
        clock_task(rtc), alarm_task(), ui_task(rtc), serial_task(rtc),
        render_matrix(disp), render_oled(oled),
    )

if __name__ == "__main__":
    asyncio.run(main())
