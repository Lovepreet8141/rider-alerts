"""Test WEBHOOK mode: MotionTools API answers 403 (restricted), orders come only from webhook events.

    DASHBOARD_PASSWORD=test python3 simulate_webhook.py     -> http://127.0.0.1:8021/dashboard
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import json
import urllib.request
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="quickzi-wh-"))
os.environ.setdefault("MT_API_TOKEN", "fake")
os.environ.setdefault("WEBHOOK_PATH_SECRET", "abc")
os.environ.setdefault("SYNC_SECONDS", "5")

import app as appmod  # noqa: E402


class RestrictedMT:
    enabled = True
    stats = {"calls": 0, "errors": 0, "last_status": 403, "bookings_filter_mode": None, "drivers_filter_mode": None,
             "last_error": '/api/hailing/bookings -> 403 {"error_code":"restricted_endpoint","description":"Your account is in restricted API mode"}'}

    async def list_bookings(self, *a, **k):
        self.stats["calls"] += 1; self.stats["errors"] += 1
        return None

    async def get_booking(self, *a):
        return None

    async def list_drivers(self, *a):
        self.stats["calls"] += 1; self.stats["errors"] += 1
        return None


appmod.mt = RestrictedMT()
NOW = datetime.now(UTC)
m = lambda n: (NOW + timedelta(minutes=n)).isoformat(timespec="seconds")
AREA = "0d0bc288-f92a-4c34-a2cd-725838be6619"
CUST = "8e5b7276-7796-45b9-a431-8067c4dcbc6e"
PLACE_BK, PLACE_CHO = "7a1c-burgerking", "9f2e-choque"


def ev(rtype, event_name, t, **data):
    return {"id": "e", "timestamp": t, "resource_type": rtype, "event": event_name, "data": {"service_area_id": AREA, **data}}


def order_events(ref, bid, place, rider_id, rider, t0, wait_min=4, deliver=True, gps=True):
    p1, d1 = f"{bid}-p", f"{bid}-d"
    out = [ev("booking", "created", m(t0), booking_id=bid, external_id=ref, customer_id=CUST, place_ids=[place], status="to_be_dispatched"),
           ev("booking", "transition", m(t0 + 0.5), booking_id=bid, external_id=ref, **{"from": "to_be_dispatched", "to": "dispatched", "event": "dispatch"}),
           ev("booking", "etas_recalculated", m(t0 + 1), booking_id=bid, external_id=ref, customer_id=CUST,
              unfinished_stops_info=[{"id": p1, "position": 1, "type": "pickup", "eta": m(t0 + 8)}, {"id": d1, "position": 2, "type": "dropoff", "eta": m(t0 + 22)}])]
    if rider_id:
        out += [ev("driver", "busy", m(t0 + 2), driver_id=rider_id),
                ev("booking", "in_progress", m(t0 + 3), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider, driver_location={"lat": 48.14, "lng": 11.56})]
        if gps:
            out += [ev("booking", "driver_location_updated", m(t0 + 3 + k), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider,
                       driver_location={"lat": 48.14 + k * 0.002, "lng": 11.56 + k * 0.002}) for k in range(1, 6)]
        out += [ev("booking", "stop_arrived", m(t0 + 9), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider, stop_id=p1, stop_type="pickup", stop_position=1)]
        if wait_min is not None:
            out += [ev("booking", "stop_completed", m(t0 + 9 + wait_min), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider, stop_id=p1, stop_type="pickup", stop_position=1)]
            if deliver:
                out += [ev("booking", "stop_arrived", m(t0 + 9 + wait_min + 9), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider, stop_id=d1, stop_type="dropoff", stop_position=2),
                        ev("booking", "stop_completed", m(t0 + 9 + wait_min + 11), booking_id=bid, external_id=ref, driver_id=rider_id, driver_name=rider, stop_id=d1, stop_type="dropoff", stop_position=2),
                        ev("booking", "transition", m(t0 + 9 + wait_min + 11), booking_id=bid, external_id=ref, **{"from": "dispatched", "to": "done", "event": "complete_stops"})]
    return out


EVENTS = [ev("driver", "online", m(-90), driver_id="r-ahmad", profile={"first_name": "Ahmad", "last_name": "Sabe", "phone_number": "+49 151 1"}, location={"lat": 48.14, "lng": 11.56}),
          ev("driver", "online", m(-90), driver_id="r-murat", profile={"first_name": "Murat", "last_name": "K."}),
          ev("driver", "online", m(-80), driver_id="r-obaida", profile={"first_name": "Obaida", "last_name": "H."}),
          ev("driver", "online", m(-60), driver_id="r-sven", profile={"first_name": "Sven", "last_name": "B."})]
# delivered earlier tonight
EVENTS += order_events("D1", "b-D1", PLACE_BK, "r-ahmad", "Ahmad Sabe", -75, wait_min=13)
EVENTS += order_events("D2", "b-D2", PLACE_CHO, "r-murat", "Murat K.", -70, wait_min=3)
EVENTS += order_events("D3", "b-D3", PLACE_BK, "r-obaida", "Obaida H.", -62, wait_min=11)
EVENTS += order_events("D4", "b-D4", PLACE_CHO, "r-ahmad", "Ahmad Sabe", -48, wait_min=2)
# live now: Murat waiting at restaurant 12 min, Obaida delivering, new order without rider 7 min, Sven accepted (busy) but not started
EVENTS += order_events("PMCHP6", "b-live1", PLACE_CHO, "r-murat", "Murat K.", -21, wait_min=None)
EVENTS += order_events("C3GKJH", "b-live2", PLACE_BK, "r-obaida", "Obaida H.", -26, wait_min=5, deliver=False)
EVENTS += order_events("Q4VJ9M", "b-live3", PLACE_BK, None, "", -7)
EVENTS += [ev("booking", "created", m(-6), booking_id="b-live4", external_id="KMW86T", customer_id=CUST, place_ids=[PLACE_CHO], status="to_be_dispatched"),
           ev("tour", "created", m(-5.5), tour_id="t-9", dispatched_booking_ids=["b-live4"], status="pickable"),
           ev("tour", "transition", m(-4), tour_id="t-9", **{"from": "pickable", "to": "claimed", "event": "claim"}, affected_user_ids=["r-sven"])]
EVENTS.sort(key=lambda e: e["timestamp"])


def post_all():
    time.sleep(3)
    for e in EVENTS:
        req = urllib.request.Request("http://127.0.0.1:8021/mt/abc", data=json.dumps(e).encode(), headers={"content-type": "application/json"})
        urllib.request.urlopen(req)
    print(f"posted {len(EVENTS)} events")


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=post_all, daemon=True).start()
    print("Dashboard: http://127.0.0.1:8021/dashboard  (password: test)")
    uvicorn.run(appmod.app, host="127.0.0.1", port=8021, log_level="warning")
