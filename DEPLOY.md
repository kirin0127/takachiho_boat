# Deploy the boat notifier to your existing VPS

This runs alongside your existing second-hand notification service. It uses the same Telegram bot and chosen chat, checks November 20, 2026 for starts between 13:00 and 16:00 Japan time, and sends a message when a slot becomes available.

The supplied systemd timer runs one check every two minutes after the previous check finishes. Each run exits when done. Multiple Python programs can run on the same VPS; systemd manages them separately.

## What was found in `second_hand_notify`

The local project at `/Users/jeffchuangmbp2024/Desktop/Projects/second_hand_notify` documents Ubuntu 22.04/24.04, `/opt/second_hand_notify`, and a service named `pttbot`. It reads `TELEGRAM_BOT_TOKEN` from `.env`. Its SQLite `keywords` table stores recipient `chat_id` values; `DB_PATH` defaults to `data.db` relative to that project.

Its existing GitHub workflow uses secrets named `VPS_HOST`, `VPS_USER`, and `VPS_SSH_KEY`. The actual secret values, live `.env`, recipient database, and VPS address are not present in this local checkout. The steps below reuse the live values on the VPS. Replace `YOUR_VPS_HOST` with your VPS address. The GitHub repository is `kirin0127/takachiho_boat`; the VPS installation directory remains `/opt/boat`. Confirm the paths against your actual service if they differ from the documented defaults.

The new notifier only calls Telegram's `sendMessage`. It does not consume incoming updates, register commands, or modify webhooks, so the existing `pttbot` continues handling commands normally. No new domain, incoming port, Caddy configuration, or bot is needed.

## 1. Push this project to GitHub

Commit the Python files, tests, `.env.example`, `.gitignore`, documentation, and `deploy/` directory. Real configuration and runtime state are ignored. There is no GitHub Actions workflow in this project yet; you can add deployment later.

Locally, run the tests before pushing:

```bash
cd /Users/jeffchuangmbp2024/Desktop/Projects/takachiho_boat
python3 -m unittest -v
```

All code uses the Python standard library. Python 3.10+ is required; there is no pip installation step.

## 2. Log in and clone on the VPS

```bash
ssh root@YOUR_VPS_HOST
systemctl status pttbot --no-pager
python3 --version
apt-get update
apt-get install -y python3 git ca-certificates tzdata
git clone git@github.com:kirin0127/takachiho_boat.git /opt/boat
cd /opt/boat
python3 -m unittest -v
```

The SSH clone command requires a key on the VPS authorized for **this** repository. For a private repo, add a read-only GitHub deploy key to `takachiho_boat`. A deploy key attached to `second_hand_notify` does not automatically grant access to `takachiho_boat`. If `/opt/boat` already exists, update that checkout instead of cloning over it.

The commands assume the root login used in your existing guide. If you use another SSH account, use `sudo` for `/opt`, `/etc`, and systemd operations. The running checker itself uses an unprivileged dynamic service user.

## 3. Reuse the existing bot and recipient

List active recipients from the existing database; this prints chat IDs and keyword counts, never the token or keywords:

```bash
cd /opt/boat
python3 configure_telegram.py --list-chats
```

The helper reads `/opt/second_hand_notify/.env`, resolves its `DB_PATH`, and opens the SQLite database read-only. If your source files are elsewhere, pass `--source /actual/path/.env` and, if needed, `--database /actual/path/data.db` to every helper invocation.

If exactly one active recipient exists, import it automatically:

```bash
python3 configure_telegram.py --output /etc/takachiho-boat.env
```

If multiple recipients exist, choose the chatroom you want from the list:

```bash
python3 configure_telegram.py --chat-id=-1001234567890 --output /etc/takachiho-boat.env
```

Replace the example ID with the real ID, preserving its minus sign. The helper refuses to guess among multiple chats or overwrite an existing output file. It creates the configuration with permissions `0600` and does not print the token. The existing project's `.env` and database remain unchanged.

If there are no active recipients, or the bot gets its token from a service environment instead of `.env`, create the boat configuration manually:

```bash
install -m 600 .env.example /etc/takachiho-boat.env
nano /etc/takachiho-boat.env
```

Fill in `TELEGRAM_BOT_TOKEN` with the **same** bot token, and `TELEGRAM_CHAT_ID` with your chosen chat ID. Do not use `getUpdates` to discover recipients while the existing bot is receiving updates; use its stored chat IDs. If you need a specific forum topic, set `TELEGRAM_THREAD_ID` too; the old project's database does not store topic IDs.

Configuration fields:

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Existing bot's API token |
| `TELEGRAM_CHAT_ID` | Numeric target chat ID, including `-` for groups |
| `TELEGRAM_THREAD_ID` | Optional Telegram forum topic ID |
| `BOAT_DATE` | `2026-11-20` |
| `BOAT_FROM` | Earliest allowed start, `13:00` Japan time |
| `BOAT_TO` | Latest allowed start, `16:00`, inclusive |
| `BOAT_STATE_FILE` | `/var/lib/takachiho-boat/notifications.json` |
| `REQUEST_TIMEOUT_SECONDS` | `30`, permitted range 1–60 |

A 16:00 ride finishes at 16:30. Set `BOAT_TO=15:30` if you must finish by 16:00. Use one `KEY=value` per line; simple quoted values and comments are supported, with no variable expansion or multiline values. Process environment variables take precedence over `--env-file` settings.

Credentials are copied into the boat configuration, not continuously synchronized. If the bot token is rotated later, update both projects' configuration files.

## 4. Verify manually

Preview a live check without sending Telegram messages or changing notification state:

```bash
cd /opt/boat
python3 boat_notifier.py --env-file /etc/takachiho-boat.env --dry-run
```

