# Quickzi — Munich ops platform

A 24/7 control room for Quickzi's Munich delivery operation. The server pulls the truth from MotionTools'
API every 30 seconds — every active order with its full timeline, every rider with online status and GPS —
and writes everything to SQLite on the Railway volume. Nothing depends on anyone watching: while you
sleep it keeps recording, and at 04:05 it freezes the daily report of the evening before.

## Tabs
- **Live** — city pulse, "needs action now" with Call / WhatsApp / Call restaurant / Map / Story / Snooze / Handled,
  every live order with its phase timeline and PTOD clock, riders (busy / idle / offline).
- **Orders** — every order of the day (live, delivered, cancelled), searchable. Tap → the order's story:
  minutes per phase, alerts raised, GPS route driven.
- **Riders** — hours online, deliveries, % within target, avg PTOD, **delivery minutes per order**, busy %
  (share of online time on an order), orders/hour, idle time, minutes per phase, km/order, double orders, alerts.
  Tap a rider → their numbers vs the team + their orders.
- **Insights** — where the minutes go, focus list, **staffing hour by hour (orders vs rider-hours online)**,
  restaurants by rider wait, districts by postcode, late orders with the phase that caused it, alert log.
- **Daily report** — 14-day trend, frozen report per operating day (04:00 → 04:00), team briefing text to copy
  into WhatsApp, riders / restaurants / hours tables, CSV.
- **Settings** — all alert thresholds editable live; system panel (sync status, log, raw MotionTools samples).

## Alerts (PTOD clock starts at dispatch; thresholds editable in Settings)
No rider (5 min) · accepted but not started (3 min) · not moving (4 min) · wrong direction (400 m) ·
late to restaurant / customer (5 min behind ETA) · waiting at restaurant (8 min) · waiting at customer (5 min) ·
PTOD at risk (25 min or ETA projects > 30) / breached (30 min) · rider offline with an order.

## Files
`app.py` server · `mt.py` MotionTools client · `orders.py` phases + alert rules · `store.py` SQLite + analytics ·
`dashboard.html` UI · `simulate.py` full run against a fake MotionTools evening · `requirements.txt` · `Procfile`

## Railway variables
`MT_API_TOKEN`, `DASHBOARD_PASSWORD`, `WEBHOOK_PATH_SECRET`, `DATA_DIR=/data`,
`MUNICH_SERVICE_AREA_ID` (comma-separated area ids; empty = all). Optional: `SYNC_SECONDS` (30), `CITY_NAME`.

## Pages
`/dashboard` (any username + DASHBOARD_PASSWORD) · `/export.csv?period=today|yesterday|week|month|YYYY-MM-DD` ·
`/health` (no login) · `/api/system`, `/api/settings`, `/api/daily?day=` (login).

## MotionTools webhook (optional — makes updates instant)
`https://<railway-url>/mt/<WEBHOOK_PATH_SECRET>`; without it the dashboard still refreshes every 30 s from the API.

## Test locally
```
pip install -r requirements.txt
DASHBOARD_PASSWORD=test python3 simulate.py     # http://127.0.0.1:8020/dashboard
```
