import contextlib
from datetime import date, time
import io
import json
import unittest
from unittest.mock import patch

import check_boats as boat


DAY = date(2026, 11, 20)


def event(start="2026-11-20T13:00:00+09:00", end="2026-11-20T13:30:00+09:00", **changes):
    result = dict(start=start, end=end, ordable=True, order_remain_amount="1",
                  is_reserve_started=True, is_reserve_dead=False, is_full=False,
                  enabled_flg=True, reserve_possible=True, is_accept=True)
    result.update(changes)
    return result


def parse(*events):
    return boat.parse_calendar({"errors": [], "results": list(events)}, DAY, time(13), time(16))


class CalendarTests(unittest.TestCase):
    def test_positive_inventory_and_explicit_bookable_flag(self):
        self.assertTrue(parse(event())[0]["available"])
        self.assertFalse(parse(event(ordable=False))[0]["available"])

    def test_sold_out_not_open_and_closed_are_distinct(self):
        cases = [
            ({"is_full": True, "order_remain_amount": "0"}, "SOLD_OUT"),
            ({"is_reserve_started": False}, "NOT_OPEN_YET"),
            ({"is_reserve_dead": True}, "BOOKING_CLOSED"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(parse(event(ordable=False, **changes))[0]["status"], expected)

    def test_start_bounds_inclusive_and_wrong_day_excluded(self):
        slots = parse(
            event("2026-11-20T12:30:00+09:00", "2026-11-20T13:00:00+09:00"),
            event(),
            event("2026-11-20T16:00:00+09:00", "2026-11-20T16:30:00+09:00"),
            event("2026-11-20T16:30:00+09:00", "2026-11-20T17:00:00+09:00"),
            event("2026-11-21T13:00:00+09:00", "2026-11-21T13:30:00+09:00"),
        )
        self.assertEqual([s["start"][11:16] for s in slots], ["13:00", "16:00"])

    def test_timezone_conversion(self):
        self.assertEqual(parse(event("2026-11-20T04:00:00+00:00",
                                     "2026-11-20T04:30:00+00:00"))[0]["start"],
                         "2026-11-20T13:00:00+09:00")

    def test_schema_changes_and_contradictions_fail_closed(self):
        for changes in ({"ordable": "false"}, {"ordable": None},
                        {"order_remain_amount": None}, {"order_remain_amount": True},
                        {"order_remain_amount": "0"}, {"is_full": True},
                        {"start": "2026-11-20T13:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(boat.CheckError):
                parse(event(**changes))

    def test_error_or_missing_results_is_not_sold_out(self):
        for payload in ({}, {"errors": ["Failed"], "results": []},
                        {"errors": [], "results": "unexpected"}):
            with self.subTest(payload=payload), self.assertRaises(boat.CheckError):
                boat.make_report(payload, DAY, time(13), time(16))
        report = boat.make_report({"errors": [], "results": []}, DAY, time(13), time(16))
        self.assertEqual(report["status"], "NO_MATCHING_SLOTS")

    def test_cli_exit_codes(self):
        cases = [([event()], 0), ([event(ordable=False)], 1), ([], 2)]
        for events, code in cases:
            with patch.object(boat, "fetch_calendar", return_value={"errors": [], "results": events}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(boat.main([]), code)

    def test_cli_defaults_include_all_times_but_only_requested_date(self):
        events = [event('2026-11-20T08:30:00+09:00', '2026-11-20T09:00:00+09:00'),
                  event('2026-11-20T16:30:00+09:00', '2026-11-20T17:00:00+09:00'),
                  event('2026-11-21T10:00:00+09:00', '2026-11-21T10:30:00+09:00')]
        output = io.StringIO()
        with patch.object(boat, 'fetch_calendar', return_value={'errors': [], 'results': events}), \
                contextlib.redirect_stdout(output):
            self.assertEqual(boat.main(['--json']), 0)
        report = json.loads(output.getvalue())
        self.assertEqual((report['start_time_from'], report['start_time_to']), ('00:00', '23:59'))
        self.assertEqual([slot['start'][11:16] for slot in report['slots']], ['08:30', '16:30'])

    def test_watch_recovers_from_error_and_stops_on_availability(self):
        responses = [boat.CheckError("Network unavailable"),
                     {"errors": [], "results": [event(ordable=False)]},
                     {"errors": [], "results": [event()]}]
        with patch.object(boat, "fetch_calendar", side_effect=responses), \
                patch.object(boat.clock, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(boat.main(["--watch", "--interval", "60"]), 0)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [120, 60])


if __name__ == "__main__":
    unittest.main()
