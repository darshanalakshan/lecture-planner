"""
Evening mailer: reads your class calendar (ICS), plans tomorrow's trip with
ResRobot (Trafiklab), and emails you the plan + a one-tap Samsung alarm link.

pip install requests icalendar recurring-ical-events

Environment variables (use GitHub Actions secrets or a .env you export):
  ICS_URL          your class calendar feed URL
  RESROBOT_KEY     Trafiklab ResRobot API key
  ORIGIN_ID        ResRobot stop id for home  (find with the lookup helper below)
  DEST_ID          ResRobot stop id for campus
  SMTP_USER        e.g. yourname@gmail.com
  SMTP_PASS        Gmail app password (not your normal password)
  EMAIL_TO         where to send the plan
  ALARM_PAGE_URL   public URL of alarm.html (e.g. GitHub Pages)
Optional: WALK_MIN (default 8), READY_MIN (default 40), ARRIVE_BUFFER_MIN (default 10)
"""
import os, smtplib, ssl, sys
from datetime import datetime, timedelta, date
from email.message import EmailMessage
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import recurring_ical_events
from icalendar import Calendar

TZ = ZoneInfo("Europe/Stockholm")
WALK_MIN = int(os.getenv("WALK_MIN", 8))        # home -> stop
READY_MIN = int(os.getenv("READY_MIN", 40))     # wake up -> out the door
BUFFER_MIN = int(os.getenv("ARRIVE_BUFFER_MIN", 10))  # arrive this early


def tomorrows_events(ics_url):
    cal = Calendar.from_ical(requests.get(ics_url, timeout=30).content)
    d = date.today() + timedelta(days=1)
    start = datetime.combine(d, datetime.min.time(), TZ)
    events = recurring_ical_events.of(cal).between(start, start + timedelta(days=1))
    out = []
    for e in events:
        if str(e.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = e.decoded("DTSTART")
        if not isinstance(s, datetime):      # skip all-day items
            continue
        s = s.astimezone(TZ)
        en = e.decoded("DTEND").astimezone(TZ) if e.get("DTEND") else s
        out.append((s, en, str(e.get("SUMMARY", "Lecture")), str(e.get("LOCATION", ""))))
    return sorted(out)


def find_stop(name):
    """Helper: python plan_mailer.py find 'Trollhättan C' -> prints stop ids."""
    r = requests.get("https://api.resrobot.se/v2.1/location.name", params={
        "input": name, "format": "json", "accessId": os.environ["RESROBOT_KEY"]}, timeout=30)
    for s in r.json().get("stopLocationOrCoordLocation", []):
        sl = s.get("StopLocation")
        if sl:
            print(sl["extId"], sl["name"])


def plan_trip(arrive_by):
    """Latest trip that arrives at DEST before arrive_by."""
    r = requests.get("https://api.resrobot.se/v2.1/trip", params={
        "originId": os.environ["ORIGIN_ID"], "destId": os.environ["DEST_ID"],
        "date": arrive_by.strftime("%Y-%m-%d"), "time": arrive_by.strftime("%H:%M"),
        "searchForArrival": 1, "numF": 0, "numB": 5,
        "format": "json", "accessId": os.environ["RESROBOT_KEY"]}, timeout=30)
    r.raise_for_status()
    best = None
    for t in r.json().get("Trip", []):
        legs = t["LegList"]["Leg"]
        dep = datetime.fromisoformat(f'{legs[0]["Origin"]["date"]}T{legs[0]["Origin"]["time"]}').replace(tzinfo=TZ)
        arr = datetime.fromisoformat(f'{legs[-1]["Destination"]["date"]}T{legs[-1]["Destination"]["time"]}').replace(tzinfo=TZ)
        if arr <= arrive_by and (best is None or dep > best[0]):
            best = (dep, arr, legs)
    return best


def describe_legs(legs):
    lines = []
    for l in legs:
        if l.get("type") == "WALK":
            continue
        o, d = l["Origin"], l["Destination"]
        lines.append(f'{o["time"][:5]} {o["name"]} → {d["time"][:5]} {d["name"]} ({l.get("name", "").strip()})')
    return lines


def build_and_send():
    events = tomorrows_events(os.environ["ICS_URL"])
    tomorrow = date.today() + timedelta(days=1)
    if not events:
        print("No lectures tomorrow, no email sent.")
        return
    first_start, _, title, room = events[0]
    arrive_by = first_start - timedelta(minutes=BUFFER_MIN)
    trip = plan_trip(arrive_by)

    sched = "\n".join(f'  {s:%H:%M}-{e:%H:%M}  {t} {("(" + r + ")") if r else ""}' for s, e, t, r in events)
    if trip:
        dep, arr, legs = trip
        out_door = dep - timedelta(minutes=WALK_MIN)
        alarm = out_door - timedelta(minutes=READY_MIN)
        link = f'{os.environ["ALARM_PAGE_URL"]}?h={alarm.hour}&m={alarm.minute}&msg={quote("Lecture day: " + title)}'
        text = (f"Plan for {tomorrow:%A %d %b}\n\n"
                f"Schedule:\n{sched}\n\n"
                f"Alarm: {alarm:%H:%M}\nLeave home: {out_door:%H:%M}\n"
                f"Depart: {dep:%H:%M}, arrive {arr:%H:%M} (first lecture {first_start:%H:%M})\n\n"
                "Route:\n  " + "\n  ".join(describe_legs(legs)) +
                f"\n\nSet alarm on your phone (open in Chrome):\n{link}\n")
        subject = f"Tomorrow: alarm {alarm:%H:%M}, first lecture {first_start:%H:%M}"
    else:
        text = f"Schedule:\n{sched}\n\nNo trip found that arrives in time. Check manually."
        subject = f"Tomorrow: first lecture {first_start:%H:%M} (no trip found)"

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_USER"], os.environ["EMAIL_TO"], subject
    msg.set_content(text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    print("Sent:", subject)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "find":
        find_stop(sys.argv[2])
    else:
        build_and_send()
