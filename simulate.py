"""Run the dashboard against a FAKE MotionTools with a realistic Munich evening.

    DASHBOARD_PASSWORD=test python3 simulate.py        -> http://127.0.0.1:8020/dashboard (user: any, pw: test)

Bookings and riders are generated in MotionTools' exact JSON shape (see docs.motiontools.io),
so this exercises the same parsing, alert rules, storage and dashboard as production.
"""
from __future__ import annotations

import os
import random
import tempfile
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
os.environ.setdefault("DASHBOARD_PASSWORD", "test")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="quickzi-sim-"))
os.environ.setdefault("MT_API_TOKEN", "fake")
os.environ.setdefault("SYNC_SECONDS", "10")

import app as appmod  # noqa: E402  (after env is set)

NOW = datetime.now(UTC)
m = lambda n: NOW + timedelta(minutes=n)
iso = lambda dt: dt.isoformat(timespec="seconds")
AREA = {"id": "0d0bc288-f92a-4c34-a2cd-725838be6619", "name": "München"}
random.seed(7)

RESTAURANTS = [("Burger King Freiham", 48.147, 11.428, "+49 89 1111111"), ("Cho Que Harras", 48.126, 11.539, "+49 89 2222222"),
               ("Da Antonio Ristorantino", 48.135, 11.502, ""), ("Burgerbae", 48.138, 11.560, "+49 89 4444444"),
               ("Sushi Nami", 48.151, 11.575, "")]
RIDERS = {"r-ahmad": ("Ahmad", "Sabe", "+49 151 1000001"), "r-murat": ("Murat", "K.", "+49 151 1000002"),
          "r-obaida": ("Obaida", "H.", "+49 151 1000003"), "r-vinit": ("vinit", "P.", "+49 151 1000004"),
          "r-ahmed": ("Ahmed", "Fauzi", "+49 151 1000005"), "r-deniz": ("Deniz", "A.", "+49 151 1000006"),
          "r-sven": ("Sven", "B.", "+49 151 1000007"), "r-karan": ("Karan", "S.", "+49 151 1000008")}


def stop(kind, lat, lng, name="", phone="", arrived=None, done=None, eta=None, status=None):
    return {"id": f"s-{random.randrange(10**9)}", "type": kind, "lat": lat, "lng": lng,
            "street": "Musterstr.", "number": str(random.randrange(1, 120)), "city": "München",
            "zip_code": random.choice(["81249", "81241", "80689", "82166", "80687"]) if kind == "dropoff" else "81249",
            "location_name": name, "place": {"name": name} if name else None, "phone_number": phone,
            "arrived_at": iso(arrived) if arrived else None, "completed_at": iso(done) if done else None,
            "expected_arrival_at": iso(eta) if eta else None,
            "status": status or ("done" if done else "arrived" if arrived else "scheduled")}


def booking(ref, rider, rest, disp, acc=None, start=None, at_rest=None, picked=None, at_cust=None, delivered=None,
            eta_rest=None, eta_cust=None, loc=None, status=None):
    events = [{"name": "dispatched", "status": "dispatched", "timestamp": iso(disp)}]
    if acc:
        events.append({"name": "claimed", "status": "claimed", "timestamp": iso(acc)})
    if start:
        events.append({"name": "en_route", "status": "en_route", "timestamp": iso(start)})
    if delivered:
        events.append({"name": "done", "status": "done", "timestamp": iso(delivered)})
    st = status or ("done" if delivered else "en_route" if start else "claimed" if acc else "pickable")
    rname, rlat, rlng, rphone = rest
    drop_lat, drop_lng = rlat + random.uniform(-0.02, 0.02), rlng + random.uniform(-0.03, 0.03)
    d = {"id": f"b-{ref}", "external_id": ref, "status": st, "created_at": iso(disp - timedelta(minutes=1)),
         "scheduled_at": iso(disp), "done_at": iso(delivered) if delivered else None, "service_area": AREA,
         "driver": {"id": rider, "first_name": RIDERS[rider][0], "last_name": RIDERS[rider][1]} if rider else None,
         "driver_location": {"lat": loc[0], "lng": loc[1]} if loc else None, "events": events,
         "stops": [stop("pickup", rlat, rlng, rname, rphone, at_rest, picked, eta_rest),
                   stop("dropoff", drop_lat, drop_lng, "", "", at_cust, delivered, eta_cust)]}
    return d


# ---------------- live orders right now ----------------
BK, CHO, ANT, BAE, SUSHI = RESTAURANTS
ACTIVE = [
    booking("DDCP44", "r-ahmad", BK, m(-22), m(-20), m(-19), m(-14), m(-10), eta_cust=m(3), loc=(48.140, 11.470)),
    booking("PMCHP6", "r-murat", CHO, m(-20), m(-18), m(-17), m(-12), eta_cust=m(9), loc=(48.126, 11.539)),
    booking("Q4VJ9M", None, ANT, m(-7)),
    booking("C3GKJH", "r-obaida", BAE, m(-27), m(-25), m(-24), m(-16), m(-8), eta_cust=m(2), loc=(48.141, 11.566)),
    booking("64Y6WY", "r-obaida", BAE, m(-15), m(-14), m(-14), m(-16), m(-8), eta_cust=m(6), loc=(48.141, 11.566)),
    booking("KMW86T", "r-vinit", SUSHI, m(-9), m(-6), loc=(48.150, 11.580)),
    booking("TWHRFK", "r-ahmed", CHO, m(-24), m(-22), m(-21), m(-15), m(-12), m(-7), eta_cust=m(-8), loc=(48.120, 11.560)),
    booking("GC7Q3D", "r-deniz", BK, m(-13), m(-11), m(-10), eta_rest=m(-4), loc=(48.135, 11.480)),
]

