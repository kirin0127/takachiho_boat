from dataclasses import replace
from datetime import date, datetime, time
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
        self.assertEqual((first.earliest, first.latest), (time(0), time(23, 59)))
        self.assertEqual((second.earliest, second.latest), (time(0), time(23, 59)))

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

    def test_full_day_alerts_and_history_include_morning_and_evening_for_both_dates(self):
        def calendar(day, timeout):
            times = [('00:00', '00:30'), ('08:30', '09:00'), ('10:00', '10:30'),
                     ('13:00', '13:30'), ('16:30', '17:00'), ('23:00', '23:30')]
            return {'errors': [], 'results': [
                event(f'{day}T{start}:00+09:00', f'{day}T{end}:00+09:00')
                for start, end in times
            ]}

        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.load(), state_file=Path(directory) / 'notifications.json')
            with patch.object(boat, 'fetch_calendar', side_effect=calendar), \
                    patch.object(notifier, 'publish_result') as publish, \
                    patch.object(notifier, 'send_message', return_value=123) as send:
                for daily in config.daily_configs():
                    notifier.check_and_notify(daily, now=NOW)
                self.assertEqual(publish.call_count, 2)
                for call in publish.call_args_list:
                    report = call.args[1]
                    self.assertEqual((report['start_time_from'], report['start_time_to']),
                                     ('00:00', '23:59'))
                    self.assertEqual(len(report['slots']), 6)
                self.assertEqual(send.call_count, 2)
                for call in send.call_args_list:
                    self.assertIn('08:30–09:00', call.args[1])
                    self.assertIn('16:30–17:00', call.args[1])

    def test_explicit_time_window_still_overrides_all_day_defaults(self):
        config = self.load({'BOAT_FROM': '10:00', 'BOAT_TO': '16:00'})
        for daily in config.daily_configs():
            self.assertEqual((daily.earliest, daily.latest), (time(10), time(16)))

    def test_negative_sold_out_inventory_publishes_successful_history(self):
        config = list(self.load().daily_configs())[1]
        events = [event('2026-11-21T13:00:00+09:00', '2026-11-21T13:30:00+09:00',
                        ordable=False, is_full=True, order_remain_amount='-1')]
        with tempfile.TemporaryDirectory() as directory:
            config = replace(config, state_file=Path(directory) / 'notifications.json')
            with patch.object(boat, 'fetch_calendar', return_value={'errors': [], 'results': events}), \
                    patch.object(notifier, 'publish_result') as publish, \
                    patch.object(notifier, 'send_message') as send:
                notifier.check_and_notify(config, now=NOW)
                publish.assert_called_once()
                report = publish.call_args.args[1]
                self.assertEqual(report['date'], '2026-11-21')
                self.assertEqual(report['status'], 'NO_AVAILABILITY')
                self.assertEqual(report['slots'][0]['status'], 'SOLD_OUT')
                self.assertEqual(report['slots'][0]['remaining'], 0)
                send.assert_not_called()

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
