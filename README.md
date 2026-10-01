# Quickzi — Munich ops platform (v4.4)

A 24/7 control room for Quickzi's Munich delivery operation. Everything is written to SQLite on the Railway
volume, so nothing depends on anyone watching: while you sleep it keeps recording, and at 04:05 it freezes the
daily report of the evening before.

## Two ways to get the truth from MotionTools — chosen automatically
- **API mode** — the token may read bookings: every 30 s all active orders (full timeline) + all riders (online, GPS).
- **Webhook mode** — MotionTools has the account in *restricted API mode* (list endpoints answer 403
  `restricted_endpoint`). Orders are then rebuilt from the events MotionTools pushes to the webhook.
  On top of that the server **probes every endpoint itself** (startup, hourly, and the *Re-check now* button in
  Settings) and uses whatever is still open:
  - `GET /api/bookings/{id}` open → restaurant, address, phones, ETAs and rider GPS are filled in automatically
    (every new order, and every live order once a minute).
  - `GET /api/users/{id}` open → rider names + phone numbers.
  - `GET /api/places/{id}` open → restaurant names.
  - nothing open → the dashboard still works on order ids; restaurant names and rider phones can be typed in Settings.
  Restricted accounts get a small **hourly quota** per endpoint (429 `restricted_rate_limit`): the client stops
  calling that endpoint until the next hour and spends the quota on dispatched orders with open alerts first.
  Paths that answer 404 on this tenant (the documented `/api/bookings…` ones) are never asked again.
  The moment a list endpoint opens the server switches back to API mode on its own — no redeploy.
  On startup, every order of the last 7 days (live and delivered) is rebuilt by replaying the stored raw events —
  dispatch moment, rider history, timestamps — so reports made before an upgrade are correct too.

## Tabs
- **Live** — city pulse, **PTOD watch** (live orders at/over the warning time, longest first), the stage board
  (alerts shown on the order cards; tap a card for the story with Call / Map / Snooze / Handled), riders.
- **Orders** — every order of the day, searchable, with filters (Live · PTOD risk · Late · Late without reason ·
  On hold · Delivered · Cancelled). Tap → the order's story: minutes per phase, every rider, alerts, route, and a
  **reason box** ("Restaurant late", "No rider available", …) — reasons are counted in Insights and the daily brief.
- **Riders** — hours online, deliveries, % within target, avg PTOD, delivery minutes per order, busy %,
  orders/hour, idle time, minutes per phase, km/order, double orders, alerts. Tap a rider → numbers vs the team.
- **Insights** — where the minutes go, focus list, staffing hour by hour, restaurants by rider wait (each row shows
  its order numbers; tap → every order of that restaurant with wait/PTOD/alerts, and a box to name an unnamed
  MotionTools place — the name is applied to all its past orders too), districts by postcode, late orders, alert log.
- **Daily report** — 14-day trend, frozen report per operating day (04:00 → 04:00), team briefing text, CSV.
- **Settings** — alert thresholds; restaurant names (by MotionTools place id); rider phone numbers;
  system panel: mode, event counts, **which MotionTools endpoints are open**, log, raw samples.

## Order stages (Live tab board, always in this order)
Waiting for rider · Accepted, not started · Riding to restaurant · At restaurant · Delivering · At customer · **On hold**.
On hold = created by Lieferando but not yet dispatched by MotionTools (pre-orders, often hours ahead): no PTOD, no alerts.
**PTOD starts when the order is released to riders**, not at creation. MotionTools' "pickable" event is the exact
moment; when it is not sent, the release is derived from the planned delivery time (ETA − *release lead*, the
MotionTools automatic-scheduling setting, editable under Settings, default 45 min). ASAP orders are released at creation.
Alerts resolve themselves when the condition ends or the order is delivered/cancelled; a live order without any
MotionTools event for 3 h is closed automatically ("Closed (no events)") so nothing stays stuck on the board.
GPS-based alerts (not moving / no GPS / wrong direction) only fire for riders we actually receive positions for.

**Double orders** (two bookings in one tour): each order keeps its own PTOD clock from its own dispatch. The order
whose next stop has the earliest ETA is the one the rider is doing now; the other is *queued* — shown on the card
("double with X — rider is doing X first") and exempt from riding/waiting alerts until it is the current one.
**Redispatched orders**: every order keeps a rider-by-rider history — accepted by A, arrived at restaurant (A),
handed back by A, accepted by B, picked up (B), delivered (B)… The story shows all of it, the card says
"redispatched 2×", the Riders tab counts hand-backs per rider, and the phase minutes belong to the rider who
actually delivered (a hand-back resets accepted/started/arrived for the next rider).

## Alerts (PTOD clock starts at dispatch; thresholds editable in Settings)
No rider (5 min) · accepted but not started (3 min) · not moving / no GPS (4 min) · wrong direction (400 m) ·
late to restaurant / customer (5 min behind ETA) · waiting at restaurant (8 min) · waiting at customer (5 min) ·
PTOD at risk (25 min or ETA projects > 30) / breached (30 min) · rider offline with an order.

## Files
`app.py` server · `mt.py` MotionTools client (multi-path, self-probing) · `events.py` webhook projector ·
`orders.py` phases + alert rules · `store.py` SQLite + analytics · `dashboard.html` UI ·
`simulate.py` API-mode evening · `simulate_webhook.py` restricted-mode evening (fake MotionTools server) ·
`requirements.txt` · `Procfile`

## Railway variables
`MT_API_TOKEN`, `DASHBOARD_PASSWORD`, `WEBHOOK_PATH_SECRET`, `DATA_DIR=/data`,
`MUNICH_SERVICE_AREA_ID` (comma-separated area ids; empty = all). Optional: `SYNC_SECONDS` (30), `CITY_NAME`.

## MotionTools webhooks (required in webhook mode)
Endpoint `https://<railway-url>/mt/<WEBHOOK_PATH_SECRET>`.
1. "Munich Rider Alerts": booking.created, booking.transition, booking.in_progress, booking.stop_arrived,
   booking.stop_completed, booking.stop_failed, booking.etas_recalculated, driver.online, driver.offline,
   tour.created, tour.transition (+ driver.busy if offered).
2. "Munich GPS": same endpoint, filter customer_id = the Lieferando customer, only booking.driver_location_updated.

## Pages
`/dashboard` (any username + DASHBOARD_PASSWORD) · `/export.csv?period=today|yesterday|week|month|YYYY-MM-DD` ·
`/health` (no login) · `/api/system`, `/api/settings`, `/api/riders`, `/api/places`, `/api/probe`,
`/export-events.jsonl` (last raw webhook events, for debugging) (login).

## Test locally
```
pip install -r requirements.txt
DASHBOARD_PASSWORD=test python3 simulate.py                       # API mode        http://127.0.0.1:8020/dashboard
DASHBOARD_PASSWORD=test SIM_OPEN=detail python3 simulate_webhook.py   # restricted mode http://127.0.0.1:8021/dashboard
#   SIM_OPEN=none (everything restricted) · detail (only detail endpoints open) · all (nothing restricted)
```
