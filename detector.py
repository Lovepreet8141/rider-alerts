"""Quickzi rider alert logic.

Checks, per rider on an active tour:
  gps         no location update for a while (app closed / phone off)
  stationary  not moving while not at a stop
  off_route   moving AWAY from the next stop, or making no progress towards it
  wait        waiting too long at the restaurant (pickup) or at the customer (dropoff)
  late        stop's own deadline (latest_arrival_at) passed or predicted to be missed
  ptod        order will exceed / has exceeded the 30-minute target
              (PTOD clock starts when the order is assigned to the rider)

All decisions take an explicit `now` so the logic can be tested with a fake clock.
Alerts are shown on the managers' dashboard only; nothing is sent to riders.

The detector reports to a `sink` with four methods:
  open(key, info, now)   update(key, info, now)   resolve(key, why, now)
  delivery(driver_id, name, on_time, ptod_min, now)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc
try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    BERLIN = UTC


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Config:
    ptod_target_min: int = 30            # order must be delivered within this many minutes
    ptod_warn_min: int = 25              # warn if still not delivered after this many minutes
    late_threshold_min: int = 5          # stop ETA past its own deadline by more than this -> alert
    stationary_min: int = 8              # no real movement for this long -> alert
    stationary_radius_m: int = 100       # "not moving" = stayed within this radius
    gps_lost_min: int = 8                # no location update for this long -> alert
    wrong_way_m: int = 400               # got this much further from the next stop than the closest point so far
    no_progress_min: int = 6             # moving, but no closer to the next stop for this long
    arrived_radius_m: int = 150          # treat as "basically there" (no direction checks)
    wait_pickup_min: int = 10            # waiting at the restaurant longer than this -> alert
    wait_dropoff_min: int = 5            # waiting at the customer longer than this -> alert
    munich_service_area_id: Optional[str] = None  # None = track everyone


@dataclass
class Stop:
    stop_id: str
    kind: str = "dropoff"                # "pickup" (restaurant) or "dropoff" (customer)
    booking_ref: str = ""
    address: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    deadline: Optional[datetime] = None  # latest_arrival_at
    eta: Optional[datetime] = None
    assigned_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    done: bool = False

    @property
    def arrived(self) -> bool:
        return self.arrived_at is not None

    @property
    def place(self) -> str:
        return "restaurant" if self.kind == "pickup" else "customer"


@dataclass
class Rider:
    driver_id: str
    name: str = "Unknown rider"
    phone: str = ""
    service_area_id: Optional[str] = None
    online: bool = False
    on_tour: bool = False
    last_fix: Optional[tuple] = None            # (lat, lng, time)
    anchor: Optional[tuple] = None              # (lat, lng, time) start of "stationary" window
    stops: dict = field(default_factory=dict)   # stop_id -> Stop, in route order
    # progress towards the next stop
    target_id: Optional[str] = None
    dist: Optional[float] = None                # current distance to target (m)
    min_dist: Optional[float] = None            # closest the rider has been to target
    min_at: Optional[datetime] = None           # when that closest point was reached


class Detector:
    def __init__(self, cfg: Config, sink):
        self.cfg = cfg
        self.sink = sink
        self.riders: dict[str, Rider] = {}
        self.open_issues: dict[tuple, dict] = {}   # (driver_id, kind, ref) -> {since, last_alert}

    # ---------- helpers ----------
    def rider(self, driver_id: str) -> Rider:
        if driver_id not in self.riders:
            self.riders[driver_id] = Rider(driver_id)
        return self.riders[driver_id]

    def tracked(self, r: Rider) -> bool:
        areas = self.cfg.munich_service_area_id
        if not areas:
            return True
        allowed = {a.strip() for a in areas.split(",") if a.strip()}
        return r.service_area_id is None or r.service_area_id in allowed

    @staticmethod
    def next_stop(r: Rider) -> Optional[Stop]:
        """First unfinished stop; restaurants before customers of the same order."""
        pending = [s for s in r.stops.values() if not s.done]
        pending.sort(key=lambda s: 0 if s.kind == "pickup" else 1)
        return pending[0] if pending else None

    def _update_progress(self, r: Rider, now):
        s = self.next_stop(r)
        if not s or s.lat is None or r.last_fix is None:
            r.target_id = r.dist = r.min_dist = r.min_at = None
            return
        d = haversine_m(r.last_fix[0], r.last_fix[1], s.lat, s.lng)
        if r.target_id != s.stop_id:                    # new target -> fresh start
            r.target_id, r.min_dist, r.min_at = s.stop_id, d, now
        elif d < r.min_dist - 50:                       # real progress (ignore GPS jitter)
            r.min_dist, r.min_at = d, now
        r.dist = d

    # ---------- state updates from MotionTools events ----------
    def on_location(self, driver_id, lat, lng, now):
        r = self.rider(driver_id)
        r.last_fix = (lat, lng, now)
        if r.anchor is None or haversine_m(r.anchor[0], r.anchor[1], lat, lng) > self.cfg.stationary_radius_m:
            r.anchor = (lat, lng, now)          # rider moved -> restart stationary window
            self._resolve(r, "stationary", "", now, "moving again")
        self._resolve(r, "gps", "", now, "GPS back")
        self._update_progress(r, now)

    def on_stop_eta(self, driver_id, stop: Stop, now):
        r = self.rider(driver_id)
        s = r.stops.get(stop.stop_id)
        if s is None:
            stop.assigned_at = stop.assigned_at or now
            r.stops[stop.stop_id] = stop
            r.on_tour = True
        else:
            for f in ("booking_ref", "address", "deadline", "eta", "lat", "lng"):
                v = getattr(stop, f)
                if v:
                    setattr(s, f, v)
            if stop.kind == "pickup":
                s.kind = "pickup"
        self._update_progress(r, now)

    def on_stop_arrived(self, driver_id, stop_id, now):
        r = self.rider(driver_id)
        s = r.stops.setdefault(stop_id, Stop(stop_id, assigned_at=now))
        s.arrived_at = s.arrived_at or now
        r.anchor = None                        # waiting at a stop is not "stationary"
        self._resolve(r, "stationary", "", now, "arrived at stop")
        self._resolve(r, "off_route", "", now, f"arrived at {s.place}")

    def on_stop_completed(self, driver_id, stop_id, now, failed=False):
        r = self.rider(driver_id)
        s = r.stops.setdefault(stop_id, Stop(stop_id, assigned_at=now))
        s.arrived_at = s.arrived_at or now
        s.done = True
        if s.kind == "dropoff" and not failed and self.tracked(r):
            ptod = (now - s.assigned_at).total_seconds() / 60 if s.assigned_at else None
            if ptod is not None:
                on_time = ptod <= self.cfg.ptod_target_min
            else:
                on_time = s.deadline is None or now <= s.deadline
            self.sink.delivery(r.driver_id, r.name, on_time, ptod, now)
        why = "failed" if failed else ("picked up" if s.kind == "pickup" else "delivered")
        for kind in ("late", "ptod", "wait"):
            self._resolve(r, kind, stop_id, now, why)
        if all(x.done for x in r.stops.values()):
            r.on_tour = False
            r.stops.clear()
            for kind in ("gps", "stationary", "off_route"):
                self._resolve(r, kind, "", now, "tour finished")
        r.anchor = None
        self._update_progress(r, now)

    def on_online(self, driver_id, online: bool, now):
        r = self.rider(driver_id)
        r.online = online
        if not online:
            r.on_tour = False
            for kind in ("gps", "stationary", "off_route"):
                self._resolve(r, kind, "", now, "went offline")

    # ---------- periodic check (run every minute) ----------
    def check(self, now: datetime):
        c = self.cfg
        for r in self.riders.values():
            if not (self.tracked(r) and r.on_tour):
                continue
            at_stop = any(s.arrived and not s.done for s in r.stops.values())
            target = self.next_stop(r)

            # --- movement: GPS lost > stationary > off route (only the most important one) ---
            if r.last_fix and now - r.last_fix[2] >= timedelta(minutes=c.gps_lost_min):
                mins = int((now - r.last_fix[2]).total_seconds() // 60)
                self._raise(r, "gps", "", now, f"No GPS for {mins} min (app closed / phone off?)")
            elif not at_stop and r.anchor and now - r.anchor[2] >= timedelta(minutes=c.stationary_min):
                mins = int((now - r.anchor[2]).total_seconds() // 60)
                self._raise(r, "stationary", "", now, f"Not moving for {mins} min")
            elif (not at_stop and target and r.dist is not None and r.target_id == target.stop_id
                  and r.dist > c.arrived_radius_m):
                km = r.dist / 1000
                if r.dist > r.min_dist + c.wrong_way_m:
                    away = (r.dist - r.min_dist) / 1000
                    self._raise(r, "off_route", "", now,
                                f"Wrong direction — moving away from {target.place} "
                                f"({away:.1f} km further, now {km:.1f} km away)", target)
                elif (now - r.min_at >= timedelta(minutes=c.no_progress_min)
                      and r.anchor and now - r.anchor[2] < timedelta(minutes=3)):   # moving, not parked
                    mins = int((now - r.min_at).total_seconds() // 60)
                    self._raise(r, "off_route", "", now,
                                f"Not heading to {target.place} — no progress for {mins} min "
                                f"(still {km:.1f} km away)", target)
                else:
                    self._resolve(r, "off_route", "", now, f"heading to {target.place} again")

            for s in r.stops.values():
                if s.done:
                    continue
                # --- long wait at restaurant / customer ---
                if s.arrived:
                    limit = c.wait_pickup_min if s.kind == "pickup" else c.wait_dropoff_min
                    waited = int((now - s.arrived_at).total_seconds() // 60)
                    if waited >= limit:
                        self._raise(r, "wait", s.stop_id, now,
                                    f"Waiting at {s.place} for {waited} min", s)
                # --- PTOD 30-minute target (customer stops) ---
                if s.kind == "dropoff" and s.assigned_at:
                    elapsed = (now - s.assigned_at).total_seconds() / 60
                    if elapsed > c.ptod_target_min:
                        self._raise(r, "ptod", s.stop_id, now,
                                    f"PTOD breached — {int(elapsed)} min and not delivered "
                                    f"(target {c.ptod_target_min})", s)
                    else:
                        projected = ((s.eta - s.assigned_at).total_seconds() / 60
                                     if s.eta and not s.arrived else None)
                        if projected is not None and projected > c.ptod_target_min:
                            self._raise(r, "ptod", s.stop_id, now,
                                        f"PTOD at risk — projected {int(projected)} min "
                                        f"(target {c.ptod_target_min})", s)
                        elif elapsed >= c.ptod_warn_min:
                            self._raise(r, "ptod", s.stop_id, now,
                                        f"PTOD at risk — {int(elapsed)} min gone, not delivered yet "
                                        f"(target {c.ptod_target_min})", s)
                # --- stop's own deadline, if MotionTools has one ---
                if not s.arrived and s.deadline is not None:
                    if now > s.deadline:
                        mins = int((now - s.deadline).total_seconds() // 60)
                        self._raise(r, "late", s.stop_id, now, f"Late — deadline passed {mins} min ago", s)
                    elif s.eta and s.eta > s.deadline + timedelta(minutes=c.late_threshold_min):
                        mins = int((s.eta - s.deadline).total_seconds() // 60)
                        self._raise(r, "late", s.stop_id, now,
                                    f"Will be late — ETA {mins} min after deadline", s)

    # ---------- alert bookkeeping ----------
    def _info(self, r: Rider, kind, headline, stop: Optional[Stop]):
        stop = stop or self.next_stop(r)
        return {
            "driver_id": r.driver_id, "rider": r.name, "phone": r.phone, "kind": kind,
            "headline": headline,
            "order_ref": stop.booking_ref if stop else "",
            "address": (f"{stop.place.title()}: {stop.address}" if stop and stop.address else ""),
            "due": stop.deadline.isoformat() if stop and stop.deadline else None,
            "map_url": (f"https://maps.google.com/?q={r.last_fix[0]:.5f},{r.last_fix[1]:.5f}"
                        if r.last_fix else ""),
        }

    def _raise(self, r: Rider, kind, ref, now, headline, stop: Optional[Stop] = None):
        key = (r.driver_id, kind, ref)
        issue = self.open_issues.get(key)
        if issue is None:
            self.open_issues[key] = {"since": now, "last_alert": now}
            self.sink.open(key, self._info(r, kind, headline, stop), now)
        elif now - issue["last_alert"] >= timedelta(minutes=1):
            # keep the headline on the dashboard current (e.g. "Waiting at restaurant for 14 min")
            issue["last_alert"] = now
            self.sink.update(key, self._info(r, kind, headline, stop), now)

    def _resolve(self, r: Rider, kind, ref, now, why):
        key = (r.driver_id, kind, ref)
        if self.open_issues.pop(key, None):
            self.sink.resolve(key, why, now)
