#!/usr/bin/env python3
"""Import the existing VPS bot token and select a recipient from its read-only DB."""

import argparse
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys

from boat_notifier import ConfigError, read_env


def existing_chats(database):
    # mode=ro prevents silently creating a new DB if the path is wrong.
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        return db.execute("SELECT chat_id, COUNT(*) FROM keywords WHERE active=1 "
                          "GROUP BY chat_id ORDER BY chat_id").fetchall()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/second_hand_notify/.env"))
    parser.add_argument("--database", type=Path, help="Defaults to the existing project's DB_PATH.")
    parser.add_argument("--list-chats", action="store_true", help="Print active chat IDs and keyword counts only.")
    parser.add_argument("--chat-id", type=int, help="Choose a chat when there is more than one recipient.")
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)
    try:
        values = read_env(args.source)
        database = args.database or Path(values.get("DB_PATH") or "data.db")
        if not database.is_absolute():
            database = args.source.resolve().parent / database
        chats = existing_chats(database)
        if args.list_chats:
            print("CHAT_ID\tACTIVE_KEYWORDS")
            for chat_id, count in chats:
                print(f"{chat_id}\t{count}")
            if not chats:
                print("No active recipients found in the existing database.")
            return 0
        chosen = args.chat_id
        if chosen is None:
            if len(chats) != 1:
                raise ConfigError("Expected exactly one active chat. Use --list-chats, then --chat-id.")
            chosen = chats[0][0]
        if chosen not in {chat_id for chat_id, _ in chats}:
            raise ConfigError("Selected chat is not an active recipient in the existing database.")
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ConfigError("Existing .env does not contain a valid TELEGRAM_BOT_TOKEN.")
        content = (
            "# Imported locally from second_hand_notify. Keep this file off GitHub.\n"
            f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chosen}\n"
            "TELEGRAM_THREAD_ID=\nBOAT_DATE=2026-11-20\nBOAT_FROM=13:00\nBOAT_TO=16:00\n"
            "BOAT_STATE_FILE=/var/lib/takachiho-boat/notifications.json\n"
            "REQUEST_TIMEOUT_SECONDS=30\n"
        )
        # Exclusive creation prevents replacing existing credentials accidentally.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(content)
        print(f"Created {args.output} with mode 0600 for chat {chosen}; token not displayed.")
        return 0
    except (ConfigError, OSError, sqlite3.Error) as exc:
        print(f"Configuration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
