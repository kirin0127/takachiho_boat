# Deploy the boat notifier to your existing VPS

This runs alongside your existing second-hand notification service. It uses the same Telegram bot and chosen chat, checks November 20, 2026 for starts between 13:00 and 16:00 Japan time, and sends a message when a slot becomes available.

The supplied systemd timer runs one check every two minutes after the previous check finishes. Each run exits when done. Multiple Python programs can run on the same VPS; systemd manages them separately.

## What was found in `second_hand_notify`

The local project at `/Users/jeffchuangmbp2024/Desktop/Projects/second_hand_notify` documents Ubuntu 22.04/24.04, `/opt/second_hand_notify`, and a service named `pttbot`. It reads `TELEGRAM_BOT_TOKEN` from `.env`. Its SQLite `keywords` table stores recipient `chat_id` values; `DB_PATH` defaults to `data.db` relative to that project.

Its existing GitHub workflow uses secrets named `VPS_HOST`, `VPS_USER`, and `VPS_SSH_KEY`. The actual secret values, live `.env`, recipient database, and VPS address are not present in this local checkout. The steps below reuse the live values on the VPS. Replace `YOUR_VPS_HOST` with your VPS address. The GitHub repository is `kirin0127/takachiho_boat`; the VPS installation directory remains `/opt/boat`. Confirm the paths against your actual service if they differ from the documented defaults.

The notifier only calls Telegram's `sendMessage`; the existing `pttbot` handles incoming commands, including `/boat_history`. After each scheduled check, the notifier also forwards a compact signed result to `pttbot` on `127.0.0.1:8766`. No new domain, public incoming port, Caddy configuration, or bot is needed.

## Enable /boat_history on an existing VPS

This feature changes **both repositories**. Push the updated `takachiho_boat` and `second_hand_notify` projects before updating the VPS. The command belongs to `second_hand_notify`, which already receives Telegram updates for the shared bot. Do not start another Telegram poller.

If you previously added `/chatid` manually on the VPS, preserve or commit that edit before pulling. The updated bot already includes `/chatid` along with `/boat_history`.

On the VPS, update the command-handling bot first. If its GitHub Actions deployment already updated it, verify the logs rather than deploying it twice:

```bash
(
set -eu
cd /opt/second_hand_notify
git pull --ff-only
.venv/bin/python -m pytest -q
sudo systemctl restart pttbot
sudo journalctl -u pttbot -n 20 --no-pager
)
```

The journal should show `Boat history cache listening on 127.0.0.1:8766`.

Then update the checker:

```bash
(
set -eu
sudo systemctl stop takachiho-boat.timer
sudo systemctl stop takachiho-boat.service
cd /opt/boat
git pull --ff-only
python3 -m unittest -v
sudo systemctl start takachiho-boat.service
sudo systemctl start takachiho-boat.timer
)
```

These updates do not change your timer configuration; an existing 60-second override remains in place. Both projects default to `BOAT_HISTORY_PORT=8766`, so no new configuration is required. If that port is already in use, set the same alternative port in `/opt/second_hand_notify/.env` and `/etc/takachiho-boat.env`, then restart `pttbot`. Set it to `0` in both to disable history. The shared bot token already in each project authenticates the connection; no new secret is needed.

Send `/boat_history` in the configured group. If Telegram privacy mode prevents delivery of the unaddressed command, send `/boat_history@YourBotUsername`. The command also appears in the bot's slash-command menu after `pttbot` restarts. Any member of the configured group can request its history; unrelated chats cannot retrieve that group's cached results.

The reply shows up to five **calendar check results**, including checks with no available slots, individual slot statuses, and failed checks. Times are in Japan time and records are newest first. It does not fetch the calendar on demand, and it is not a history of Telegram alerts. Failed calendar checks are shown as `ERROR`, never as sold out. A failed Telegram alert does not alter the calendar result already cached.

History is a Python `deque(maxlen=5)` in the long-running `pttbot` process. Nothing is written to a history database or file. After a bot restart, fewer than five records are normal until checks refill the cache. After the booking cutoff, no further calendar checks are made, so the last cached timestamps stop advancing. The existing JSON file for notification deduplication remains separate from this history feature.

