# Takachiho Gorge boat availability checker

For VPS monitoring with your existing Telegram bot, follow [DEPLOY.md](DEPLOY.md).
Pushes to `main` can deploy automatically through `.github/workflows/deploy.yml`;
configure only `VPS_HOST`, `VPS_USER` (`deploy`), and `VPS_SSH_KEY` using your existing deploy private key. SSH uses port 22. See section 8 of the deployment guide for setup.
`boat_notifier.py` checks once and sends alerts; the supplied systemd timer runs it
every two minutes. `configure_telegram.py` imports the bot token and selected chat
from `second_hand_notify` on the VPS. Successful alerts are remembered across
restarts, and failed sends can retry. The command-line checker below remains
available for standalone use.

Send `/boat_history` in the configured Telegram chat to see the five most recent
calendar check results, newest first. In a group, use
`/boat_history@YourBotUsername` if needed. This requires the updated
`second_hand_notify` bot: it holds a five-entry Python `deque` in memory and
receives each scheduled result on an authenticated localhost endpoint. History
clears when `pttbot` restarts; no check-history database or file is created.
See **Enable /boat_history on an existing VPS** in [DEPLOY.md](DEPLOY.md).

Python 3.10+; no third-party packages required. Run from this directory:

```sh
python3 check_boats.py
```

Defaults to **November 20, 2026**, for **start times from 13:00 through 16:00 inclusive, Japan time (UTC+9)**. A 16:00 ride ends at 16:30. If you need to finish by 16:00, use `--to 15:30`.

Repeat every five minutes until a bookable slot appears:

```sh
python3 check_boats.py --watch
```

The script prints each slot's status, sounds a terminal bell when availability is found (if your terminal supports it), prints the booking calendar link, and exits. Keep the process running and the computer awake. Stop with Ctrl+C. It does not submit a booking or hold a boat.

Other examples:

```sh
python3 check_boats.py --date 2026-11-20 --from 13:00 --to 15:30
python3 check_boats.py --watch --interval 120
python3 check_boats.py --json
python3 -m unittest -v
```

Exit codes: `0` = availability found; `1` = slots returned but none bookable; `2` = request/parsing failure or no matching slots; `130` = interrupted. JSON watch mode emits one JSON object per check, including error objects. Errors use increasing retry delays. The minimum polling interval is 60 seconds.

## Website analysis

Inspected on November 6, 2026:

- The [booking page](https://eipro.jp/takachiho1/eventCalendars/index) uses JavaScript to load the calendar. Its initial HTML does not contain availability.
- [Calendar JavaScript](https://eipro.jp/module/Front/js/app.page.EventCalendars.js) sends a read-only search using HTTP POST to `/takachiho1/eventCalendars/search` with form fields `data[conds][ServiceView][max_session_dateOver]`, `data[conds][ServiceView][min_session_dateUnder]`, `calendar_view_name=agendaDay`, and `calendar_type=day`. Dates use `YYYY/MM/DD`, with the requested date and following date as bounds.
- The response contains a `results` array with timezone-aware `start` and `end`, the website's `ordable` flag (that spelling is intentional), `order_remain_amount`, `is_full`, `is_reserve_started`, and `is_reserve_dead`. The checker requires an explicit bookable flag, positive remaining inventory, and consistent booking-state flags. It never computes remaining inventory from capacity or cancellations.
- The website explicitly recommends week/day view because month-view symbols can differ. The checker uses day view and filters the returned timestamps again locally.
- The live November 20 response contained 16 half-hour slots, from 08:30 to 16:00. All seven starts in the requested window were marked full, with `ordable=false` and zero remaining. This is a snapshot; cancellations may change it.
- [Reservation conditions](https://eipro.jp/takachiho1/terms/view/WPT00000006) state booking opens two weeks before the ride at 09:00 Japan time and closes two days before at 09:00. For November 20, this means November 6 at 09:00 through just before November 18 at 09:00 Japan time (08:00 Taiwan time). The [FAQ](https://eipro.jp/takachiho1/terms/view/WPT00000003) specifies the final booking minute as 08:59.

The endpoint worked without login or cookies during inspection. It is an undocumented website endpoint and may change. Invalid responses are reported as errors, and an empty result is not labeled sold out. Availability is a point-in-time check; complete your reservation on the official site.
