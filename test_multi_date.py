from dataclasses import replace
from datetime import date, datetime
import io
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import boat_notifier as notifier
import check_boats as boat
from test_check_boats import event

NOW = datetime(2026, 11, 7, 12, tzinfo=boat.JAPAN)


class MultiDateTests(unittest.TestCase):
    def load(self, values=None):
        with patch.dict(os.environ, values or {}, clear=True):
            return notifier.load_config(require_telegram=False)

    def test_defaults_and_independent_state_files(self):
        config = self.load()
        first, second = config.daily_configs()
        self.assertEqual([first.day, second.day], [date(2026, 11, 20), date(2026, 11, 21)])
        self.assertEqual(first.state_file, config.state_file)
        self.assertEqual(second.state_file.name, 'notifications-2026-11-21.json')
        self.assertNotEqual(first.scope, second.scope)
        self.assertEqual(second.deadline.isoformat(), '2026-11-19T09:00:00+09:00')
        self.assertEqual((first.earliest, first.latest), (second.earliest, second.latest))

    def test_explicit_dates_override_legacy_date_and_deduplicate(self):
        config = self.load({'BOAT_DATE': '2026-11-19',
                            'BOAT_DATES': '2026-11-20, 2026-11-21,2026-11-20'})
        self.assertEqual([c.day.day for c in config.daily_configs()], [20, 21])
        legacy = self.load({'BOAT_DATE': '2026-11-22'})
        self.assertEqual([c.day.day for c in legacy.daily_configs()], [22])

    def test_bad_date_lists_fail_before_checks(self):
        for value in ('', '2026-11-20,', '2026-11-20,not-a-date'):
            with self.subTest(value=value), self.assertRaises(notifier.ConfigError):
                self.load({'BOAT_DATES': value})

    def test_each_day_alerts_once_and_publishes_every_check(self):
        def calendar(day, timeout):
            return {'errors': [], 'results': [event(
                f'{day}T13:00:00+09:00', f'{day}T13:30:00+09:00')]}

        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.load(), state_file=Path(directory) / 'notifications.json')
            with patch.object(boat, 'fetch_calendar', side_effect=calendar), \
                    patch.object(notifier, 'publish_result') as publish, \
                    patch.object(notifier, 'send_message', return_value=123) as send:
                for _ in range(3):
                    for daily in config.daily_configs():
                        notifier.check_and_notify(daily, now=NOW)
                self.assertEqual(send.call_count, 2)
                self.assertEqual([call.args[0].day.day for call in send.call_args_list], [20, 21])
                self.assertEqual(publish.call_count, 6)
                self.assertEqual([call.args[1]['date'] for call in publish.call_args_list],
                                 ['2026-11-20', '2026-11-21'] * 3)

    def test_main_continues_second_date_after_first_failure_and_reports_failure(self):
        for error in (boat.CheckError('offline'), notifier.TelegramError('send failed')):
            with self.subTest(error=error), \
                    patch.object(notifier, 'load_config', return_value=self.load()), \
                    patch.object(notifier, 'check_and_notify', side_effect=[error, None]) as check:
                self.assertEqual(notifier.main([]), 2)
                self.assertEqual([call.args[0].day.day for call in check.call_args_list], [20, 21])

    def test_main_dry_run_checks_both_dates(self):
        with patch.object(notifier, 'load_config', return_value=self.load()), \
                patch.object(notifier, 'check_and_notify') as check:
            self.assertEqual(notifier.main(['--dry-run']), 0)
            self.assertEqual(check.call_count, 2)
            self.assertTrue(all(call.kwargs['dry_run'] for call in check.call_args_list))

    def test_first_cutoff_does_not_stop_second_date(self):
        first, second = self.load().daily_configs()
        with patch.object(boat, 'fetch_calendar', return_value={'errors': [], 'results': [
                event('2026-11-21T13:00:00+09:00', '2026-11-21T13:30:00+09:00')]}) as fetch, \
                contextlib.redirect_stdout(io.StringIO()):
            for daily in (first, second):
                notifier.check_and_notify(daily, dry_run=True, now=first.deadline)
            fetch.assert_called_once_with(second.day, second.timeout)


if __name__ == '__main__':
    unittest.main()
