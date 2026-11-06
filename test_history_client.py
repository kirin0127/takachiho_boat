import hashlib
import hmac
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import check_boats as boat
import history_client
from test_boat_notifier import NOW, TOKEN
from test_check_boats import DAY, event


class HistoryClientTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(history_port=8766, token=TOKEN, chat_id="-123",
                                      day=DAY, earliest=NOW.time(), latest=NOW.time())

    def test_posts_signed_record_without_token_or_html(self):
        response = MagicMock()
        response.__enter__.return_value.status = 204
        report = boat.make_report({"errors": [], "results": [event()]}, DAY,
                                  NOW.replace(hour=13).time(), NOW.replace(hour=16).time())
        with patch.object(history_client.HISTORY_HTTP, "open", return_value=response) as send:
            history_client.publish_result(self.config, report)
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8766/boat-history")
        self.assertEqual(json.loads(request.data)["chat_id"], "-123")
        self.assertEqual(request.get_header("X-boat-signature"),
                         hmac.new(TOKEN.encode(), request.data, hashlib.sha256).hexdigest())
        self.assertNotIn(TOKEN, request.data.decode())
        self.assertEqual(send.call_args.kwargs["timeout"], 2)

    def test_disabled_history_does_not_use_network(self):
        self.config.history_port = 0
        with patch.object(history_client.HISTORY_HTTP, "open") as send:
            history_client.publish_result(self.config)
        send.assert_not_called()

    def test_offline_history_is_best_effort(self):
        with patch.object(history_client.HISTORY_HTTP, "open", side_effect=URLError("Offline")), \
                self.assertLogs(history_client.log, level="WARNING"):
            history_client.publish_result(self.config)


if __name__ == "__main__":
    unittest.main()
