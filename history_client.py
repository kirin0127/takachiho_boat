"""Forward a small check result to pttbot's in-memory localhost cache."""

from datetime import datetime
import hashlib
import hmac
from http.client import HTTPException
import json
import logging
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import check_boats as boat

log = logging.getLogger(__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Localhost results must never be routed through an environment-configured proxy.
HISTORY_HTTP = build_opener(ProxyHandler({}), NoRedirect())


def publish_result(config, report=None):
    """Best effort: cache delivery failures must not block availability alerts."""
    if not config.history_port:
        return
    if report is None:
        report = {"checked_at": datetime.now(boat.JAPAN).isoformat(timespec="seconds"),
                  "date": config.day.isoformat(),
                  "start_time_from": config.earliest.strftime("%H:%M"),
                  "start_time_to": config.latest.strftime("%H:%M"),
                  "status": "ERROR", "slots": []}
    record = {key: report[key] for key in (
        "checked_at", "date", "start_time_from", "start_time_to", "status", "slots")}
    record["chat_id"] = config.chat_id
    body = json.dumps(record, ensure_ascii=False).encode("utf-8")
    signature = hmac.new(config.token.encode("utf-8"), body, hashlib.sha256).hexdigest()
    request = Request(f"http://127.0.0.1:{config.history_port}/boat-history", data=body,
                      headers={"Content-Type": "application/json", "X-Boat-Signature": signature})
    try:
        with HISTORY_HTTP.open(request, timeout=2) as response:
            if response.status != 204:
                raise OSError("Unexpected cache response")
    except (URLError, OSError, HTTPException):
        log.warning("History cache unreachable or rejected this check; verify pttbot, its token, and BOAT_HISTORY_PORT.")
