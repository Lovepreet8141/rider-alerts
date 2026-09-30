"""Simulated Munich evening with 4 riders to test every alert type and the database.

Run:  python3 simulate.py
Writes a throwaway demo.db (deleted at the end) — the live service uses its own database.

Scenarios (minute 0 = order assigned to the rider, PTOD target 30 min):
  Ali    perfect run                                   -> no alerts, PTOD ~14 min
  Marco  rides AWAY from the restaurant, then turns    -> "Wrong direction", then "Not heading to restaurant"
  Sven   stuck at the restaurant 18 min                -> "Waiting at restaurant", then "PTOD breached"
  Deniz  parks on the way for 12 min                   -> "Not moving" (not also "wrong way")
"""
import math
import os
from datetime import datetime, timedelta

from detector import UTC, Config, Detector, Stop
from store import Store

DB = "demo.db"
if os.path.exists(DB):
    os.remove(DB)
store = Store(DB)
T0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=45)
m = lambda n: T0 + timedelta(minutes=n)


class PrintingStore:
    def open(self, key, info, now):
        print(f" min {int((now - T0).total_seconds() // 60):>2}  🔴 {info['rider']:<9} {info['headline']}")
        store.open(key, info, now)

    def update(self, key, info, now):
        store.update(key, info, now)

    def resolve(self, key, why, now):
        name = det.riders[key[0]].name
        print(f" min {int((now - T0).total_seconds() // 60):>2}  ✅ {name:<9} {key[1]} resolved: {why}")
        store.resolve(key, why, now)

    def delivery(self, driver_id, name, on_time, ptod, now):
        print(f" min {int((now - T0).total_seconds() // 60):>2}  📦 {name:<9} delivered, PTOD {ptod:.0f} min")
        store.delivery(driver_id, name, on_time, ptod, now)


det = Detector(Config(), PrintingStore())

# metres -> degrees (Munich latitude)
DLAT = 1 / 111_320
DLNG = 1 / (111_320 * math.cos(math.radians(48.14)))


def step(pos, target, metres):
    """Move `metres` from pos towards target (negative = away)."""
    dy, dx = (target[0] - pos[0]) / DLAT, (target[1] - pos[1]) / DLNG
    d = math.hypot(dx, dy)
    if d < 1e-6 or (metres > 0 and metres >= d):
        return target if metres > 0 else pos
    f = metres / d
    return (pos[0] + dy * f * DLAT, pos[1] + dx * f * DLNG)


riders = {
    #        name      start            restaurant        customer
    "d1": ("Ali K.",   (48.140, 11.570), (48.149, 11.570), (48.149, 11.590)),
    "d2": ("Marco R.", (48.130, 11.560), (48.139, 11.560), (48.139, 11.580)),
    "d3": ("Sven B.",  (48.120, 11.600), (48.129, 11.600), (48.129, 11.625)),
    "d4": ("Deniz A.", (48.150, 11.540), (48.159, 11.540), (48.159, 11.560)),
}
pos, phase = {}, {}
for did, (name, start, rest, cust) in riders.items():
    r = det.rider(did); r.name, r.phone = name, "+49 151 0000000"
    det.on_online(did, True, m(0))
    pos[did], phase[did] = start, "to_rest"
    det.on_stop_eta(did, Stop(f"{did}-p", kind="pickup", booking_ref=f"#{did.upper()}", address="Restaurant",
                              lat=rest[0], lng=rest[1]), m(0))
    det.on_stop_eta(did, Stop(f"{did}-d", kind="dropoff", booking_ref=f"#{did.upper()}", address="Customer",
                              lat=cust[0], lng=cust[1]), m(0))

SPEED = 250          # metres per minute on a bike
arrived_at = {}

for minute in range(0, 45):
    now = m(minute)
    for did, (name, start, rest, cust) in riders.items():
        ph = phase[did]
        if ph == "done":
            continue
        target = rest if ph == "to_rest" else cust
        speed = SPEED
        if did == "d2" and minute < 6:
            speed = -SPEED                      # Marco rides the wrong way first
        if did == "d4" and 3 <= minute < 15:
            speed = 0                           # Deniz parks
        if ph in ("to_rest", "to_cust"):
            pos[did] = step(pos[did], target, speed) if speed else pos[did]
            det.on_location(did, pos[did][0], pos[did][1], now)
            if pos[did] == target:
                stop = f"{did}-p" if ph == "to_rest" else f"{did}-d"
                det.on_stop_arrived(did, stop, now)
                arrived_at[did] = minute
                phase[did] = "at_rest" if ph == "to_rest" else "at_cust"
        else:
            det.on_location(did, pos[did][0], pos[did][1], now)   # phone keeps sending GPS while waiting
        if ph == "at_rest":
            wait = 18 if did == "d3" else 3        # Sven waits 18 min for the food
            if minute - arrived_at[did] >= wait:
                det.on_stop_completed(did, f"{did}-p", now)
                phase[did] = "to_cust"
        elif ph == "at_cust" and minute - arrived_at[did] >= 1:
            det.on_stop_completed(did, f"{did}-d", now)
            phase[did] = "done"
    det.check(now)

print("\nRider performance (today):")
for r in store.performance("today", m(45)):
    pct = f"{r['on_time_pct']}%" if r["on_time_pct"] is not None else "–"
    print(f"  {r['rider']:<9} deliveries {r['deliveries']}  ≤30 min {pct:>4}  avg PTOD {r['avg_ptod']} min  "
          f"alerts {r['issues']} (wrong way {r['off_route']}, waiting {r['wait']}, PTOD {r['ptod']}, "
          f"stopped {r['stationary']})")
os.remove(DB)