If the command reports an empty cache, confirm `TELEGRAM_CHAT_ID` is the chat where you issued the command, and inspect both service journals. A `History cache unreachable or rejected this check` warning means the listener is down, the ports differ, or the two bot tokens differ. Cache delivery is best-effort with a two-second timeout; missed history records are not replayed. Availability alerts continue when the cache is unavailable. Do not open port 8766 in the firewall or expose it through Caddy.

## 1. Push this project to GitHub

Commit the Python files, tests, `.env.example`, `.gitignore`, documentation, `deploy/`, and `.github/workflows/deploy.yml`. Real configuration and runtime state are ignored. The included workflow deploys pushes to `main` after tests pass; configure the GitHub secrets in section 8 before pushing it.

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

Sections 2–5 describe one-time administrative setup. Use an administrator account for those steps, with `sudo` when needed. Automatic GitHub Actions deployment uses your existing **`deploy`** account; section 8 covers its SSH access and repository ownership. The running checker itself uses a separate unprivileged dynamic service user.

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
| `BOAT_HISTORY_PORT` | Local `pttbot` cache port, default `8766`; `0` disables publishing |

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

## 7. Manual updates

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

## 8. Deploy automatically on GitHub pushes

The workflow at `.github/workflows/deploy.yml` runs on every push to `main` and supports manual **Run workflow** from the Actions tab. It tests Python 3.10 and 3.12, then connects to your VPS, tests the candidate with the VPS's Python, installs the exact tested commit in `/opt/boat`, preserves the administrator-managed systemd unit files, performs a real availability check, and enables the timer.

An in-progress deployment finishes before another starts. Superseded commits are skipped rather than deploying an untested newer commit. Deployment fails if `/opt/boat` contains local changes, so preserve those changes before retrying. If an installation or live check fails after stopping the timer, the script attempts to restore the previous commit, and restarts the timer if it was running before deployment. Inspect the failed Actions log if rollback itself encounters a VPS error.

Credentials in `/etc/takachiho-boat.env`, state under `/var/lib/takachiho-boat`, and systemd drop-in overrides remain intact. Your 60-second override is preserved. The workflow does not deploy or restart `pttbot`; use the `second_hand_notify` workflow for that project. A deployment's real check can send a normal availability alert if it finds an opening.

### A. Check the existing `deploy` account

Set `VPS_USER=deploy`. The workflow already supports a non-root login and uses `sudo -n` for administrative commands, so the YAML does not need a username hardcoded into it.

Using your administrator login on the VPS, inspect the existing account, home directory, repository ownership, and sudo policy:

```bash
id deploy
getent passwd deploy
ls -ld /opt/boat /opt/boat/.git
sudo -l -U deploy
```

The examples below assume the home directory is `/home/deploy`; use the home directory shown by `getent passwd deploy` if different. If `/opt/boat` was cloned as root and `deploy` cannot write it, transfer ownership of this checkout only:

```bash
sudo chown -R deploy:"$(id -gn deploy)" /opt/boat
```

Keep `/etc/takachiho-boat.env` and `/var/lib/takachiho-boat` under their existing ownership. The service reads its credentials through systemd; the deployment account does not need to read the Telegram token.

Once SSH key authentication is set up in section B, log in **as deploy** from your Mac:

```bash
ssh deploy@YOUR_VPS_HOST
```

Then verify the checkout and the non-interactive permissions on the VPS:

```bash
cd /opt/boat
git status --short
git fetch origin main
python3 --version
sudo -n -l
test -s /etc/takachiho-boat.env
test -f /etc/systemd/system/takachiho-boat.service
test -f /etc/systemd/system/takachiho-boat.timer
sudo -n systemctl show takachiho-boat.service -p Result -p ExecMainStatus
```

`git status --short` should be empty. Missing files mean the initial installation is incomplete; a sudo permission error means the existing role needs additional authorization. An account allowed to restart only `pttbot` will not automatically be allowed to deploy this project.

The exact non-interactive permissions are listed in `deploy/deploy.sudoers`. As an administrator, use `sudo visudo -f /etc/sudoers.d/takachiho-boat` and paste that file's contents, then run `sudo visudo -c`. This adds only the boat service/timer commands and boat service logs used by the workflow; preserve the existing `pttbot` policy. Systemd unit changes must be installed separately by an administrator.

Run `git fetch` as **deploy**, not as root. Root's GitHub key and SSH configuration are not automatically available to deploy. If the private repository cannot be fetched, configure a separate read-only GitHub deploy key under the deploy account and authorize its public key in this repository's **Settings → Deploy keys**.

