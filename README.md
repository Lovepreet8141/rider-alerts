# Quickzi — Munich rider dashboard

This is a live dashboard for your dispatchers. It shows which Munich riders need a call right now and how each rider performs over time. Nothing is sent to riders.

## What it alerts on
| Alert | When |
|---|---|
| **PTOD** | Order still not delivered after 25 min, a projected delivery time over 30 min, or the order has gone past 30 min |
| **Wrong way** | Rider moves 400 m+ further away from the restaurant or customer than they already were |
| **Wrong way (no progress)** | Rider is moving but hasn't got any closer to the next stop for 6 min |
| **Waiting** | Rider has been at the restaurant 10+ min, or at the customer 5+ min |
| **Not moving** | Rider stays within 100 m for 8+ min while not at a stop |
| **No GPS** | No location update for 8+ min (app closed or phone off) |
| **Late** | The stop's own MotionTools deadline has passed or is predicted to be missed |

The **PTOD clock** starts when the order is assigned to the rider in MotionTools and stops at delivery. The target is 30 min. All thresholds can be changed in `Config` in `detector.py`.

## Dashboard
Open `https://<your-url>/dashboard`. The browser asks for a username and password: type anything as the username, and use your `DASHBOARD_PASSWORD` as the password.
- **Live alerts:** open problems appear at the top, each with Call, WhatsApp, and Map buttons. Resolved problems stay visible for 30 min.
- **Refresh and sound:** the page refreshes every 30 seconds. Tap "Sound off" once to switch on a beep for new alerts.
- **Rider performance:** shows today, this week, or this month, with deliveries, % delivered in ≤30 min, average PTOD, and alert counts. Tap a rider to see their last 30 days.
- **On your phone:** use "Add to Home Screen" to open it like an app.

## Deploy on Railway
1. Upload all files to a **private GitHub repo**:
   - `app.py`, `detector.py`, `store.py`, `dashboard.html`
   - `simulate.py`, `requirements.txt`, `Procfile`, `README.md`
2. In Railway, choose **New Project**, then **Deploy from GitHub repo**.
3. Add a **Volume** mounted at `/data`. This keeps the performance history when Railway restarts or redeploys.
4. Under **Variables**, add:
   - `MT_API_TOKEN`: your Rider-Alerts token
   - `DASHBOARD_PASSWORD`: a password you choose
   - `WEBHOOK_PATH_SECRET`: a long random word
   - `DATA_DIR`: `/data`
   - `MUNICH_SERVICE_AREA_ID`: optional. Leave it empty to track all riders.
5. Go to **Settings → Networking → Generate Domain**. Check that `https://<your-url>/health` shows `"ok": true`.

## MotionTools webhook
- **Name:** `Munich Rider Alerts`
- **Endpoint URL:** `https://<your-url>/mt/<WEBHOOK_PATH_SECRET>`
- **Filters:** leave empty.
- **Events:**
  - `booking.etas_recalculated`, `booking.stop_arrived`, `booking.stop_completed`, `booking.stop_failed`
  - `driver.online`, `driver.offline`, `driver.service_area_changed`
  - `tour.created`, `tour.driver_location_updated`, `tour.etas_recalculated`, `tour.force_assigned`, `tour.modified`
- Switch on **Active**.

## After the first live events
Every raw event is saved to `events.jsonl` in the data folder. MotionTools' exact payload fields aren't fully public, so check a few real events. Then adjust the field paths in the `dig(...)` calls in `app.py` if needed. The most important fields are:
- the stop type (pickup or dropoff)
- the stop coordinates
- the driver ID
- the location lat/lng

## Test without MotionTools
```
python3 simulate.py
```
This runs a simulated Munich evening with 4 riders and prints every alert and the performance table. It covers wrong direction, waiting at the restaurant, not moving, and PTOD.
