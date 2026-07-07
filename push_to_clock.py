#!/usr/bin/env python3
# Copyright (C) 2026 Erico Mendonca <erico.mendonca@gmail.com>
# Licensed under the GNU General Public License v3.0. See LICENSE for details.
"""
push_to_clock.py -- companion for the Pico meeting clock (runs on your PC).

Speaks the tiny USB-serial protocol the Pico understands:
    T<HH:MM:SS>        set the RTC time
    M<text>            push a scrolling message
    A<HH:MM>           add an alarm
    G<HH:MM>|<label>   add a meeting
    C                  clear all meetings (used before a fresh calendar sync)

Install:
    pip install pyserial
    pip install google-api-python-client google-auth-oauthlib   # for `gcal`

Find your serial port:
    Linux/mac -> /dev/ttyACM0 or /dev/tty.usbmodem*   Windows -> COMx

Google Calendar one-time setup:
    1. console.cloud.google.com -> new project -> enable "Google Calendar API"
    2. Create OAuth client ID, type "Desktop app", download as credentials.json
    3. Put credentials.json next to this script
    4. First `gcal` run opens a browser to authorize; a token.json is cached after.

Examples:
    python push_to_clock.py -p /dev/ttyACM0 sync
    python push_to_clock.py -p /dev/ttyACM0 gcal                 # today's events, once
    python push_to_clock.py -p /dev/ttyACM0 gcal --watch 10      # re-sync every 10 min
    python push_to_clock.py -p /dev/ttyACM0 cal meetings.ics     # offline .ics fallback
    python push_to_clock.py -p /dev/ttyACM0 msg "Standup moved to 11"
    python push_to_clock.py -p /dev/ttyACM0 alarm 07:30
"""
import argparse, sys, time, datetime as dt

try:
    import serial  # pyserial
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pyserial")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def send(port, baud, lines):
    with serial.Serial(port, baud, timeout=1) as s:
        time.sleep(2.0)  # allow the Pico USB CDC to come up
        for ln in lines:
            s.write((ln + "\n").encode())
            time.sleep(0.05)
            print("->", ln)


def sync_cmd():
    n = dt.datetime.now()
    return "Y%d,%d,%d,%d,%d,%d,%d" % (n.year % 100, n.month, n.day,
                                      n.isoweekday(), n.hour, n.minute, n.second)


def meeting_cmds(events, sync_first):
    """events = list of (hour, minute, label). Prepend sync + clear."""
    lines = [sync_cmd()] if sync_first else []
    lines.append("C")  # wipe old meetings so re-sync is idempotent
    for h, m, label in sorted(events):
        lines.append("G%02d:%02d|%s" % (h, m, label[:16]))  # short for 32x16
    if not events:
        print("No timed events for today.")
    return lines


# ------------------------------ Google Calendar ------------------------------
def _gcal_service(creds_path, token_path):
    import os.path
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Run:  pip install google-api-python-client google-auth-oauthlib")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def fetch_gcal_today(creds_path, token_path, cal_id):
    svc = _gcal_service(creds_path, token_path)
    start = dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    res = svc.events().list(
        calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime").execute()   # expands recurrences
    out = []
    for e in res.get("items", []):
        s = e["start"].get("dateTime")
        if not s:
            continue  # all-day event, skip
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
        out.append((d.hour, d.minute, e.get("summary", "MTG")))
    return out


# --------------------------------- .ics fallback -----------------------------
def _unfold(raw):
    out = []
    for line in raw.splitlines():
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def fetch_ics_today(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = _unfold(f.read())
    today = dt.date.today()
    start, summary, events = None, "", []
    for ln in lines:
        if ln.startswith("BEGIN:VEVENT"):
            start, summary = None, ""
        elif ln.startswith("SUMMARY"):
            summary = ln.split(":", 1)[-1].strip()
        elif ln.startswith("DTSTART"):
            head, _, val = ln.partition(":"); val = val.strip()
            if "VALUE=DATE" in head and "T" not in val:
                continue
            try:
                d = dt.datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
                if val.endswith("Z"):
                    d = d.replace(tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
                start = d
            except ValueError:
                start = None
        elif ln.startswith("END:VEVENT") and start and start.date() == today:
            events.append((start.hour, start.minute, summary or "MTG"))
    return events


# ------------------------------------ main -----------------------------------
def main():
    ap = argparse.ArgumentParser(description="Push data to the Pico meeting clock over USB.")
    ap.add_argument("-p", "--port", required=True, help="serial port (/dev/ttyACM0, COM3, ...)")
    ap.add_argument("-b", "--baud", type=int, default=115200)
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("sync", help="set the RTC to this computer's clock")

    g = sub.add_parser("gcal", help="push today's meetings from Google Calendar")
    g.add_argument("--watch", type=int, default=0, metavar="MIN",
                   help="re-sync every MIN minutes (0 = once)")
    g.add_argument("--credentials", default="credentials.json")
    g.add_argument("--token", default="token.json")
    g.add_argument("--calendar", default="primary", help="calendar id")

    c = sub.add_parser("cal", help="push today's meetings from an .ics file")
    c.add_argument("ics")

    m = sub.add_parser("msg", help="push a scrolling message");  m.add_argument("text")
    a = sub.add_parser("alarm", help="add an alarm HH:MM");      a.add_argument("time")
    args = ap.parse_args()

    if args.action == "sync":
        send(args.port, args.baud, [sync_cmd()])
    elif args.action == "msg":
        send(args.port, args.baud, ["M" + args.text])
    elif args.action == "alarm":
        dt.datetime.strptime(args.time, "%H:%M")  # validate
        send(args.port, args.baud, ["A" + args.time])
    elif args.action == "cal":
        send(args.port, args.baud, meeting_cmds(fetch_ics_today(args.ics), True))
    elif args.action == "gcal":
        first = True
        while True:
            events = fetch_gcal_today(args.credentials, args.token, args.calendar)
            send(args.port, args.baud, meeting_cmds(events, first))
            first = False
            if not args.watch:
                break
            print("...sleeping %d min" % args.watch)
            time.sleep(args.watch * 60)
    print("Done.")


if __name__ == "__main__":
    main()