Then deliberately send one setup test to the selected chat:

```bash
python3 boat_notifier.py --env-file /etc/takachiho-boat.env --test-telegram
```

Confirm the message appears in the intended chat. It clearly says it is a setup test, not an availability alert. This command does not reserve anything or alter availability state.

## 5. Install and enable systemd

```bash
cd /opt/boat
install -m 644 deploy/takachiho-boat.service /etc/systemd/system/takachiho-boat.service
install -m 644 deploy/takachiho-boat.timer /etc/systemd/system/takachiho-boat.timer
systemd-analyze verify /etc/systemd/system/takachiho-boat.service /etc/systemd/system/takachiho-boat.timer
systemctl daemon-reload
systemctl start takachiho-boat.service
journalctl -u takachiho-boat.service -n 30 --no-pager
systemctl enable --now takachiho-boat.timer
systemctl list-timers takachiho-boat.timer --no-pager
```

The first service start performs a real check and sends an alert if a matching slot is available. The service runs as a dynamic unprivileged user. systemd reads `/etc/takachiho-boat.env` and creates a persistent writable state directory at `/var/lib/takachiho-boat` (which may be a systemd-managed symlink).

The **timer** should be `active (waiting)`. The **service** normally becomes `inactive (dead)` after a successful check; this is expected for `Type=oneshot`. Use `systemctl show takachiho-boat.service -p Result -p ExecMainStatus` to check the result. Exit `0` means a successful check, including no availability; exit `2` means an error. These differ from `check_boats.py`'s command-line exit codes.

Do not run a second scheduler or `check_boats.py --watch` for this service. The timer schedules the checks, and an exclusive file lock prevents overlapping notifier runs using the same state file.

## 6. Notification behavior and daily operations

On the first successful check, any available matching slots trigger an alert. Newly available slots are grouped into one message with Japan ride times, remaining boat counts, the check timestamp, and a booking link.

A successfully notified slot is not repeatedly announced while it remains available, even after restarting or redeploying. If a later successful check explicitly observes it unavailable, it is rearmed and can trigger another alert when it opens again. Inventory count changes alone do not trigger another message. Availability that appears and disappears entirely between checks can be missed.

Calendar failures, empty responses, and missing individual slots do not erase prior acknowledgements. Failed Telegram sends remain eligible for retry on later checks if the slot is still available. Telegram's `retry_after` is persisted to defer rate-limited sends. As with any external messaging API, a timeout after Telegram accepts a message, or a crash before saving state, can cause a duplicate on retry; delivery cannot be guaranteed exactly once.

| Task | Command |
| --- | --- |
| Follow logs | `journalctl -u takachiho-boat.service -f` |
| View recent results | `journalctl -u takachiho-boat.service -n 50 --no-pager` |
| Check the schedule | `systemctl list-timers takachiho-boat.timer --no-pager` |
| Run a check now | `systemctl start takachiho-boat.service` |
| Stop monitoring | `systemctl disable --now takachiho-boat.timer` then `systemctl stop takachiho-boat.service` |
| Resume monitoring | `systemctl enable --now takachiho-boat.timer` |
| Change settings | `nano /etc/takachiho-boat.env`; the next invocation reads them |

To change the interval, use `systemctl edit takachiho-boat.timer` and add:

```ini
[Timer]
OnUnitInactiveSec=
OnUnitInactiveSec=300s
```

Then run `systemctl daemon-reload` and `systemctl restart takachiho-boat.timer`. Keep intervals at least 60 seconds; the supplied timer uses 120 seconds. The interval includes the time spent performing the previous check.

For November 20, the published booking cutoff is **November 18 at 09:00 Japan time / 08:00 Taiwan time**. The notifier stops network requests at that cutoff, although the timer keeps producing inexpensive cutoff log entries until disabled. Disable the timer after booking or after the deadline.

If the service fails, the next timer activation tries again. Check the journal for calendar/network errors or Telegram codes: `400` usually means a bad chat/topic ID, `401` a token problem, `403` missing access to the chat, and `429` rate limiting. The bot must already have permission to message that chat. If a group migrates to a supergroup, update the chat ID. For invalid state, stop the timer and inspect or restore the state file; deleting it intentionally rearms all alerts.

## 7. Manual updates and future GitHub Actions

After pushing an update, run on the VPS:

```bash
(
set -eu
systemctl stop takachiho-boat.timer
systemctl stop takachiho-boat.service
cd /opt/boat
git pull --ff-only
python3 -m unittest -v
install -m 644 deploy/takachiho-boat.service /etc/systemd/system/takachiho-boat.service
install -m 644 deploy/takachiho-boat.timer /etc/systemd/system/takachiho-boat.timer
systemd-analyze verify /etc/systemd/system/takachiho-boat.service /etc/systemd/system/takachiho-boat.timer
systemctl daemon-reload
systemctl start takachiho-boat.service
systemctl start takachiho-boat.timer
journalctl -u takachiho-boat.service -n 20 --no-pager
)
```

Keep `/etc/takachiho-boat.env` and `/var/lib/takachiho-boat` outside Git and intact across updates. If an update fails, restore the previous code before restarting the timer.

For your later GitHub Actions workflow, the existing secret naming convention (`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`) can be reused, but repository secrets must be configured separately in the new repo (or shared through organization secrets). Run tests before deployment, serialize deployments, and fail the remote script on errors. Deploy to `/opt/boat` and manage `takachiho-boat.service` / `takachiho-boat.timer`; keep the `pttbot` service and its files untouched. Use the oneshot exit result and the active timer as health checks, rather than expecting the checker service to stay running.

Reference documentation: [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage), [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html), [systemd execution settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html), and [the boat reservation conditions](https://eipro.jp/takachiho1/terms/view/WPT00000006).
