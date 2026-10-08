"""Webhook mode: rebuild orders and riders from the events MotionTools pushes.

Used when the account is in "restricted API mode" (the token may not read bookings / users).
Every event keeps the same order shape as parse_booking(), so alerts, storage and the
dashboard work unchanged. Payload formats: docs.motiontools.io → Event notifications.

Phase sources
  created_at      booking.created (pre-orders sit "on hold" here, sometimes for hours — no PTOD yet)
  dispatched_at   booking.transition to dispatched/pickable | tour.created | booking.created already dispatched
                  (= the order is offered to riders = PTOD start)
  accepted_at     tour.transition to=claimed  |  driver.busy shortly before booking.in_progress
  started_at      booking.in_progress (rider started the tour; brings driver_id, driver_name, GPS)
  at_restaurant   booking.stop_arrived   stop_type=pickup
  picked_up       booking.stop_completed stop_type=pickup
  at_customer     booking.stop_arrived   stop_type=dropoff
  delivered       booking.stop_completed stop_type=dropoff  |  booking.transition to=done
  cancelled       booking.transition to=cancelled  |  booking.stop_failed
  GPS             booking.driver_location_updated (needs a customer_id filter on the webhook)
                  | tour.driver_location_updated (tour -> bookings via tour.created)
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta

from orders import UTC, new_order, phase_from, ts

# PTOD starts when riders can see / have the order. Real MotionTools tour timeline (1 Oct, order WPC4W7):
#   On hold (create) 08:37 -> Scheduled 11:00 -> Pickable (ready_to_pick) 11:00 -> En route (pick) 11:00 -> Done 11:34
# "dispatched"/"partially_dispatched" only means "put into a tour" — that already happens at creation for pre-orders,
# so it must NOT start the clock.  pickable = offered to riders, claimed/en_route = a rider has it.
DISPATCHED = {"pickable", "claimed", "en_route"}
ASSIGNED = {"dispatched", "partially_dispatched", "scheduled"}
DONE = {"done", "completed", "finished", "paid", "processing_payment"}
STALE_HOURS = 3           # a live order without any MotionTools event for this long is closed automatically
SEEN_MAX = 150000         # remembered event ids (≈ 1½ days of non-GPS events at 10 000 orders a day, ~15 MB)


def event_key(p: dict):
    """MotionTools delivers "at least once" — the same event can arrive twice (docs: Webhooks → Idempotence).
    Every event has its own id (uuid); id + type + timestamp identify one delivery's content. GPS updates are
    exempt: a repeated position changes nothing, and they are most of the traffic."""
    eid = p.get("id")
    if not eid or not isinstance(eid, (str, int)) or str(p.get("event") or "") == "driver_location_updated":
        return None
    d = p.get("data") if isinstance(p.get("data"), dict) else {}
    # the subject (+ target state / stop) is part of the key too: only a true repeat can ever match
    return hash((eid, str(p.get("resource_type") or ""), str(p.get("event") or ""), str(p.get("timestamp") or ""),
                 str(d.get("booking_id") or d.get("tour_id") or d.get("driver_id") or ""), str(d.get("to") or ""),
                 str(d.get("stop_id") or "")))


class Projector:
    def __init__(self, state: dict, store, tracker, areas: list, keep_finished: bool = False):
        self.state, self.store, self.tracker, self.areas = state, store, tracker, areas
        self.keep_finished = keep_finished     # replay mode: finished orders stay in state so their story can be copied
        self.lead_min = 45                     # see Rules.release_lead_min (kept in sync by the server)
        self.tours: dict = {}          # tour_id -> [booking_id]
        self.busy_at: dict = {}        # driver_id -> time the driver last became busy (≈ accepted an order)
        self.places: dict = {}         # place_id -> restaurant name (editable in Settings / filled from the API)
        self.place_ll: dict = {}       # place_id -> (lat, lng): from the place API, a booking detail, or where a rider arrived
        self.phones: dict = {}         # rider_id -> phone number typed in Settings (used when MotionTools sends none)
        self.counts: dict = {}
        self.done_keys: OrderedDict = OrderedDict()   # event_key -> None, oldest first (NOT self.seen: that is a method)
        self.repeats = 0
        self._load()

    # ---------------- at-least-once delivery ----------------
    def repeat(self, p: dict) -> bool:
        """True when this exact event was processed before (then it must change nothing)."""
        k = event_key(p)
        if k is None:
            return False
        if k in self.done_keys:
            self.repeats += 1
            return True
        self.done_keys[k] = None
        if len(self.done_keys) > SEEN_MAX:
            self.done_keys.popitem(last=False)
        return False

    def seed_seen(self, keys: list):
        """After a restart: the events already in the event log count as processed (they are, by the repair)."""
        merged = OrderedDict.fromkeys(k for k in keys if k is not None)
        for k in self.done_keys:
            merged.pop(k, None)
            merged[k] = None
        while len(merged) > SEEN_MAX:
            merged.popitem(last=False)
        self.done_keys = merged

    # ---------------- persistence of small maps ----------------
    def _load(self):
        for k, v in self.store.get_settings().items():
            if k.startswith("place:"):
                self.places[k[6:]] = v
            elif k.startswith("placell:"):
                try:
                    la, ln = v.split(",")
                    self.place_ll[k[8:]] = (float(la), float(ln))
                except ValueError:
                    pass
            elif k.startswith("tour:"):
                self.tours[k[5:]] = v.split(",")
            elif k.startswith("phone:"):
                self.phones[k[6:]] = v

    def set_place_ll(self, pid: str, lat, lng):
        if not pid or lat is None or lng is None or pid in self.place_ll:
            return
        self.place_ll[pid] = (float(lat), float(lng))
        self.store.set_settings({f"placell:{pid}": f"{float(lat):.6f},{float(lng):.6f}"})
        for o in self.state["orders"].values():
            if o.get("place_id") == pid and o.get("pick_lat") is None:
                o["pick_lat"], o["pick_lng"] = self.place_ll[pid]

    def set_place(self, pid: str, name: str):
        self.places[pid] = name
        self.store.set_settings({f"place:{pid}": name})
        for o in self.state["orders"].values():
            if o.get("place_id") == pid:
                o["restaurant"] = name or self.restaurant_name(pid)
        if hasattr(self.store, "rename_place"):
            self.store.rename_place(pid, name or self.restaurant_name(pid))

    def set_phone(self, rid: str, phone: str):
        self.phones[rid] = phone
        self.store.set_settings({f"phone:{rid}": phone})
        r = self.state["riders"].get(rid)
        if r is not None:
            r["phone"] = phone or r.get("mt_phone") or ""

    def phone_for(self, rid: str, mt_phone: str = "") -> str:
        """A number typed in Settings wins; otherwise whatever MotionTools sent."""
        return self.phones.get(rid) or mt_phone or ""

    def restaurant_name(self, pid: str) -> str:
        return self.places.get(pid) or (f"Restaurant {pid[:6]}" if pid else "Restaurant")

    # ---------------- helpers ----------------
    def order(self, bid: str, d: dict, now: datetime) -> dict:
        o = self.state["orders"].get(bid)
        if o is None:
            o = self.store.order(bid)                 # already finished earlier? keep its real timestamps
            if o is not None:
                o.setdefault("stop_types", {})
                o["cancelled"] = o.get("phase") == "cancelled"
            else:
                o = new_order(bid, d.get("external_id") or "", d.get("service_area_id"), now)
                o["partial"] = True                   # we did not see this order's creation
                o["dispatched_at"] = None             # ...so it counts as on hold until a rider-facing event arrives
            self.state["orders"][bid] = o
        if d.get("external_id"):
            o["ref"] = d["external_id"]
        if d.get("service_area_id"):
            o["area"] = d["service_area_id"]
        return o

    def rider(self, rid: str, name: str = "", now: datetime = None):
        r = self.state["riders"].get(rid)
        created = r is None
        if created:
            r = self.state["riders"][rid] = {"id": rid, "name": "", "phone": self.phones.get(rid, ""), "mt_phone": "",
                                             "online": True, "lat": None, "lng": None, "active_ids": []}
        if name and not r["name"]:
            r["name"] = name
        if created or (name and not r.get("persisted")):
            # every rider we hear about is stored at once, so a restart never forgets who is on the road
            self.store.upsert_rider(rid, r["name"] or "Rider", r.get("phone", ""), True, None, None, [], now or datetime.now(UTC))
            r["persisted"] = bool(r["name"])
        return r

    @staticmethod
    def note(o: dict, now: datetime, what: str, rid=None, name: str = ""):
        """Order history shown in the story: who accepted, who handed it back, who took it next."""
        h = o.setdefault("history", [])
        if h and h[-1]["what"] == what and h[-1]["rider_id"] == rid and h[-1]["at"] == now.isoformat(timespec="seconds"):
            return
        h.append({"at": now.isoformat(timespec="seconds"), "what": what, "rider_id": rid, "rider": name or ""})
        del h[:-40]

    def release(self, o: dict, now: datetime, why: str = "released"):
        """The rider who had this order no longer has it (handed back / unassigned by the dispatcher)."""
        if not o.get("rider_id"):
            return
        self.note(o, now, why, o["rider_id"], o.get("rider") or "")
        o["reassigned"] = (o.get("reassigned") or 0) + 1
        o["rider_id"], o["rider"] = None, ""
        o["accepted_at"], o["started_at"] = None, None          # the next rider's acceptance counts from here
        if not o.get("picked_up_at"):
            o["at_restaurant_at"], o["at_customer_at"] = None, None

    def set_rider(self, o: dict, rid, name, now):
        if rid:
            if o.get("rider_id") and o["rider_id"] != rid:
                self.release(o, now)                            # another rider takes over
            new_rider = o.get("rider_id") != rid
            o["rider_id"] = rid
            r = self.rider(rid, name or "", now)
            o["rider"] = name or r["name"] or o["rider"]
            if not o["accepted_at"]:
                busy = self.busy_at.get(rid)
                fresh = busy and now - busy < timedelta(minutes=20) and (not o.get("dispatched_at") or busy >= o["dispatched_at"]) \
                    and not any(h["what"] == "released" and h["at"] > busy.isoformat(timespec="seconds") for h in o.get("history") or [])
                o["accepted_at"] = busy if fresh else now
            if new_rider:
                self.note(o, now, "accepted", rid, o["rider"])

    def seen(self, rid, now):
        """Any event from a rider's app (started, arrived, GPS, busy) proves the rider is online right now."""
        if not rid:
            return
        r = self.rider(rid)
        r["last_seen"] = now.isoformat(timespec="seconds")
        if not r.get("online"):
            r["online"] = True
            self.store.upsert_rider(rid, r["name"] or "Rider", r.get("phone", ""), True, r.get("lat"), r.get("lng"),
                                    [o["id"] for o in self.state["orders"].values() if o["rider_id"] == rid], now)

    def gps(self, rid, lat, lng, now, order_id=None):
        if rid and lat is not None and lng is not None:
            r = self.rider(rid)
            r["lat"], r["lng"] = lat, lng
            self.tracker.push(rid, lat, lng, now)
            self.store.record_position(rid, lat, lng, order_id, now)

    TEXT_FIELDS = ("ref", "area", "area_name", "restaurant_phone", "customer_addr", "customer_phone", "customer_zip",
                   "customer_name", "customer_notes", "place_id", "cancel_reason")
    TIME_FIELDS = ("created_at", "dispatched_at", "accepted_at", "started_at", "at_restaurant_at", "picked_up_at",
                   "at_customer_at", "delivered_at")

    def merge_api(self, o: dict, p: dict, now: datetime):
        """Fill what the events could not tell us from a full booking read through the API (if that endpoint is open)."""
        for k in self.TEXT_FIELDS:
            if p.get(k) and not o.get(k):
                o[k] = p[k]
        for k in ("pick_lat", "pick_lng", "drop_lat", "drop_lng", "est_distance_m", "eta_restaurant", "eta_customer"):
            if p.get(k) is not None:
                o[k] = p[k]
        if p.get("pick_lat") is not None and (o.get("place_id") or p.get("place_id")):
            self.set_place_ll(o.get("place_id") or p.get("place_id"), p["pick_lat"], p.get("pick_lng"))
        if p.get("restaurant") and p["restaurant"] != "Restaurant":
            o["restaurant"] = p["restaurant"]
            if o.get("place_id") and not self.places.get(o["place_id"]):
                self.set_place(o["place_id"], p["restaurant"])
        for k in self.TIME_FIELDS:
            if p.get(k) and not o.get(k):
                o[k] = p[k]
        if p.get("scheduled_at"):
            o["scheduled_at"] = p["scheduled_at"]
        if not o.get("promised_at"):
            o["promised_at"] = p.get("promised_at") or (p.get("eta_customer") if not (o.get("delivered_at") or o.get("cancelled")) else None)
        if p.get("status"):
            o["status"] = p["status"]
            if p["status"] in DISPATCHED or p["status"] in DONE:
                self.dispatched(o, now)
        if p.get("phase") == "cancelled":
            o["cancelled"] = True
        if p.get("rider_id"):
            self.set_rider(o, p["rider_id"], p.get("rider") or "", now)
        if p.get("rider_lat") is not None and o["rider_id"] and not (o.get("delivered_at") or o.get("cancelled")):
            o["rider_lat"], o["rider_lng"] = p["rider_lat"], p["rider_lng"]
            self.gps(o["rider_id"], p["rider_lat"], p["rider_lng"], now, o["id"])
        o["partial"] = False
        self.finish(o, now)

    def dispatched(self, o: dict, now: datetime, exact: bool = False):
        """The order is (or must have been) offered to riders — start the PTOD clock if it isn't running.
        exact=True: this event IS the release (pickable).  Otherwise (rider accepted / started / arrived…)
        the release happened earlier: if a planned delivery time is known, the clock starts at the release
        moment (planned − lead) — a rider accepting at 12:50 an order released at 12:45 means 5 minutes of
        waiting that belong to the PTOD."""
        if o.get("dispatched_at"):
            return
        planned = o.get("eta_customer") or o.get("scheduled_at")
        start = now
        if planned is not None and not exact:
            release = planned - timedelta(minutes=self.lead_min)
            start = max(min(now, release), o.get("created_at") or release)
        o["dispatched_at"] = start

    def weak_dispatch(self, o: dict, now: datetime):
        """A signal that only means "the order is in a tour" (created as dispatched, transition to dispatched,
        tour created). For an ASAP order that IS the release; for a pre-order the tour sits on hold for hours.
        Decide by the planned delivery time: released = planned − lead (MotionTools' automatic scheduling)."""
        o["in_tour"] = True
        if o.get("dispatched_at"):
            return
        planned = o.get("eta_customer") or o.get("scheduled_at")
        if planned is None:
            # no planned time known (yet): MotionTools sends the ETAs right after creation — wait for them briefly,
            # then treat the order as ASAP (released at creation)
            created = o.get("created_at")
            if created and now - created >= timedelta(minutes=3):
                self.dispatched(o, created)
            return
        release = planned - timedelta(minutes=self.lead_min)
        if now >= release:
            self.dispatched(o, now)

    def release_due(self, now: datetime) -> int:
        """Every sync: pre-orders whose release time has come move from On hold to Waiting for rider,
        even if MotionTools sends no event for it."""
        n = 0
        for o in list(self.state["orders"].values()):
            if o.get("dispatched_at") or o.get("cancelled") or o.get("closed_auto") or not o.get("in_tour"):
                continue
            self.weak_dispatch(o, now)
            if o.get("dispatched_at"):
                self.finish(o, now)
                n += 1
        return n

    def expire(self, now: datetime) -> int:
        """Close live orders that MotionTools stopped talking about (no event for STALE_HOURS)."""
        n = 0
        for o in list(self.state["orders"].values()):
            last = o.get("last_event_at") or o.get("created_at")
            if last and now - last > timedelta(hours=STALE_HOURS):
                o["closed_auto"] = True
                o["cancel_reason"] = f"closed automatically — no MotionTools events for {STALE_HOURS} h"
                self.finish(o, now)
                n += 1
        return n

    def finish(self, o: dict, now: datetime):
        o["phase"] = phase_from(o)
        stacked = o["rider_id"] and sum(1 for x in self.state["orders"].values()
                                        if x["rider_id"] == o["rider_id"] and x["phase"] not in ("delivered", "cancelled")) >= 2
        self.store.upsert_order(o, now, stacked=bool(stacked), force=True)   # events are the full truth — no merging
        if o["phase"] in ("delivered", "cancelled", "closed"):
            why = {"closed": "order closed (no events)"}.get(o["phase"], o["phase"])
            for key in [k for k in self.state["open_alerts"] if k[0] == o["id"]]:
                self.store.resolve_alert(self.state["open_alerts"].pop(key), why, now)
                self.state["sev"].pop(key, None)
                self.state.get("heads", {}).pop(key, None)
            if not self.keep_finished:
                self.state["orders"].pop(o["id"], None)

    # ---------------- the event switch ----------------
    def apply(self, p: dict) -> str:
        if self.repeat(p):
            return "repeat"
        rtype, ev = str(p.get("resource_type") or ""), str(p.get("event") or "")
        name = f"{rtype}.{ev}"
        d = p.get("data") or {}
        now = ts(p.get("timestamp") or d.get("timestamp")) or datetime.now(UTC)
        area = d.get("service_area_id")
        if self.areas and area and area not in self.areas:
            return "other area"
        self.counts[name] = self.counts.get(name, 0) + 1

        if rtype == "booking":
            bid = d.get("booking_id")
            if not bid:
                return "no booking id"
            if ev == "created":
                o = self.state["orders"].get(bid) or new_order(bid, d.get("external_id") or "", area, now)
                o["created_at"] = o["last_event_at"] = now
                o["status"] = d.get("status") or ""
                o["dispatched_at"] = now if o["status"] in DISPATCHED else None      # pre-orders wait "on hold"
                if o["status"] in ASSIGNED:
                    self.weak_dispatch(o, now)                                        # ASAP order created straight into a tour
                o["scheduled_at"] = ts(d.get("scheduled_at") or d.get("scheduled_for") or d.get("pickup_at")
                                       or d.get("earliest_pickup_at") or d.get("delivery_at")) or o.get("scheduled_at")
                o["partial"] = False
                pids = d.get("place_ids") or []
                if isinstance(pids, str):
                    pids = [pids]
                if pids:
                    o["place_id"] = pids[0]
                    o["restaurant"] = self.restaurant_name(pids[0])
                    if o.get("pick_lat") is None and pids[0] in self.place_ll:
                        o["pick_lat"], o["pick_lng"] = self.place_ll[pids[0]]
                o["area"] = area or o["area"]
                self.state["orders"][bid] = o
                self.finish(o, now)
                return name
            o = self.order(bid, d, now)
            o["last_event_at"] = now                      # only real MotionTools events count as "still alive"
            if ev == "transition":
                to = str(d.get("to") or "")
                if to in DONE:
                    self.dispatched(o, now)
                    if not o["delivered_at"]:
                        o["delivered_at"] = now
                elif to == "cancelled":
                    o["cancelled"] = True
                    o["cancel_reason"] = o.get("cancel_reason") or "cancelled in MotionTools"
                    self.note(o, now, "cancelled", o.get("rider_id"), o.get("rider") or "")
                elif to in DISPATCHED:
                    if to == "pickable" and o.get("rider_id") and not o.get("picked_up_at"):
                        self.release(o, now)                                     # back to "pickable" = rider handed it back
                    self.dispatched(o, now, exact=(to == "pickable"))            # PTOD clock starts here
                    users = d.get("affected_user_ids") or []
                    users = [users] if isinstance(users, str) else list(users)
                    if to in ("claimed", "en_route") and users:
                        self.set_rider(o, users[0], "", now)                     # the rider who accepted (booking-level claim)
                    if to == "en_route" and o.get("rider_id"):
                        o["started_at"] = o["started_at"] or now
                elif to in ASSIGNED or to == "to_be_dispatched":
                    if o.get("rider_id") and not o.get("picked_up_at"):
                        self.release(o, now)
                    if to in ASSIGNED:
                        o["assigned_at"] = now                                   # in a tour — on hold or released?
                        self.weak_dispatch(o, now)
                o["status"] = to or o["status"]
            elif ev == "in_progress":
                self.dispatched(o, now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                self.seen(d.get("driver_id"), now)
                o["started_at"] = o["started_at"] or now
                loc = d.get("driver_location") or {}
                o["rider_lat"], o["rider_lng"] = loc.get("lat"), loc.get("lng")
                self.gps(o["rider_id"], loc.get("lat"), loc.get("lng"), now, bid)
            elif ev == "etas_recalculated":
                o["eta_at"] = now
                for s in d.get("unfinished_stops_info") or []:
                    kind = str(s.get("type") or "").lower()          # docs: pickup | dropoff | task | return
                    o["stop_types"][str(s.get("id"))] = kind
                    if kind not in ("pickup", "dropoff"):
                        continue                                  # a task / return stop's ETA is not the customer's
                    eta = ts(s.get("eta"))
                    if eta:
                        o["eta_restaurant" if kind == "pickup" else "eta_customer"] = eta
                        if kind == "dropoff" and not o.get("promised_at"):
                            o["promised_at"] = eta                  # the first ETA = the planned delivery time (never moves)
                if o.get("status") != "to_be_dispatched":
                    self.weak_dispatch(o, now)
            elif ev in ("stop_arrived", "stop_completed", "stop_failed"):
                self.dispatched(o, now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                self.seen(d.get("driver_id") or o.get("rider_id"), now)
                kind = str(d.get("stop_type") or o["stop_types"].get(str(d.get("stop_id")), "")).lower()
                who = o.get("rider") or d.get("driver_name") or ""
                if kind in ("task", "return"):
                    pass
                elif ev == "stop_arrived":
                    key = "at_restaurant_at" if kind == "pickup" else "at_customer_at"
                    o[key] = o[key] or now
                    loc = d.get("driver_location") or {}
                    fix = self.tracker.last_fix(o.get("rider_id")) if o.get("rider_id") else None
                    lat, lng = (loc.get("lat"), loc.get("lng")) if loc.get("lat") is not None else ((fix[1], fix[2]) if fix and (now - fix[0]).total_seconds() < 600 else (None, None))
                    if lat is not None:                       # where the rider stood when he arrived = the stop's position
                        if kind == "pickup":
                            if o.get("pick_lat") is None:
                                o["pick_lat"], o["pick_lng"] = lat, lng
                            self.set_place_ll(o.get("place_id"), lat, lng)
                        elif o.get("drop_lat") is None:
                            o["drop_lat"], o["drop_lng"] = lat, lng
                    self.note(o, now, "arrived_restaurant" if kind == "pickup" else "arrived_customer", o.get("rider_id"), who)
                elif ev == "stop_completed":
                    if kind == "pickup":
                        o["picked_up_at"] = o["picked_up_at"] or now
                        o["at_restaurant_at"] = o["at_restaurant_at"] or now
                        self.note(o, now, "picked_up", o.get("rider_id"), who)
                    else:
                        o["at_customer_at"] = o["at_customer_at"] or now
                        o["delivered_at"] = o["delivered_at"] or now
                        if o.get("cancelled") and str(o.get("cancel_reason") or "").startswith("delivery failed"):
                            o["cancelled"] = False                # an earlier failed attempt, delivered after all
                            o["cancel_reason"] = ""
                        self.note(o, now, "delivered", o.get("rider_id"), who)
                elif ev == "stop_failed":
                    # docs: outcome = skip_recovery | reattempt_later | reattempt_now | return_later | return_now,
                    # failure_reason.key = recipient_unavailable, location_closed, … — a re-attempt is NOT the end
                    self.note(o, now, "pickup_failed" if kind == "pickup" else "delivery_failed", o.get("rider_id"), who)
                    fr = d.get("failure_reason") if isinstance(d.get("failure_reason"), dict) else {}
                    why = str(fr.get("key") or "").replace("_", " ")
                    if kind == "dropoff" and not str(d.get("outcome") or "").startswith("reattempt"):
                        o["cancelled"] = True
                        o["cancel_reason"] = "delivery failed" + (f" — {why}" if why else "")
            elif ev == "driver_location_updated":
                self.seen(d.get("driver_id") or o.get("rider_id"), now)
                loc = d.get("driver_location") or {}
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now) if d.get("driver_id") and not o["rider_id"] else None
                o["rider_lat"], o["rider_lng"] = loc.get("lat"), loc.get("lng")
                self.gps(d.get("driver_id") or o["rider_id"], loc.get("lat"), loc.get("lng"), now, bid)
                return name                                   # position only — nothing to store on the order
            self.finish(o, now)
            return name

        if rtype == "driver":
            rid = d.get("driver_id")
            if not rid:
                return "no driver id"
            r = self.rider(rid)
            prof = d.get("profile") or {}
            pname = " ".join(x for x in [prof.get("first_name"), prof.get("last_name")] if x).strip()
            if pname:
                r["name"] = pname
            if prof.get("phone_number"):
                r["mt_phone"] = prof["phone_number"]
                r["phone"] = self.phone_for(rid, r["mt_phone"])
            if ev == "online":
                r["online"] = True
                r["last_seen"] = now.isoformat(timespec="seconds")
                loc = d.get("location") or {}
                self.gps(rid, loc.get("lat"), loc.get("lng"), now)
            elif ev == "offline":
                r["online"] = False
                r["offline_at"] = now.isoformat(timespec="seconds")
            elif ev in ("busy", "no_longer_busy"):
                r["last_seen"] = now.isoformat(timespec="seconds")
                r["online"] = True
                if ev == "busy":
                    self.busy_at[rid] = now
            self.store.upsert_rider(rid, r["name"] or "Rider", r["phone"], r["online"], r.get("lat"), r.get("lng"),
                                    [o["id"] for o in self.state["orders"].values() if o["rider_id"] == rid], now)
            return name

        if rtype == "tour":
            tid = d.get("tour_id")
            if ev == "created":
                ids = d.get("dispatched_booking_ids") or []
                self.tours[tid] = [ids] if isinstance(ids, str) else list(ids)
                self.store.set_settings({f"tour:{tid}": ",".join(self.tours[tid])})
                st = str(d.get("status") or "")
                for bid in self.tours[tid]:
                    if bid in self.state["orders"]:
                        o = self.state["orders"][bid]
                        o["assigned_at"] = now
                        o["tour_id"] = tid
                        if st in DISPATCHED:                                     # tours are usually created on hold
                            self.dispatched(o, now, exact=(st == "pickable"))
                        else:
                            self.weak_dispatch(o, now)
                        self.finish(o, now)
                return name
            bookings = [self.state["orders"][b] for b in self.tours.get(tid, []) if b in self.state["orders"]]
            for o in bookings:
                o["last_event_at"] = now
            if ev == "transition":
                to = d.get("to")
                users = d.get("affected_user_ids") or []
                users = [users] if isinstance(users, str) else users
                for o in bookings:
                    o["tour_id"] = tid
                    if to == "claimed":
                        self.dispatched(o, now)
                        if users and o.get("rider_id") and o["rider_id"] != users[0]:
                            self.release(o, now)
                        o["accepted_at"] = o["accepted_at"] or now
                        if users and not o["rider_id"]:
                            o["rider_id"] = users[0]
                            o["rider"] = self.rider(users[0])["name"]
                            self.note(o, now, "accepted", users[0], o["rider"])
                    elif to == "pickable":
                        if o.get("rider_id") and not o.get("picked_up_at"):
                            self.release(o, now)                                 # tour offered again = handed back
                        self.dispatched(o, now, exact=True)
                    elif to == "en_route":
                        self.dispatched(o, now)
                        o["started_at"] = o["started_at"] or now
                    elif to in DISPATCHED:
                        self.dispatched(o, now)
                    elif to in ASSIGNED:
                        self.weak_dispatch(o, now)
                    elif to in DONE:
                        if not o.get("cancelled"):
                            o["delivered_at"] = o["delivered_at"] or now
                    elif to == "cancelled":
                        o["cancelled"] = True
                    self.finish(o, now)
            elif ev == "force_assigned":
                for o in bookings:
                    self.dispatched(o, now)
                    self.set_rider(o, d.get("driver_id"), "", now)
                    self.finish(o, now)
            elif ev == "force_unassigned":
                for o in bookings:                        # the dispatcher took the tour away from this rider
                    if o.get("rider_id") and (not d.get("driver_id") or o["rider_id"] == d.get("driver_id")):
                        self.release(o, now, "unassigned")
                        self.finish(o, now)
            elif ev == "driver_location_updated":
                for o in bookings:
                    o["rider_lat"], o["rider_lng"] = d.get("lat"), d.get("lng")
                    self.seen(o["rider_id"], now)
                    self.gps(o["rider_id"], d.get("lat"), d.get("lng"), now, o["id"])
            return name
        return name
