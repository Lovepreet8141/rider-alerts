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


class Projector:
    def __init__(self, state: dict, store, tracker, areas: list):
        self.state, self.store, self.tracker, self.areas = state, store, tracker, areas
        self.tours: dict = {}          # tour_id -> [booking_id]
        self.busy_at: dict = {}        # driver_id -> time the driver last became busy (≈ accepted an order)
        self.places: dict = {}         # place_id -> restaurant name (editable in Settings / filled from the API)
        self.phones: dict = {}         # rider_id -> phone number typed in Settings (used when MotionTools sends none)
        self.counts: dict = {}
        self._load()

    # ---------------- persistence of small maps ----------------
    def _load(self):
        for k, v in self.store.get_settings().items():
            if k.startswith("place:"):
                self.places[k[6:]] = v
            elif k.startswith("tour:"):
                self.tours[k[5:]] = v.split(",")
            elif k.startswith("phone:"):
                self.phones[k[6:]] = v

    def set_place(self, pid: str, name: str):
        self.places[pid] = name
        self.store.set_settings({f"place:{pid}": name})
        for o in self.state["orders"].values():
            if o.get("place_id") == pid:
                o["restaurant"] = name or self.restaurant_name(pid)

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
            self.state["orders"][bid] = o
        if d.get("external_id"):
            o["ref"] = d["external_id"]
        if d.get("service_area_id"):
            o["area"] = d["service_area_id"]
        return o

    def rider(self, rid: str, name: str = "", now: datetime = None):
        r = self.state["riders"].get(rid)
        if r is None:
            r = self.state["riders"][rid] = {"id": rid, "name": "", "phone": self.phones.get(rid, ""), "mt_phone": "",
                                             "online": True, "lat": None, "lng": None, "active_ids": []}
        if name and not r["name"]:
            r["name"] = name
        return r

    def set_rider(self, o: dict, rid, name, now):
        if rid:
            o["rider_id"] = rid
            r = self.rider(rid, name or "", now)
            o["rider"] = name or r["name"] or o["rider"]
            if not o["accepted_at"]:
                busy = self.busy_at.get(rid)
                o["accepted_at"] = busy if busy and now - busy < timedelta(minutes=20) else now

    def gps(self, rid, lat, lng, now, order_id=None):
        if rid and lat is not None and lng is not None:
            r = self.rider(rid)
            r["lat"], r["lng"] = lat, lng
            self.tracker.push(rid, lat, lng, now)
            self.store.record_position(rid, lat, lng, order_id, now)

    TEXT_FIELDS = ("ref", "area", "area_name", "restaurant_phone", "customer_addr", "customer_phone", "customer_zip",
                   "place_id", "cancel_reason")
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
        if p.get("restaurant") and p["restaurant"] != "Restaurant":
            o["restaurant"] = p["restaurant"]
            if o.get("place_id") and not self.places.get(o["place_id"]):
                self.set_place(o["place_id"], p["restaurant"])
        for k in self.TIME_FIELDS:
            if p.get(k) and not o.get(k):
                o[k] = p[k]
        if p.get("scheduled_at"):
            o["scheduled_at"] = p["scheduled_at"]
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

    def dispatched(self, o: dict, now: datetime):
        """The order is (or must have been) offered to riders — start the PTOD clock if it isn't running."""
        if not o.get("dispatched_at"):
            o["dispatched_at"] = now

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
        self.store.upsert_order(o, now, stacked=bool(stacked))
        if o["phase"] in ("delivered", "cancelled", "closed"):
            why = {"closed": "order closed (no events)"}.get(o["phase"], o["phase"])
            for key in [k for k in self.state["open_alerts"] if k[0] == o["id"]]:
                self.store.resolve_alert(self.state["open_alerts"].pop(key), why, now)
                self.state["sev"].pop(key, None)
                self.state.get("heads", {}).pop(key, None)
            self.state["orders"].pop(o["id"], None)

    # ---------------- the event switch ----------------
    def apply(self, p: dict) -> str:
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
                o["scheduled_at"] = ts(d.get("scheduled_at") or d.get("scheduled_for") or d.get("pickup_at")
                                       or d.get("earliest_pickup_at") or d.get("delivery_at")) or o.get("scheduled_at")
                o["partial"] = False
                pids = d.get("place_ids") or []
                if isinstance(pids, str):
                    pids = [pids]
                if pids:
                    o["place_id"] = pids[0]
                    o["restaurant"] = self.restaurant_name(pids[0])
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
                elif to in DISPATCHED:
                    self.dispatched(o, now)                                      # PTOD clock starts here
                elif to in ASSIGNED:
                    o["assigned_at"] = now                                       # in a tour, still on hold
                o["status"] = to or o["status"]
            elif ev == "in_progress":
                self.dispatched(o, now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                o["started_at"] = o["started_at"] or now
                loc = d.get("driver_location") or {}
                o["rider_lat"], o["rider_lng"] = loc.get("lat"), loc.get("lng")
                self.gps(o["rider_id"], loc.get("lat"), loc.get("lng"), now, bid)
            elif ev == "etas_recalculated":
                for s in d.get("unfinished_stops_info") or []:
                    kind = "pickup" if "pick" in str(s.get("type", "")).lower() else "dropoff"
                    o["stop_types"][str(s.get("id"))] = kind
                    eta = ts(s.get("eta"))
                    if eta:
                        o["eta_restaurant" if kind == "pickup" else "eta_customer"] = eta
            elif ev in ("stop_arrived", "stop_completed", "stop_failed"):
                self.dispatched(o, now)
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now)
                kind = str(d.get("stop_type") or o["stop_types"].get(str(d.get("stop_id")), "")).lower()
                if kind in ("task", "return"):
                    pass
                elif ev == "stop_arrived":
                    key = "at_restaurant_at" if kind == "pickup" else "at_customer_at"
                    o[key] = o[key] or now
                elif ev == "stop_completed":
                    if kind == "pickup":
                        o["picked_up_at"] = o["picked_up_at"] or now
                        o["at_restaurant_at"] = o["at_restaurant_at"] or now
                    else:
                        o["at_customer_at"] = o["at_customer_at"] or now
                        o["delivered_at"] = o["delivered_at"] or now
                elif ev == "stop_failed" and kind == "dropoff":
                    o["cancelled"] = True
                    o["cancel_reason"] = "delivery failed"
            elif ev == "driver_location_updated":
                loc = d.get("driver_location") or {}
                self.set_rider(o, d.get("driver_id"), d.get("driver_name"), now) if d.get("driver_id") and not o["rider_id"] else None
                o["rider_lat"], o["rider_lng"] = loc.get("lat"), loc.get("lng")
                self.gps(d.get("driver_id") or o["rider_id"], loc.get("lat"), loc.get("lng"), now, bid)
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
                loc = d.get("location") or {}
                self.gps(rid, loc.get("lat"), loc.get("lng"), now)
            elif ev == "offline":
                r["online"] = False
            elif ev == "busy":
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
                        if st in DISPATCHED:                                     # tours are usually created on hold
                            self.dispatched(o, now)
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
                    if to == "claimed":
                        self.dispatched(o, now)
                        o["accepted_at"] = o["accepted_at"] or now
                        if users and not o["rider_id"]:
                            o["rider_id"] = users[0]
                            o["rider"] = self.rider(users[0])["name"]
                    elif to == "en_route":
                        self.dispatched(o, now)
                        o["started_at"] = o["started_at"] or now
                    elif to in DISPATCHED:
                        self.dispatched(o, now)
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
            elif ev == "driver_location_updated":
                for o in bookings:
                    o["rider_lat"], o["rider_lng"] = d.get("lat"), d.get("lng")
                    self.gps(o["rider_id"], d.get("lat"), d.get("lng"), now, o["id"])
            return name
        return name
