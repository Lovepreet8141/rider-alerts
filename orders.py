"""Order model, phase timeline and alert rules for the Quickzi Munich ops dashboard.

Everything is derived from MotionTools' booking JSON (GET /api/hailing/bookings) and the
riders list (GET /api/users?filters[role]=driver). No guessing from webhooks any more.

Order phases (PTOD clock starts at DISPATCH):
  dispatched -> accepted (rider claimed) -> started (en_route) -> at_restaurant -> picked_up
             -> at_customer -> delivered
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

UTC = timezone.utc
try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover
    BERLIN = UTC


# ---------------------------------------------------------------- config
@dataclass
class Rules:
    ptod_target_min: int = 30       # deliver within 30 min of dispatch
    ptod_warn_min: int = 25         # warn from here
    accept_limit_min: int = 5       # nobody accepted the order
    start_limit_min: int = 3        # accepted but rider hasn't started the tour
    stationary_min: int = 4         # rider should be riding but hasn't moved 100 m
    stationary_radius_m: int = 100
    wrong_way_m: int = 400          # got 400 m further from the next stop than the closest point so far
    late_grace_min: int = 5         # ETA to a stop passed by this much
    wait_restaurant_min: int = 8    # waiting at the restaurant
    wait_customer_min: int = 5      # waiting at the customer
    stale_gps_min: int = 6          # no GPS update while riding
    target_within_pct: int = 90     # goal: this % of orders within ptod_target_min

    EDITABLE = ("ptod_target_min", "ptod_warn_min", "accept_limit_min", "start_limit_min", "stationary_min",
                "wrong_way_m", "late_grace_min", "wait_restaurant_min", "wait_customer_min", "target_within_pct")

    def apply(self, values: dict):
        for k, v in values.items():
            if k in self.EDITABLE and v not in (None, ""):
                try:
                    setattr(self, k, int(v))
                except (TypeError, ValueError):
                    pass

    def as_dict(self):
        return {k: getattr(self, k) for k in self.EDITABLE}


# ---------------------------------------------------------------- helpers
def ts(v) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(UTC).isoformat(timespec="seconds") if dt else None


def mins(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 60, 1)


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _addr(stop: dict) -> str:
    street = " ".join(x for x in [stop.get("street"), str(stop.get("number") or "")] if x).strip()
    city = stop.get("city") or ""
    return ", ".join(x for x in [street, city] if x)


def _name(obj: Optional[dict]) -> str:
    if not obj:
        return ""
    return " ".join(x for x in [obj.get("first_name"), obj.get("last_name")] if x).strip() or obj.get("name", "")


# ---------------------------------------------------------------- parsing
ACTIVE = {"to_be_dispatched", "dispatched", "partially_dispatched", "pickable", "claimed", "en_route"}
UNASSIGNED = {"to_be_dispatched", "dispatched", "partially_dispatched", "pickable"}


def parse_booking(b: dict) -> dict:
    """Normalise one MotionTools booking into the order shape the dashboard uses."""
    stops = b.get("stops") or []
    pick = next((s for s in stops if s.get("type") == "pickup"), None) or (stops[0] if stops else {})
    drops = [s for s in stops if s.get("type") == "dropoff"]
    drop = drops[-1] if drops else (stops[-1] if len(stops) > 1 else {})
    events = b.get("events") or []

    def ev(*statuses):
        times = [ts(e.get("timestamp")) for e in events if e.get("status") in statuses]
        times = [t for t in times if t]
        return min(times) if times else None

    status = b.get("status") or ""
    created = ts(b.get("created_at"))
    # PTOD starts when MotionTools DISPATCHES the order (offers it to riders) — not when a pre-order is created
    # hours earlier and sits "on hold" (status to_be_dispatched).
    dispatched = ev("pickable", "claimed", "en_route")          # offered to riders / taken by a rider
    if dispatched is None and status not in ("to_be_dispatched", "dispatched", "partially_dispatched"):
        dispatched = created                                    # finished order without an event list
    scheduled = ts(b.get("scheduled_at") or b.get("scheduled_for") or pick.get("scheduled_at")
                   or pick.get("earliest_arrival_at") or pick.get("latest_arrival_at"))
    accepted = ev("claimed")
    started = ev("en_route")
    at_rest, picked = ts(pick.get("arrived_at")), ts(pick.get("completed_at"))
    at_cust, delivered = ts(drop.get("arrived_at")), ts(drop.get("completed_at"))
    if status in ("done", "paid", "processing_payment") and not delivered:
        delivered = ts(b.get("done_at")) or at_cust
    driver = b.get("driver") or {}
    loc = b.get("driver_location") or {}
    place = (pick.get("place") or {}).get("name")
    restaurant = place or pick.get("location_name") or _addr(pick) or "Restaurant"
    cancel_reason = ""
    for s_ in stops:
        fr = s_.get("failure_reason") or {}
        if fr.get("key") or fr.get("note"):
            cancel_reason = " ".join(x for x in [fr.get("key"), fr.get("note")] if x)

    if status == "cancelled":
        phase = "cancelled"
    elif delivered or status in ("done", "paid", "processing_payment"):
        phase = "delivered"
    elif dispatched is None:
        phase = "on_hold"
    elif not driver.get("id") or status in UNASSIGNED:
        phase = "unassigned"
    elif status == "claimed" and not started:
        phase = "accepted"
    elif at_cust:
        phase = "at_customer"
    elif picked:
        phase = "to_customer"
    elif at_rest:
        phase = "at_restaurant"
    else:
        phase = "to_restaurant"

    return {
        "id": b.get("id"), "ref": b.get("external_id") or (b.get("id") or "")[:8], "status": status, "phase": phase,
        "area": (b.get("service_area") or {}).get("id"), "area_name": (b.get("service_area") or {}).get("name"),
        "rider_id": driver.get("id"), "rider": _name(driver), "restaurant": restaurant,
        "restaurant_phone": pick.get("phone_number") or "", "customer_addr": _addr(drop),
        "customer_phone": drop.get("phone_number") or "",
        "pick_lat": pick.get("lat"), "pick_lng": pick.get("lng"), "drop_lat": drop.get("lat"), "drop_lng": drop.get("lng"),
        "rider_lat": loc.get("lat"), "rider_lng": loc.get("lng"),
        "eta_restaurant": ts(pick.get("expected_arrival_at")), "eta_customer": ts(drop.get("expected_arrival_at")),
        "pick_status": pick.get("status"), "drop_status": drop.get("status"),
        "created_at": created, "dispatched_at": dispatched, "scheduled_at": scheduled, "accepted_at": accepted, "started_at": started,
        "at_restaurant_at": at_rest, "picked_up_at": picked, "at_customer_at": at_cust, "delivered_at": delivered,
        "stops": len(stops), "customer_zip": str(drop.get("zip_code") or ""), "place_id": pick.get("place_id") or "",
        "cancel_reason": cancel_reason, "est_distance_m": b.get("total_estimated_distance_meters") if isinstance(b.get("total_estimated_distance_meters"), (int, float)) else None,
    }


def new_order(oid: str, ref: str = "", area: str = None, now: Optional[datetime] = None) -> dict:
    """Empty order in the same shape parse_booking() produces (used in webhook mode)."""
    return {"id": oid, "ref": ref or oid[:8], "status": "", "phase": "unassigned", "area": area, "area_name": None,
            "rider_id": None, "rider": "", "restaurant": "", "restaurant_phone": "", "customer_addr": "",
            "customer_phone": "", "pick_lat": None, "pick_lng": None, "drop_lat": None, "drop_lng": None,
            "rider_lat": None, "rider_lng": None, "eta_restaurant": None, "eta_customer": None,
            "pick_status": None, "drop_status": None, "created_at": now, "dispatched_at": now, "scheduled_at": None, "accepted_at": None,
            "started_at": None, "at_restaurant_at": None, "picked_up_at": None, "at_customer_at": None,
            "delivered_at": None, "stops": 0, "customer_zip": "", "place_id": "", "cancel_reason": "",
            "est_distance_m": None, "stop_types": {}, "partial": False}


def phase_from(o: dict) -> str:
    """Derive the phase from whatever timestamps we have (webhook mode)."""
    if o.get("cancelled"):
        return "cancelled"
    if o.get("closed_auto"):
        return "closed"
    if o["delivered_at"]:
        return "delivered"
    if o["at_customer_at"]:
        return "at_customer"
    if o["picked_up_at"]:
        return "to_customer"
    if o["at_restaurant_at"]:
        return "at_restaurant"
    if o["started_at"]:
        return "to_restaurant"
    if o["accepted_at"] or o["rider_id"]:
        return "accepted"
    if not o.get("dispatched_at"):
        return "on_hold"
    return "unassigned"


def phase_minutes(o: dict) -> dict:
    """Minutes spent in each phase (None when the phase hasn't happened)."""
    return {
        "to_accept": mins(o["dispatched_at"], o["accepted_at"]),
        "to_restaurant": mins(o["accepted_at"] or o["dispatched_at"], o["at_restaurant_at"]),
        "at_restaurant": mins(o["at_restaurant_at"], o["picked_up_at"]),
        "to_customer": mins(o["picked_up_at"], o["at_customer_at"]),
        "handover": mins(o["at_customer_at"], o["delivered_at"]),
        "ptod": mins(o["dispatched_at"], o["delivered_at"]),
    }


# ---------------------------------------------------------------- rider GPS tracking
class RiderTracker:
    """Keeps a short GPS history per rider from the polled positions."""

    def __init__(self):
        self.hist: dict[str, deque] = {}
        self.best: dict[tuple, tuple] = {}   # (rider, target_stop_key) -> (min_dist, at)
        self.pushes: dict[str, deque] = {}   # rider -> times a position was reported (even if unchanged)

    def push(self, rider_id: str, lat, lng, now: datetime):
        if rider_id is None or lat is None or lng is None:
            return
        p = self.pushes.setdefault(rider_id, deque(maxlen=200))
        p.append(now)
        h = self.hist.setdefault(rider_id, deque(maxlen=60))
        if h and h[-1][1] == lat and h[-1][2] == lng:
            return                                    # identical fix = no new information
        h.append((now, float(lat), float(lng)))
        while h and now - h[0][0] > timedelta(minutes=20):
            h.popleft()

    def last_fix(self, rider_id):
        h = self.hist.get(rider_id)
        return h[-1] if h else None

    def has_feed(self, rider_id: str, now: datetime, minutes: int = 15, min_reports: int = 3) -> bool:
        """True when we really receive positions for this rider (API polling or the GPS webhook).
        Without a feed, "not moving" / "no GPS" / "wrong direction" would be guesses — so they stay silent."""
        p = self.pushes.get(rider_id)
        if not p:
            return False
        cutoff = now - timedelta(minutes=minutes)
        return sum(1 for t in p if t >= cutoff) >= min_reports

    def stationary_minutes(self, rider_id: str, now: datetime, radius_m: int) -> Optional[float]:
        """How long the rider has stayed within radius_m of the latest position (None = unknown)."""
        h = self.hist.get(rider_id)
        if not h:
            return None
        t_last, la, ln = h[-1]
        since = t_last
        for t, a, b in reversed(h):
            if haversine_m(la, ln, a, b) > radius_m:
                break
            since = t
        # if the last fix is old, the rider has been "still" since then too
        return round((now - min(since, t_last)).total_seconds() / 60, 1)

    def wrong_way(self, rider_id: str, key: str, tlat, tlng, now: datetime, limit_m: int) -> Optional[float]:
        """Metres further from the target than the closest point reached in this leg (None if fine)."""
        fix = self.last_fix(rider_id)
        if not fix or tlat is None or tlng is None:
            return None
        d = haversine_m(fix[1], fix[2], tlat, tlng)
        best = self.best.get((rider_id, key))
        if best is None or d < best[0] - 30:
            self.best[(rider_id, key)] = (d, now)
            return None
        if d - best[0] >= limit_m:
            return round(d - best[0])
        return None


# ---------------------------------------------------------------- alert rules
def evaluate(o: dict, now: datetime, rules: Rules, tracker: Optional[RiderTracker] = None,
             rider_online: Optional[bool] = None) -> list:
    """Return the list of alert conditions currently true for this order.
    Each: {"kind", "severity" (red|amber), "headline", "action"}"""
    out = []
    if o["phase"] in ("delivered", "cancelled", "on_hold", "closed") or not o.get("dispatched_at"):
        return out
    elapsed = mins(o["dispatched_at"], now) or 0
    tgt, warn = rules.ptod_target_min, rules.ptod_warn_min

    # --- PTOD clock (every live order) ---
    if elapsed >= tgt:
        out.append({"kind": "ptod", "severity": "red",
                    "headline": f"PTOD breached — {int(elapsed)} min since dispatch, not delivered",
                    "action": "Call the rider now; if far away, reassign"})
    elif elapsed >= warn:
        out.append({"kind": "ptod", "severity": "amber",
                    "headline": f"PTOD at risk — {int(elapsed)} min since dispatch ({tgt - int(elapsed)} min left)",
                    "action": "Call the rider, ask for direct delivery"})
    elif o["eta_customer"] and o["phase"] != "unassigned":
        projected = mins(o["dispatched_at"], o["eta_customer"])
        if projected and projected > tgt:
            out.append({"kind": "ptod", "severity": "amber",
                        "headline": f"PTOD at risk — ETA projects {int(projected)} min total (target {tgt})",
                        "action": "Check route / restaurant wait"})

    ph = o["phase"]
    # --- nobody accepted ---
    if ph == "unassigned":
        if elapsed >= rules.accept_limit_min:
            out.append({"kind": "unassigned", "severity": "red" if elapsed >= rules.accept_limit_min + 3 else "amber",
                        "headline": f"No rider after {int(elapsed)} min — nobody accepted",
                        "action": "Assign a free rider in MotionTools / call idle riders"})
        return out

    # --- accepted but not started ---
    if ph == "accepted":
        since = mins(o["accepted_at"], now) or 0
        if since >= rules.start_limit_min:
            out.append({"kind": "not_started", "severity": "amber" if since < rules.start_limit_min * 2 else "red",
                        "headline": f"Accepted {int(since)} min ago but hasn't started",
                        "action": "Call the rider: start the tour now"})

    # --- riding phases: late vs ETA, stationary, wrong way ---
    if ph in ("to_restaurant", "to_customer"):
        target = "restaurant" if ph == "to_restaurant" else "customer"
        eta = o["eta_restaurant"] if ph == "to_restaurant" else o["eta_customer"]
        if eta and now > eta + timedelta(minutes=rules.late_grace_min):
            late = int(mins(eta, now) or 0)
            out.append({"kind": f"late_{target}", "severity": "amber" if late < 10 else "red",
                        "headline": f"Late to {target} — {late} min behind ETA",
                        "action": "Call the rider, check where they are"})
        if tracker and o["rider_id"] and tracker.has_feed(o["rider_id"], now):
            still = tracker.stationary_minutes(o["rider_id"], now, rules.stationary_radius_m)
            fix = tracker.last_fix(o["rider_id"])
            age = (now - fix[0]).total_seconds() / 60 if fix else None
            if still is not None and still >= rules.stationary_min and (mins(o["started_at"] or o["accepted_at"] or o["dispatched_at"], now) or 0) >= rules.stationary_min:
                if age is not None and age >= rules.stale_gps_min:
                    out.append({"kind": "stationary", "severity": "amber" if age < rules.stale_gps_min * 3 else "red",
                                "headline": f"No GPS update for {int(age)} min while riding to the {target} (app closed / phone off?)",
                                "action": "Call the rider: is the app running?"})
                else:
                    out.append({"kind": "stationary", "severity": "amber" if still < rules.stationary_min * 2 else "red",
                                "headline": f"Not moving for {int(still)} min (should be riding to the {target})",
                                "action": "Call the rider"})
            tl, tg = (o["pick_lat"], o["pick_lng"]) if ph == "to_restaurant" else (o["drop_lat"], o["drop_lng"])
            away = tracker.wrong_way(o["rider_id"], f"{o['id']}:{ph}", tl, tg, now, rules.wrong_way_m)
            if away:
                out.append({"kind": "off_route", "severity": "amber",
                            "headline": f"Wrong direction — {away} m further from the {target} than before",
                            "action": "Call the rider, confirm the address"})

    # --- waiting at restaurant / customer ---
    if ph == "at_restaurant":
        w = mins(o["at_restaurant_at"], now) or 0
        if w >= rules.wait_restaurant_min:
            out.append({"kind": "wait_restaurant", "severity": "amber" if w < rules.wait_restaurant_min * 2 else "red",
                        "headline": f"Waiting at {o['restaurant']} for {int(w)} min",
                        "action": "Call the restaurant: is the order ready?"})
    if ph == "at_customer":
        w = mins(o["at_customer_at"], now) or 0
        if w >= rules.wait_customer_min:
            out.append({"kind": "wait_customer", "severity": "amber" if w < rules.wait_customer_min * 2 else "red",
                        "headline": f"At the customer for {int(w)} min without handover",
                        "action": "Call the rider: customer reachable?"})

    if rider_online is False and ph not in ("unassigned",):
        out.append({"kind": "offline", "severity": "red",
                    "headline": "Rider is OFFLINE with an active order",
                    "action": "Call the rider immediately / reassign"})
    return out
