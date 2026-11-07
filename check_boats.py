#!/usr/bin/env python3
"""Check Takachiho boat availability using the public calendar's day-view search."""

import argparse
from datetime import date, datetime, time, timedelta
import json
import math
import sys
import time as clock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

JAPAN = ZoneInfo("Asia/Tokyo")
CALENDAR_URL = "https://eipro.jp/takachiho1/eventCalendars/index"
SEARCH_URL = "https://eipro.jp/takachiho1/eventCalendars/search"


class CheckError(Exception):
    """The check failed; availability must not be inferred from this result."""


def fetch_calendar(day, timeout=30):
    # These are the same form fields used by app.page.EventCalendars.js.
    body = urlencode({
        "data[conds][ServiceView][max_session_dateOver]": day.strftime("%Y/%m/%d"),
        "data[conds][ServiceView][min_session_dateUnder]":
            (day + timedelta(days=1)).strftime("%Y/%m/%d"),
        "calendar_view_name": "agendaDay",
        "calendar_type": "day",
    }).encode("ascii")
    request = Request(SEARCH_URL, data=body, headers={
        "User-Agent": "TakachihoAvailabilityChecker/1.0 (Python)",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": CALENDAR_URL,
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            # Refuse a redirect/login page or unexpectedly huge response.
            if response.geturl() != SEARCH_URL:
                raise CheckError("The search endpoint redirected; inspect the website.")
            raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise CheckError("Unexpectedly large calendar response.")
            return json.loads(raw)
    except HTTPError as exc:
        raise CheckError(f"Calendar returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CheckError(f"Network request failed: {exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise CheckError("Expected calendar JSON; received an invalid response.") from exc


def flag(event, name):
    value = event.get(name)
    if type(value) is not bool:
        raise CheckError(f"Missing or changed calendar field: {name}.")
    return value


def event_datetime(event, name):
    try:
        value = datetime.fromisoformat(event[name])
        if value.tzinfo is None:
            raise ValueError("Missing timezone")
        return value.astimezone(JAPAN)
    except (KeyError, ValueError, TypeError) as exc:
        raise CheckError(f"Missing or invalid event timestamp: {name}.") from exc


def parse_calendar(payload, day, earliest, latest):
    if not isinstance(payload, dict) or "errors" not in payload:
        raise CheckError("Unexpected calendar response structure.")
    if payload["errors"] != []:
        raise CheckError(f"Calendar reported errors: {payload['errors']!r}")
    events = payload.get("results")
    if not isinstance(events, list):
        raise CheckError("Calendar response has no results list.")

    slots = []
    for event in events:
        if not isinstance(event, dict):
            raise CheckError("Invalid calendar event.")
        start = event_datetime(event, "start")
        end = event_datetime(event, "end")
        if end <= start:
            raise CheckError("Calendar event has an invalid duration.")
        if start.date() != day or not earliest <= start.time() <= latest:
            continue

        # Require explicit machine-readable flags; never infer availability from
        # the rendered HTML title, color, capacity, or cancellation count.
        flags = {name: flag(event, name) for name in (
            "ordable", "is_reserve_started", "is_reserve_dead", "is_full",
            "enabled_flg", "reserve_possible", "is_accept",
        )}
        remaining = event.get("order_remain_amount")
        if type(remaining) not in (str, int) or not str(remaining).isascii() \
                or not str(remaining).removeprefix("-").isdigit():
            raise CheckError("Missing or invalid order_remain_amount.")
        remaining = int(remaining)
        if remaining < 0:
            # The live calendar can return -1 for a full, unbookable slot.
            # Only those explicit flags justify treating negative inventory as zero.
            if flags["ordable"] or not flags["is_full"]:
                raise CheckError("Negative inventory without explicit sold-out flags.")
            remaining = 0
        available = (
            flags["ordable"] and flags["is_reserve_started"]
            and not flags["is_reserve_dead"] and not flags["is_full"]
            and flags["enabled_flg"] and flags["reserve_possible"]
            and flags["is_accept"] and remaining > 0
        )
        if flags["ordable"] and not available:
            raise CheckError("Conflicting availability flags; inspect the calendar.")
        if available:
            status = "AVAILABLE"
        elif not flags["is_reserve_started"]:
            status = "NOT_OPEN_YET"
        elif flags["is_reserve_dead"]:
            status = "BOOKING_CLOSED"
        elif flags["is_full"]:
            status = "SOLD_OUT"
        else:
            status = "UNAVAILABLE"
        slots.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "status": status,
            "available": available,
            "remaining": remaining,
        })
    return sorted(slots, key=lambda slot: slot["start"])


def make_report(payload, day, earliest, latest):
    slots = parse_calendar(payload, day, earliest, latest)
    return {
        "checked_at": datetime.now(JAPAN).isoformat(timespec="seconds"),
        "timezone": "Asia/Tokyo",
        "date": day.isoformat(),
        "start_time_from": earliest.isoformat(timespec="minutes"),
        "start_time_to": latest.isoformat(timespec="minutes"),
        "status": ("AVAILABLE" if any(s["available"] for s in slots)
                   else "NO_AVAILABILITY" if slots else "NO_MATCHING_SLOTS"),
        "slots": slots,
        "calendar_url": CALENDAR_URL,
    }


def display(report, as_json):
    if as_json:
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return
    print(f"\nChecked {report['checked_at']} (Japan time)")
    print(f"{report['date']} — start times {report['start_time_from']}"
          f"–{report['start_time_to']}, inclusive")
    for slot in report["slots"]:
        print(f"  {slot['start'][11:16]}–{slot['end'][11:16]}  "
              f"{slot['status']:<16} remaining: {slot['remaining']}")
    if report["status"] == "AVAILABLE":
        print(f"Available slot found! Book manually: {CALENDAR_URL}")
    elif report["status"] == "NO_MATCHING_SLOTS":
        print("No matching slots were returned. This does not establish sold-out status.")
    else:
        print("No bookable slots in this window right now.")
    sys.stdout.flush()


def parse_time(value):
    try:
        result = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use HH:MM, e.g. 13:00.") from exc
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 11, 20))
    parser.add_argument("--from", dest="earliest", type=parse_time, default=time(0))
    parser.add_argument("--to", dest="latest", type=parse_time, default=time(23, 59),
                        help="Latest allowed START time, inclusive (default: 23:59).")
    parser.add_argument("--watch", action="store_true",
                        help="Repeat until availability is found; Ctrl+C to stop.")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between checks in watch mode (minimum 60; default 300).")
    parser.add_argument("--timeout", type=float, default=30,
                        help="Network timeout in seconds (default 30).")
    parser.add_argument("--json", action="store_true", help="Output JSON (one object per check).")
    args = parser.parse_args(argv)
    if args.earliest > args.latest:
        parser.error("--from must be earlier than or equal to --to.")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number.")

    failures = 0
    try:
        while True:
            try:
                payload = fetch_calendar(args.date, args.timeout)
                report = make_report(payload, args.date, args.earliest, args.latest)
                display(report, args.json)
                failures = 0
                if report["status"] == "AVAILABLE":
                    if args.watch and not args.json:
                        print("\a", end="", flush=True)
                    return 0
                if not args.watch:
                    return 2 if report["status"] == "NO_MATCHING_SLOTS" else 1
            except CheckError as exc:
                failures += 1
                if args.json:
                    print(json.dumps({"status": "ERROR", "error": str(exc),
                                      "checked_at": datetime.now(JAPAN).isoformat()}), flush=True)
                else:
                    print(f"Check failed: {exc}", file=sys.stderr, flush=True)
                if not args.watch:
                    return 2
            # Back off after failures, without ever polling faster than requested.
            delay = max(args.interval, min(args.interval * 2 ** min(failures, 5), 1800))
            if not args.json:
                print(f"Next check in {delay} seconds. Ctrl+C to stop.", flush=True)
            clock.sleep(delay)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
