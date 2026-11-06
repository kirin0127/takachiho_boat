#!/usr/bin/env python3
"""One availability check plus Telegram alerts; scheduled by a systemd timer."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import fcntl
from http.client import HTTPException
import json
import logging
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import check_boats as boat
from history_client import publish_result

log = logging.getLogger("boat_notifier")
BASE_DIR = Path(__file__).resolve().parent


class ConfigError(Exception):
    pass


class TelegramError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def read_env(path):
    """Read simple KEY=value files without executing shell code or interpolation."""
    values = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"Invalid environment assignment on line {number}.")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigError(f"Invalid quoting on environment line {number}.") from exc
        if len(parts) > 1:
            raise ConfigError(f"Quote values containing spaces on environment line {number}.")
        values[key] = parts[0] if parts else ""
    return values


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    chat_id: str
    day: date
    earliest: time
    latest: time
    state_file: Path
    timeout: int = 30
    thread_id: int | None = None
    history_port: int = 8766

    @property
    def deadline(self):
        # Published booking cutoff: 09:00 Japan time, two days before riding.
        return datetime.combine(self.day - timedelta(days=2), time(9), boat.JAPAN)

    @property
    def scope(self):
        # Changing destination or window should not suppress that destination's alerts.
        return "|".join((self.token.split(":", 1)[0], self.chat_id,
                         str(self.thread_id), self.day.isoformat(),
                         self.earliest.isoformat(), self.latest.isoformat()))


def load_config(env_file=None, require_telegram=True):
    values = {}
    path = Path(env_file) if env_file else BASE_DIR / ".env"
    if env_file or path.exists():
        values.update(read_env(path))
    values.update(os.environ)  # systemd's EnvironmentFile takes precedence.
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = values.get("TELEGRAM_CHAT_ID", "").strip()
    if require_telegram:
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("Set TELEGRAM_BOT_TOKEN to the existing bot token.")
        if not re.fullmatch(r"-?[1-9][0-9]*", chat_id):
            raise ConfigError("Set TELEGRAM_CHAT_ID to the existing numeric chat ID.")
    try:
        day = date.fromisoformat(values.get("BOAT_DATE", "2026-11-20"))
        earliest = datetime.strptime(values.get("BOAT_FROM", "13:00"), "%H:%M").time()
        latest = datetime.strptime(values.get("BOAT_TO", "16:00"), "%H:%M").time()
        timeout = int(values.get("REQUEST_TIMEOUT_SECONDS", "30"))
        thread_id = int(values["TELEGRAM_THREAD_ID"]) if values.get("TELEGRAM_THREAD_ID") else None
        history_port = int(values.get("BOAT_HISTORY_PORT", "8766"))
    except ValueError as exc:
        raise ConfigError("Invalid date, time, timeout, Telegram topic ID, or history port in configuration.") from exc
    if earliest > latest or not 1 <= timeout <= 60 or (thread_id is not None and thread_id <= 0):
        raise ConfigError("Use FROM <= TO, timeout 1–60 seconds, and a positive topic ID.")
    if not 0 <= history_port <= 65535:
        raise ConfigError("BOAT_HISTORY_PORT must be between 0 and 65535 (0 disables history).")
    state_file = Path(values.get("BOAT_STATE_FILE", str(BASE_DIR / "state" / "notifications.json")))
    if not state_file.is_absolute():
        state_file = BASE_DIR / state_file
    return Config(token, chat_id, day, earliest, latest, state_file, timeout, thread_id, history_port)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


TELEGRAM_HTTP = build_opener(NoRedirect())


def send_message(config, text):
    """Send only: never start polling or change the existing bot's webhook."""
    body = {
        "chat_id": config.chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": [[
            {"text": "Open booking calendar", "url": boat.CALENDAR_URL}
        ]]},
    }
    if config.thread_id is not None:
        body["message_thread_id"] = config.thread_id
    request = Request(
        f"https://api.telegram.org/bot{config.token}/sendMessage",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    status = 200
    try:
        try:
            with TELEGRAM_HTTP.open(request, timeout=config.timeout) as response:
                raw = response.read(1_000_001)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(1_000_001)
            exc.close()
        payload = json.loads(raw)
    except (URLError, OSError, ValueError, HTTPException):
        # urllib exceptions can contain the token-bearing URL. Never log them.
        raise TelegramError("Telegram network/response error; delivery is unconfirmed.") from None
    if not isinstance(payload, dict):
        raise TelegramError("Unexpected Telegram response; delivery is unconfirmed.")
    if status != 200 or payload.get("ok") is not True:
        code = payload.get("error_code", status)
        code = code if type(code) is int else status
        parameters = payload.get("parameters")
        retry = parameters.get("retry_after", 0) if isinstance(parameters, dict) else 0
        retry = retry if type(retry) is int and retry > 0 else 0
        hints = {400: "check chat/topic ID", 401: "check bot token",
                 403: "bot cannot send to this chat", 429: "rate limited"}
        raise TelegramError(f"Telegram error {code}: {hints.get(code, 'request failed')}.", retry)
    result = payload.get("result")
    if not isinstance(result, dict) or type(result.get("message_id")) is not int:
        raise TelegramError("Telegram did not confirm a message ID.")
    return result["message_id"]


def slot_key(slot):
    return slot["start"] + "/" + slot["end"]


def notification_text(config, slots, checked_at):
    lines = ["🚣 Takachiho Gorge boat available!", f"Date: {config.day.isoformat()}",
             "Ride times below are Japan time (UTC+9):"]
    for slot in slots:
        lines.append(f"• {slot['start'][11:16]}–{slot['end'][11:16]}"
                     f" — {slot['remaining']} boat(s) available")
    lines += ["", f"Checked: {checked_at}",
              "Book promptly on the official website; availability can change.", boat.CALENDAR_URL]
    return "\n".join(lines)


@contextmanager
def state_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("Another notifier is already using this state file.") from None
        yield


def load_state(path, scope):
    empty = {"version": 1, "scope": scope, "notified": [], "next_send_after": 0}
    if not path.exists():
        return empty
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        valid = (
            isinstance(state, dict) and state.get("version") == 1
            and isinstance(state.get("scope"), str)
            and isinstance(state.get("notified"), list)
            and all(isinstance(key, str) for key in state["notified"])
            and type(state.get("next_send_after")) is int
        )
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ConfigError("Invalid notification state; inspect or restore the state file.")
    return state if state["scope"] == scope else empty


def save_state(path, state):
    # Replace atomically so a restart cannot leave a partially written JSON file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".notifications-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def check_and_notify(config, dry_run=False, now=None):
    now = now or datetime.now(boat.JAPAN)
    if now >= config.deadline:
        log.info("Booking cutoff reached (%s); no further network checks.", config.deadline.isoformat())
        return

    def check(state=None):
        try:
            report = boat.make_report(boat.fetch_calendar(config.day, config.timeout),
                                      config.day, config.earliest, config.latest)
        except (boat.CheckError, OSError, HTTPException):
            if not dry_run:
                publish_result(config)
            raise
        if not dry_run:
            publish_result(config, report)
        if not report["slots"]:
            raise boat.CheckError("No matching slots returned; retaining previous notification state.")
        available = [slot for slot in report["slots"] if slot["available"]]
        log.info("Checked %s, %s–%s Japan time: %d matching, %d available.",
                 config.day, config.earliest.strftime("%H:%M"), config.latest.strftime("%H:%M"),
                 len(report["slots"]), len(available))
        if dry_run:
            boat.display(report, False)
            if available:
                print("\nTelegram preview (not sent):\n" + notification_text(config, available, report["checked_at"]))
            return
        notified = set(state["notified"])
        # Only an explicit unavailable observation re-arms a previously alerted slot.
        # Missing slots and failed checks must not cause duplicate alerts.
        notified.difference_update(slot_key(slot) for slot in report["slots"] if not slot["available"])
        new_slots = [slot for slot in available if slot_key(slot) not in notified]
        state["notified"] = sorted(notified)
        state["last_checked_at"] = report["checked_at"]
        if new_slots and now.timestamp() >= state["next_send_after"]:
            try:
                message_id = send_message(config, notification_text(config, new_slots, report["checked_at"]))
            except TelegramError as exc:
                state["next_send_after"] = int(datetime.now(boat.JAPAN).timestamp()) + exc.retry_after + 1
                save_state(config.state_file, state)
                raise
            notified.update(slot_key(slot) for slot in new_slots)
            state["notified"] = sorted(notified)
            state["next_send_after"] = 0
            log.info("Telegram message %s sent for %d newly available slot(s).", message_id, len(new_slots))
        elif new_slots:
            log.info("Telegram retry deferred until the server's retry_after deadline.")
        save_state(config.state_file, state)

    if dry_run:
        check()
    else:
        with state_lock(config.state_file):
            check(load_state(config.state_file, config.scope))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Read KEY=value settings from this file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Check and preview; no Telegram sends or state changes.")
    mode.add_argument("--test-telegram", action="store_true", help="Send one explicit setup test message; no calendar check.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.env_file, require_telegram=not args.dry_run)
        if args.test_telegram:
            send_message(config, "🚣 Takachiho boat notifier setup test.\n"
                         f"Monitoring {config.day}, {config.earliest:%H:%M}–{config.latest:%H:%M} "
                         "start times (Japan time).\nThis is a setup test, not an availability alert.")
            log.info("Telegram setup test sent successfully.")
        else:
            check_and_notify(config, dry_run=args.dry_run)
        return 0
    except (ConfigError, boat.CheckError, TelegramError, OSError, HTTPException) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
