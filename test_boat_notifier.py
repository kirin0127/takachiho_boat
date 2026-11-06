import contextlib
from dataclasses import replace
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import boat_notifier as notifier
import check_boats as boat
import configure_telegram
from test_check_boats import DAY, event

NOW = datetime(2026, 11, 6, 12, tzinfo=boat.JAPAN)
TOKEN = "123456789:" + "x" * 30  # Synthetic test value, never used on the network.


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        with patch.dict(os.environ, {}, clear=True):
            self.config = replace(notifier.load_config(require_telegram=False),
                                  token=TOKEN, chat_id="-100123456789",
                                  history_port=0,
                                  state_file=self.root / "state" / "notifications.json")

    def run_check(self, events, sender=None, **kwargs):
        with patch.object(boat, "fetch_calendar", return_value={"errors": [], "results": events}), \
                patch.object(notifier, "send_message", side_effect=sender, return_value=123) as send:
            notifier.check_and_notify(self.config, now=NOW, **kwargs)
            return send

    def test_alerts_on_first_availability_and_persists_across_runs(self):
        self.assertEqual(self.run_check([event()]).call_count, 1)
        self.assertEqual(self.run_check([event(order_remain_amount="2")]).call_count, 0)
        state = notifier.load_state(self.config.state_file, self.config.scope)
        self.assertEqual(len(state["notified"]), 1)

    def test_sold_out_then_reopened_notifies_again(self):
        self.run_check([event()])
        self.assertEqual(self.run_check([event(ordable=False, is_full=True, order_remain_amount="0")]).call_count, 0)
        self.assertEqual(self.run_check([event()]).call_count, 1)

    def test_new_slots_are_sent_together_without_repeating_existing(self):
        self.run_check([event()])
        later = event("2026-11-20T14:00:00+09:00", "2026-11-20T14:30:00+09:00")
        last = event("2026-11-20T15:00:00+09:00", "2026-11-20T15:30:00+09:00")
        send = self.run_check([event(), later, last])
        text = send.call_args.args[1]
        self.assertIn("14:00–14:30", text)
        self.assertIn("15:00–15:30", text)
        self.assertNotIn("13:00–13:30", text)

    def test_send_failure_is_not_marked_delivered_and_can_retry(self):
        with patch.object(notifier, "datetime", wraps=datetime) as fake_datetime, \
                self.assertRaises(notifier.TelegramError):
            fake_datetime.now.return_value = NOW
            self.run_check([event()], sender=notifier.TelegramError("Temporary failure"))
        state = notifier.load_state(self.config.state_file, self.config.scope)
        self.assertEqual(state["notified"], [])
        with patch.object(boat, "fetch_calendar", return_value={"errors": [], "results": [event()]}), \
                patch.object(notifier, "send_message", return_value=124) as send:
            notifier.check_and_notify(self.config, now=datetime(2026, 11, 17, tzinfo=boat.JAPAN))
            send.assert_called_once()

    def test_rate_limit_deadline_prevents_early_send(self):
        self.config.state_file.parent.mkdir()
        notifier.save_state(self.config.state_file, {
            "version": 1, "scope": self.config.scope, "notified": [],
            "next_send_after": int(NOW.timestamp()) + 600,
        })
        self.assertEqual(self.run_check([event()]).call_count, 0)

    def test_calendar_errors_and_empty_results_preserve_state(self):
        self.run_check([event()])
        before = self.config.state_file.read_bytes()
        with self.assertRaises(boat.CheckError):
            self.run_check([])
        with patch.object(boat, "fetch_calendar", side_effect=boat.CheckError("Offline")):
            with self.assertRaises(boat.CheckError):
                notifier.check_and_notify(self.config, now=NOW)
        self.assertEqual(self.config.state_file.read_bytes(), before)
        self.assertEqual(self.run_check([event()]).call_count, 0)

    def test_missing_individual_slot_does_not_rearm(self):
        self.run_check([event()])
        other = event("2026-11-20T14:00:00+09:00", "2026-11-20T14:30:00+09:00", ordable=False)
        self.run_check([other])
        self.assertEqual(self.run_check([event(), other]).call_count, 0)

    def test_dry_run_does_not_send_or_write_state(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_check([event()], dry_run=True).call_count, 0)
        self.assertFalse(self.config.state_file.parent.exists())

    def test_history_includes_successes_without_new_alerts_and_failed_checks(self):
        with patch.object(notifier, "publish_result") as publish:
            self.run_check([event()])
            self.run_check([event()])  # Still recorded when the alert is suppressed.
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(publish.call_args.args[1]["status"], "AVAILABLE")
            self.run_check([event(ordable=False)])
            self.assertEqual(publish.call_args.args[1]["status"], "NO_AVAILABILITY")
            with self.assertRaises(boat.CheckError):
                self.run_check([])
            self.assertEqual(publish.call_args.args[1]["status"], "NO_MATCHING_SLOTS")
            with patch.object(boat, "fetch_calendar", side_effect=boat.CheckError("Offline")):
                with self.assertRaises(boat.CheckError):
                    notifier.check_and_notify(self.config, now=NOW)
            self.assertEqual(publish.call_args.args, (self.config,))

    def test_dry_run_does_not_publish_history(self):
        with patch.object(notifier, "publish_result") as publish, \
                contextlib.redirect_stdout(io.StringIO()):
            self.run_check([event()], dry_run=True)
        publish.assert_not_called()

    def test_cutoff_skips_network(self):
        with patch.object(boat, "fetch_calendar") as fetch:
            notifier.check_and_notify(self.config, now=self.config.deadline)
        fetch.assert_not_called()
        self.assertEqual(self.config.deadline.isoformat(), "2026-11-18T09:00:00+09:00")

    def test_changed_chat_gets_its_own_alert(self):
        self.run_check([event()])
        self.config = replace(self.config, chat_id="1234")
        self.assertEqual(self.run_check([event()]).call_count, 1)

    def test_corrupt_state_fails_without_sending(self):
        self.config.state_file.parent.mkdir()
        self.config.state_file.write_text("{")
        with patch.object(notifier, "send_message") as send:
            with self.assertRaises(notifier.ConfigError):
                notifier.check_and_notify(self.config, now=NOW)
        send.assert_not_called()

    def test_overlap_is_rejected(self):
        with notifier.state_lock(self.config.state_file):
            with self.assertRaises(notifier.ConfigError):
                with notifier.state_lock(self.config.state_file):
                    self.fail("Acquired an already-held lock")

    def test_telegram_request_uses_send_only_and_optional_topic(self):
        response = io.BytesIO(json.dumps({"ok": True, "result": {"message_id": 123}}).encode())
        with patch.object(notifier.TELEGRAM_HTTP, "open", return_value=response) as request:
            self.assertEqual(notifier.send_message(replace(self.config, thread_id=25), "Test"), 123)
        sent = request.call_args.args[0]
        self.assertTrue(sent.full_url.endswith("/sendMessage"))
        payload = json.loads(sent.data)
        self.assertEqual(payload["chat_id"], self.config.chat_id)
        self.assertEqual(payload["message_thread_id"], 25)
        self.assertNotIn("parse_mode", payload)

    def test_telegram_errors_hide_token_and_preserve_retry_after(self):
        with patch.object(notifier.TELEGRAM_HTTP, "open", side_effect=URLError(TOKEN)):
            with self.assertRaises(notifier.TelegramError) as caught:
                notifier.send_message(self.config, "Test")
        self.assertNotIn(TOKEN, str(caught.exception))
        error = HTTPError("https://example.invalid/" + TOKEN, 429, TOKEN, {}, io.BytesIO(
            json.dumps({"ok": False, "error_code": 429, "description": TOKEN,
                        "parameters": {"retry_after": 600}}).encode()))
        with patch.object(notifier.TELEGRAM_HTTP, "open", side_effect=error):
            with self.assertRaises(notifier.TelegramError) as caught:
                notifier.send_message(self.config, "Test")
        self.assertEqual(caught.exception.retry_after, 600)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_env_file_and_environment_precedence(self):
        env = self.root / ".env"
        env.write_text(f'TELEGRAM_BOT_TOKEN="{TOKEN}" # token\nTELEGRAM_CHAT_ID=-123\nBOAT_TO=15:30\n')
        with patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "456"}, clear=True):
            config = notifier.load_config(env)
        self.assertEqual(config.chat_id, "456")
        self.assertEqual(config.latest.strftime("%H:%M"), "15:30")
        self.assertNotIn(TOKEN, repr(config))


class ConfigurationImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.env"
        self.source.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nDB_PATH=old.db\n")
        self.database = self.root / "old.db"
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE keywords (chat_id INTEGER, active INTEGER)")
            db.execute("INSERT INTO keywords VALUES (-123, 1)")
        self.output = self.root / "output.env"

    def run_import(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = configure_telegram.main(["--source", str(self.source),
                                            "--output", str(self.output), *args])
        self.assertNotIn(TOKEN, output.getvalue())
        return code, output.getvalue()

    def test_single_chat_import_uses_existing_token_and_private_permissions(self):
        before = self.database.read_bytes()
        self.assertEqual(self.run_import()[0], 0)
        values = notifier.read_env(self.output)
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], TOKEN)
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "-123")
        self.assertEqual(values["BOAT_FROM"], "00:00")
        self.assertEqual(values["BOAT_TO"], "23:59")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.run_import()[0], 2)  # Existing config is not overwritten.

    def test_multiple_chats_require_explicit_selection(self):
        with contextlib.closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO keywords VALUES (456, 1)")
        self.assertEqual(self.run_import()[0], 2)
        self.assertFalse(self.output.exists())
        code, output = self.run_import("--list-chats")
        self.assertEqual(code, 0)
        self.assertIn("456", output)
        self.assertEqual(self.run_import("--chat-id=-123")[0], 0)

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing.db"
        self.assertEqual(self.run_import("--database", str(missing))[0], 2)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