### B. Reuse the existing deploy private key

Open [takachiho_boat → Settings → Secrets and variables → Actions](https://github.com/kirin0127/takachiho_boat/settings/secrets/actions). Configure only these three repository secrets:

| Secret | Value |
| --- | --- |
| `VPS_HOST` | VPS IP address or hostname |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | Complete existing deploy private key, including BEGIN/END lines |

Reuse the private key you already created as deploy on the VPS and can retrieve. No new key is needed. Its corresponding public key must be present in `/home/deploy/.ssh/authorized_keys` for incoming login. If the same key already works for the `second_hand_notify` Actions login as deploy, reuse that exact private key here. Secrets in the other repository are not inherited automatically.

The workflow uses SSH port 22 and a private key without a passphrase. It does not perform host fingerprint verification. It reads no additional SSH secrets. The Telegram token and chat ID remain in the existing VPS environment file.

To copy an existing key from the VPS directly to your Mac clipboard, run on your Mac, replacing the host and filename with the actual values:

```bash
ssh deploy@YOUR_VPS_HOST 'cat /home/deploy/.ssh/YOUR_EXISTING_PRIVATE_KEY' | pbcopy
```

Paste the clipboard into `VPS_SSH_KEY`. This command reads the existing key; it does not create or replace one.

After a deployment succeeds using that existing key, clean up the temporary setup:

1. Delete unused `VPS_HOST_FINGERPRINT`, `VPS_PORT`, and `VPS_SSH_PASSPHRASE` secrets from this repository if present.
2. On the Mac, display `~/.ssh/takachiho_actions.pub` to identify the exact temporary public key. Keep an administrator VPS session open.
3. On the VPS, back up `/home/deploy/.ssh/authorized_keys` and remove only the line matching that temporary public key. Preserve the existing deploy key and other authorized keys.
4. On the Mac, delete `~/.ssh/takachiho_actions` and `~/.ssh/takachiho_actions.pub` after confirming they are the temporary pair and are no longer in use.

Keep `/opt/boat` owned by deploy and keep `/etc/sudoers.d/takachiho-boat`: both are required by this workflow. Keep the original `/etc/sudoers.d/deploy` policy used by pttbot. Changing the Actions SSH key does not require changing these permissions or restarting the bot.

### C. Commit and push

After configuring the secrets, run on your Mac:

```bash
cd /Users/jeffchuangmbp2024/Desktop/Projects/takachiho_boat
python3 -m unittest -v
git add .
git diff --cached --stat
git commit -m "Add boat history and automatic VPS deployment"
git push origin main
```

The pending `/boat_history` changes are included in this commit. Also commit and push the corresponding changes in `second_hand_notify` so its existing workflow deploys the command handler and in-memory cache. Updating only this repository will not add a Telegram command to the already-running bot.

### D. Verify the deployment

Open [the boat repository's Actions tab](https://github.com/kirin0127/takachiho_boat/actions), choose **Deploy boat notifier to VPS**, and inspect the latest run. Both test jobs and the deployment job should pass. The deploy log prints `Deployed <commit>` and shows the service exit result and timer schedule.

On the VPS you can also check:

```bash
git -C /opt/boat log -1 --oneline
sudo systemctl status takachiho-boat.timer --no-pager
sudo systemctl show takachiho-boat.service -p Result -p ExecMainStatus
sudo journalctl -u takachiho-boat.service -n 20 --no-pager
```

Expect an active timer and `Result=success`, `ExecMainStatus=0`. The oneshot service does not stay running between checks. If a run failed because secrets were missing, add the secrets and select **Re-run failed jobs**; no empty commit is necessary. Manual dispatch should use the `main` branch.

Common failures: `Permission denied (publickey)` means the Actions SSH key is not authorized on the VPS (or the VPS's own key cannot fetch the repo; check which step failed); `sudo: a password is required` means the deployment account lacks non-interactive sudo; local changes mean you need to preserve the edits in `/opt/boat` before deployment can proceed.

GitHub reference: [repository secrets](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets). SSH action reference: [SSH action inputs](https://github.com/appleboy/ssh-action).

Reference documentation: [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage), [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html), [systemd execution settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html), and [the boat reservation conditions](https://eipro.jp/takachiho1/terms/view/WPT00000006).
