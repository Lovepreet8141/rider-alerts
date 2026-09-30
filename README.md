# Quickzi — Munich ops platform (v3.2)

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
  The moment a list endpoint opens (`/api/bookings/active`, `/api/bookings` or legacy `/api/hailing/bookings`)
  the server switches back to API mode on its own — no redeploy.

## Tabs
- **Live** — city pulse, "needs action now" with Call / WhatsApp / Call restaurant / Map / Story / Snooze / Handled,
  every live order with its phase timeline and PTOD clock, riders (busy / idle / offline).
- **Orders** — every order of the day (live, delivered, cancelled), searchable. Tap → the order's story:
  minutes per phase, alerts raised, GPS route driven.
- **Riders** — hours online, deliveries, % within target, avg PTOD, delivery minutes per order, busy %,
  orders/hour, idle time, minutes per phase, km/order, double orders, alerts. Tap a rider → numbers vs the team.
- **Insights** — where the minutes go, focus list, staffing hour by hour, restaurants by rider wait,
  districts by postcode, late orders with the phase that caused it, alert log.
- **Daily report** — 14-day trend, frozen report per operating day (04:00 → 04:00), team briefing text, CSV.
- **Settings** — alert thresholds; restaurant names (by MotionTools place id); rider phone numbers;
  system panel: mode, event counts, **which MotionTools endpoints are open**, log, raw samples.

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
`/health` (no login) · `/api/system`, `/api/settings`, `/api/riders`, `/api/places`, `/api/probe` (login).

## Test locally
```
pip install -r requirements.txt
DASHBOARD_PASSWORD=test python3 simulate.py                       # API mode        http://127.0.0.1:8020/dashboard
DASHBOARD_PASSWORD=test SIM_OPEN=detail python3 simulate_webhook.py   # restricted mode http://127.0.0.1:8021/dashboard
#   SIM_OPEN=none (everything restricted) · detail (only detail endpoints open) · all (nothing restricted)
```