# ---------------- delivered today (for the performance section) ----------------
DONE = []
hour_profile = [(17, 2), (18, 5), (19, 8), (20, 7), (21, 4), (22, 3)]
riders_cycle = ["r-ahmad", "r-murat", "r-obaida", "r-vinit", "r-ahmed", "r-deniz", "r-sven"]
i = 0
for hour, n in hour_profile:
    for k in range(n):
        rest = random.choice(RESTAURANTS)
        rider = riders_cycle[i % len(riders_cycle)]
        i += 1
        base = NOW.replace(hour=hour, minute=random.randrange(0, 59), second=0) - timedelta(hours=2)  # Berlin -> UTC
        accept = random.uniform(1, 7) if hour in (19, 20) else random.uniform(0.5, 3)
        to_rest = random.uniform(4, 10)
        wait = random.uniform(9, 16) if rest is BK else random.uniform(1, 6)
        to_cust = random.uniform(5, 12)
        hand = random.uniform(1, 4) if rider != "r-deniz" else random.uniform(4, 8)
        t = base
        acc = t + timedelta(minutes=accept); start = acc + timedelta(minutes=1)
        at_r = start + timedelta(minutes=to_rest); pk = at_r + timedelta(minutes=wait)
        at_c = pk + timedelta(minutes=to_cust); dl = at_c + timedelta(minutes=hand)
        DONE.append(booking(f"D{hour}{k:02d}", rider, rest, t, acc, start, at_r, pk, at_c, dl))

DRIVERS = []
online = {"r-ahmad", "r-murat", "r-obaida", "r-vinit", "r-ahmed", "r-deniz", "r-sven"}
for rid, (fn, ln, ph) in RIDERS.items():
    loc = next((b["driver_location"] for b in ACTIVE if b["driver"] and b["driver"]["id"] == rid and b["driver_location"]), None)
    DRIVERS.append({"id": rid, "role": "driver", "status": "online" if rid in online else "offline",
                    "profile": {"first_name": fn, "last_name": ln, "phone_number": ph},
                    "location": loc or {"lat": 48.137, "lng": 11.575}, "service_area": AREA,
                    "active_hailing_booking_ids": [b["id"] for b in ACTIVE if b["driver"] and b["driver"]["id"] == rid]})


class FakeMT:
    enabled = True
    stats = {"calls": 0, "errors": 0, "last_status": 200, "last_error": None, "bookings_filter_mode": "fake",
             "drivers_filter_mode": "fake"}

    async def list_bookings(self, area_ids, statuses, extra=None):
        self.stats["calls"] += 1
        return DONE if "done" in statuses else ACTIVE

    async def get_booking(self, bid):
        return None

    async def list_drivers(self, area_ids):
        return DRIVERS


appmod.mt = FakeMT()

# rider sessions: everyone online since 17:00 Berlin (so hours online / busy % / staffing per hour work)
from orders import iso as _iso
day0 = NOW.astimezone(appmod.BERLIN).replace(hour=17, minute=0, second=0, microsecond=0)
if day0 > NOW.astimezone(appmod.BERLIN):
    day0 -= timedelta(days=1)
for rid in online:
    appmod.store._exec("INSERT INTO rider_sessions (rider_id, online_at) VALUES (?,?)", (rid, _iso(day0.astimezone(UTC))))
    appmod.store._exec("INSERT INTO riders (id, name, phone, online, online_since, updated_at) VALUES (?,?,?,?,?,?)",
                       (rid, RIDERS[rid][0] + " " + RIDERS[rid][1], RIDERS[rid][2], 1, _iso(day0.astimezone(UTC)), _iso(NOW)))
# a GPS trail for Ahmad's live order (so the order story shows a route)
for k in range(12):
    appmod.store.record_position("r-ahmad", 48.147 - k * 0.0006, 11.428 + k * 0.0035, "b-DDCP44", m(-19 + k * 1.5))
# 13 earlier daily reports so the trend chart has history
import json as _json
for i in range(14, 1, -1):
    dk = (NOW - timedelta(days=i)).astimezone(appmod.BERLIN).strftime("%Y-%m-%d")
    n = random.randint(18, 40); w = random.randint(62, 94)
    appmod.store.save_daily(dk, {"day": dk, "delivered": n, "cancelled": random.randint(0, 3), "within_pct": w,
                                 "avg_ptod": round(random.uniform(24, 31), 1), "late": max(0, round(n * (100 - w) / 100)),
                                 "phases": {}, "riders": [], "restaurants": [], "hours": [], "focus": [], "alerts_total": 0,
                                 "handled": 0, "median_ptod": None, "p90_ptod": None, "target_within_pct": 90})
# Deniz has been standing still for 6 minutes on the way to the restaurant
for k in range(7):
    appmod.tracker.push("r-deniz", 48.135 + k * 1e-6, 11.480, m(-6 + k))

if __name__ == "__main__":
    import uvicorn
    print("Dashboard: http://127.0.0.1:8020/dashboard  (password: %s)" % os.environ["DASHBOARD_PASSWORD"])
    uvicorn.run(appmod.app, host="127.0.0.1", port=8020, log_level="warning")
