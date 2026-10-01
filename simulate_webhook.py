"""Test WEBHOOK mode against a FAKE MotionTools HTTP server in "restricted API mode".

    DASHBOARD_PASSWORD=test python3 simulate_webhook.py              -> http://127.0.0.1:8021/dashboard
    SIM_OPEN=none  ...   every read endpoint answers 403 restricted_endpoint (worst case)
    SIM_OPEN=detail ...  (default) list endpoints restricted, but /api/bookings/{id}, /api/users/{id}, /api/places/{id} open
    SIM_OPEN=all   ...   nothing restricted -> the server must switch to API mode by itself
    SIM_OPEN=real  ...   exactly what the real tenant answered on 2026-10-01: documented paths 404, lists restricted,
                         detail endpoints with a tiny hourly quota (429 restricted_rate_limit), rider detail open

The real mt.py client talks to the fake server, so probing, blocking and enrichment are exercised end to end.
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
SIM_OPEN = os.environ.get("SIM_OPEN", "detail")

import httpx  # noqa: E402
from fastapi import FastAPI, Response  # noqa: E402

import app as appmod  # noqa: E402
import mt as mtmod  # noqa: E402

FAKE_PORT = 8031
RESTRICTED = Response(content='{"error_code":"restricted_endpoint","description":"Your account is in restricted API mode and cannot access this endpoint. Please contact support."}',
                      status_code=403, media_type="application/json")
NOW = datetime.now(UTC)
m = lambda n: (NOW + timedelta(minutes=n)).isoformat(timespec="seconds")
AREA = "0d0bc288-f92a-4c34-a2cd-725838be6619"
CUST = "8e5b7276-7796-45b9-a431-8067c4dcbc6e"
PLACE_BK, PLACE_CHO = "7a1c-burgerking", "9f2e-choque"


def ev(rtype, event_name, t, **data):
    return {"id": "e", "timestamp": t, "resource_type": rtype, "event": event_name, "data": {"service_area_id": AREA, **data}}


BOOK: dict = {}          # booking id -> what the fake API knows about it
PLACES = {PLACE_BK: ("Burger King Freiham", "Bodenseestr. 200", 48.147, 11.428, "+49 89 1111111"),
          PLACE_CHO: ("Cho Que Harras", "Albert-Roßhaupter-Str. 4", 48.126, 11.539, "+49 89 2222222")}
RIDERS = {"r-ahmad": ("Ahmad", "Sabe", "+49 151 1000001"), "r-murat": ("Murat", "K.", "+49 151 1000002"),
          "r-obaida": ("Obaida", "H.", "+49 151 1000003"), "r-sven": ("Sven", "B.", "+49 151 1000007")}


def order_events(ref, bid, place, rider_id, rider, t0, wait_min=4, deliver=True, gps=True):
    p1, d1 = f"{bid}-p", f"{bid}-d"
    BOOK[bid] = {"ref": ref, "place": place, "rider_id": rider_id, "rider": rider, "delivered": bool(rider_id and wait_min is not None and deliver)}
    out = [ev("booking", "created", m(t0), booking_id=bid, external_id=ref, customer_id=CUST, place_ids=[place], status="to_be_dispatched"),
           ev("booking", "transition", m(t0 + 0.5), booking_id=bid, external_id=ref, **{"from": "to_be_dispatched", "to": "pickable", "event": "ready_to_pick"}),
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
           ev("tour", "created", m(-5.5), tour_id="t-9", dispatched_booking_ids=["b-live4"], status="on_hold"),
           ev("tour", "transition", m(-5), tour_id="t-9", **{"from": "on_hold", "to": "pickable", "event": "ready_to_pick"}),
           ev("tour", "transition", m(-4), tour_id="t-9", **{"from": "pickable", "to": "claimed", "event": "claim"}, affected_user_ids=["r-sven"])]
# pre-orders (Lieferando customers ordering hours ahead): created long ago, still ON HOLD -> no PTOD, no alerts
EVENTS += [ev("booking", "created", m(-120), booking_id="b-pre1", external_id="PRE001", customer_id=CUST, place_ids=[PLACE_BK], status="to_be_dispatched", scheduled_at=m(60)),
           ev("booking", "created", m(-95), booking_id="b-pre2", external_id="PRE002", customer_id=CUST, place_ids=[PLACE_CHO], status="to_be_dispatched", scheduled_at=m(45)),
           # created 130 min ago, dispatched 6 min ago, nobody accepted yet -> PTOD 6, "no rider" alert
           ev("booking", "created", m(-130), booking_id="b-pre3", external_id="PRE003", customer_id=CUST, place_ids=[PLACE_BK], status="to_be_dispatched", scheduled_at=m(10)),
           ev("booking", "transition", m(-6), booking_id="b-pre3", external_id="PRE003", **{"from": "to_be_dispatched", "to": "pickable", "event": "ready_to_pick"})]
# DOUBLE order: Obaida has A (picked up, delivering) and B (accepted in the same tour, not started) -> B is queued, no alert
EVENTS += order_events("DBL-A", "b-dblA", PLACE_BK, "r-obaida", "Obaida H.", -20, wait_min=3, deliver=False)
EVENTS += [ev("booking", "created", m(-19), booking_id="b-dblB", external_id="DBL-B", customer_id=CUST, place_ids=[PLACE_CHO], status="to_be_dispatched"),
           ev("booking", "transition", m(-18.5), booking_id="b-dblB", external_id="DBL-B", **{"from": "to_be_dispatched", "to": "pickable", "event": "ready_to_pick"}),
           ev("tour", "created", m(-18), tour_id="t-dbl", dispatched_booking_ids=["b-dblA", "b-dblB"], status="pickable"),
           ev("tour", "transition", m(-17), tour_id="t-dbl", **{"from": "pickable", "to": "claimed", "event": "claim"}, affected_user_ids=["r-obaida"])]
# HAND-BACK: Sven accepted, asked to be released, tour offered again, Ahmad took it
EVENTS += [ev("booking", "created", m(-15), booking_id="b-hb", external_id="HB1234", customer_id=CUST, place_ids=[PLACE_BK], status="to_be_dispatched"),
           ev("booking", "transition", m(-14), booking_id="b-hb", external_id="HB1234", **{"from": "to_be_dispatched", "to": "pickable", "event": "ready_to_pick"}),
           ev("tour", "created", m(-14), tour_id="t-hb", dispatched_booking_ids=["b-hb"], status="pickable"),
           ev("tour", "transition", m(-13), tour_id="t-hb", **{"from": "pickable", "to": "claimed", "event": "claim"}, affected_user_ids=["r-sven"]),
           ev("booking", "in_progress", m(-12), booking_id="b-hb", external_id="HB1234", driver_id="r-sven", driver_name="Sven B.", driver_location={"lat": 48.14, "lng": 11.56}),
           ev("booking", "stop_arrived", m(-9), booking_id="b-hb", external_id="HB1234", driver_id="r-sven", driver_name="Sven B.", stop_id="b-hb-p", stop_type="pickup", stop_position=1),
           ev("tour", "transition", m(-7), tour_id="t-hb", **{"from": "claimed", "to": "pickable", "event": "unclaim"}, affected_user_ids=["r-sven"]),
           ev("booking", "in_progress", m(-3), booking_id="b-hb", external_id="HB1234", driver_id="r-ahmad", driver_name="Ahmad Sabe", driver_location={"lat": 48.14, "lng": 11.56}),
           ev("tour", "force_assigned", m(-1), tour_id="t-hb", driver_id="r-murat")]
# pre-orders as Lieferando/MotionTools really create them: already "dispatched" (in a tour on hold) with a planned delivery
EVENTS += [ev("booking", "created", m(-100), booking_id="b-plan1", external_id="PLAN60", customer_id=CUST, place_ids=[PLACE_BK], status="dispatched"),
           ev("booking", "etas_recalculated", m(-99), booking_id="b-plan1", external_id="PLAN60", customer_id=CUST,
              unfinished_stops_info=[{"id": "b-plan1-p", "position": 1, "type": "pickup", "eta": m(45)}, {"id": "b-plan1-d", "position": 2, "type": "dropoff", "eta": m(60)}]),   # planned +60 -> on hold (released at +15)
           ev("booking", "created", m(-90), booking_id="b-plan2", external_id="PLAN20", customer_id=CUST, place_ids=[PLACE_CHO], status="dispatched"),
           ev("booking", "etas_recalculated", m(-89), booking_id="b-plan2", external_id="PLAN20", customer_id=CUST,
              unfinished_stops_info=[{"id": "b-plan2-p", "position": 1, "type": "pickup", "eta": m(10)}, {"id": "b-plan2-d", "position": 2, "type": "dropoff", "eta": m(20)}]),   # planned +20 -> released 25 min ago -> waiting, no rider
           ev("booking", "created", m(-85), booking_id="b-plan3", external_id="PLAN01", customer_id=CUST, place_ids=[PLACE_BK], status="dispatched"),
           ev("booking", "etas_recalculated", m(-84), booking_id="b-plan3", external_id="PLAN01", customer_id=CUST,
              unfinished_stops_info=[{"id": "b-plan3-p", "position": 1, "type": "pickup", "eta": m(36)}, {"id": "b-plan3-d", "position": 2, "type": "dropoff", "eta": m(46)}])]   # planned +46 -> releases in ~1 min (timer test)
# an order MotionTools stopped talking about 4 h ago -> must be closed automatically, alerts resolved
EVENTS += order_events("OLD999", "b-old", PLACE_CHO, "r-ahmad", "Ahmad Sabe", -250, wait_min=None)
EVENTS.sort(key=lambda e: e["timestamp"])
BOOK["b-live4"] = {"ref": "KMW86T", "place": PLACE_CHO, "rider_id": "r-sven", "rider": "Sven B.", "delivered": False}
for b, ref, pl in (("b-pre1", "PRE001", PLACE_BK), ("b-pre2", "PRE002", PLACE_CHO), ("b-pre3", "PRE003", PLACE_BK)):
    BOOK[b] = {"ref": ref, "place": pl, "rider_id": None, "rider": "", "delivered": False}
for b, ref, pl in (("b-plan1", "PLAN60", PLACE_BK), ("b-plan2", "PLAN20", PLACE_CHO), ("b-plan3", "PLAN01", PLACE_BK)):
    BOOK[b] = {"ref": ref, "place": pl, "rider_id": None, "rider": "", "delivered": False}
BOOK["b-dblB"] = {"ref": "DBL-B", "place": PLACE_CHO, "rider_id": "r-obaida", "rider": "Obaida H.", "delivered": False}
BOOK["b-hb"] = {"ref": "HB1234", "place": PLACE_BK, "rider_id": "r-ahmad", "rider": "Ahmad Sabe", "delivered": False}


# ---------------------------------------------------------------- fake MotionTools HTTP server
fake = FastAPI()


POSTED: dict = {}        # booking id -> {event name: timestamp} of events already delivered (the API never knows the future)


def booking_json(bid: str) -> dict:
    i = BOOK[bid]
    name, street, plat, plng, pphone = PLACES[i["place"]]
    seen = POSTED.get(bid, {})
    rid = i["rider_id"] if ("in_progress" in seen or "claimed" in seen) else None
    prof = RIDERS.get(rid)
    done = "done" in seen
    disp = seen.get("transition")                    # the fake only "dispatches" once that transition was delivered
    events = [{"name": "ready_to_pick", "status": "pickable", "timestamp": disp}] if disp else []
    if "claimed" in seen:
        events.append({"name": "claimed", "status": "claimed", "timestamp": seen["claimed"]})
    if "in_progress" in seen:
        events.append({"name": "en_route", "status": "en_route", "timestamp": seen["in_progress"]})
    return {"id": bid, "external_id": i["ref"], "status": "done" if done else ("en_route" if rid else ("pickable" if disp else "to_be_dispatched")),
            "created_at": seen.get("created") or m(-30), "service_area": {"id": AREA, "name": "München"},
            "driver": {"id": rid, "profile": {"first_name": prof[0], "last_name": prof[1]}} if rid and prof else None,
            "driver_location": {"lat": 48.1501, "lng": 11.5702} if rid and not done else None,
            "stops": [{"id": f"{bid}-p", "type": "pickup", "lat": plat, "lng": plng, "place_id": i["place"], "place": {"name": name},
                       "phone_number": pphone, "street": street.rsplit(" ", 1)[0], "number": street.rsplit(" ", 1)[1], "city": "München",
                       "zip_code": "81249", "status": "scheduled"},
                      {"id": f"{bid}-d", "type": "dropoff", "lat": plat + 0.01, "lng": plng + 0.02, "street": "Leopoldstr.", "number": "12",
                       "city": "München", "zip_code": "80802", "phone_number": "+49 170 0000000", "status": "scheduled"}],
            "events": [e for e in events if e["timestamp"]], "total_estimated_distance_meters": 3400}


QUOTA = {"detail": 3, "place": 2}        # SIM_OPEN=real: what the real tenant did on 2026-10-01 — tiny hourly quotas
LIMITED = Response(content='{"error_code":"restricted_rate_limit","description":"Your account is in restricted API access mode and has reached the hourly limit for this endpoint."}',
                   status_code=429, media_type="application/json")
NOT_FOUND = Response(content='{"error_code":"not_found"}', status_code=404, media_type="application/json")


def open_(kind: str) -> bool:
    return SIM_OPEN == "all" or (SIM_OPEN == "detail" and kind == "detail")


def quota(kind: str):
    """SIM_OPEN=real: answer 429 once the tiny hourly quota is used up."""
    QUOTA[kind] -= 1
    return None if QUOTA[kind] >= 0 else LIMITED


@fake.get("/api/user")
def f_me():
    if SIM_OPEN == "real":
        return RESTRICTED
    return {"user": {"id": "admin-1", "role": "admin", "email": "ops@quickzi.de"}}


@fake.get("/api/bookings/active")
@fake.get("/api/bookings")
def f_list_404():
    if SIM_OPEN == "real":
        return NOT_FOUND
    return f_list()


@fake.get("/api/hailing/bookings")
def f_list():
    if not open_("list"):
        return RESTRICTED
    return {"results": [booking_json(b) for b in BOOK if not BOOK[b]["delivered"]], "meta": {"pagination": {"next": None}}}


@fake.get("/api/bookings/{bid}")
def f_detail_404(bid: str):
    if SIM_OPEN == "real":
        return NOT_FOUND
    return f_detail(bid)


@fake.get("/api/hailing/bookings/{bid}")
def f_detail(bid: str):
    if SIM_OPEN == "real":
        lim = quota("detail")
        if lim is not None:
            return lim
    elif not open_("detail"):
        return RESTRICTED
    if bid not in BOOK:
        return Response(content='{"error_code":"not_found"}', status_code=404, media_type="application/json")
    return {"booking": booking_json(bid)}


@fake.get("/api/users")
def f_users():
    if not open_("list"):
        return RESTRICTED
    return {"results": [{"id": rid, "role": "driver", "status": "online", "profile": {"first_name": p[0], "last_name": p[1], "phone_number": p[2]},
                         "location": {"lat": 48.14, "lng": 11.56}, "active_hailing_booking_ids": []} for rid, p in RIDERS.items()],
            "meta": {"pagination": {"next": None}}}


@fake.get("/api/users/{rid}")
def f_user(rid: str):
    if not open_("detail") and SIM_OPEN != "real":
        return RESTRICTED
    p = RIDERS.get(rid)
    if not p:
        return Response(content='{"error_code":"not_found"}', status_code=404, media_type="application/json")
    return {"user": {"id": rid, "role": "driver", "status": "offline" if rid == "r-sven" else "online",
                     "profile": {"first_name": p[0], "last_name": p[1], "phone_number": p[2]}}}


@fake.get("/api/places/{pid}")
def f_place(pid: str):
    if SIM_OPEN == "real":
        lim = quota("place")
        if lim is not None:
            return lim
    elif not open_("detail"):
        return RESTRICTED
    if pid not in PLACES:
        return Response(content='{"error_code":"not_found"}', status_code=404, media_type="application/json")
    name, street, lat, lng, phone = PLACES[pid]
    return {"place": {"id": pid, "name": name, "formatted_address": f"{street}, München", "lat": lat, "lng": lng}}


def post_all():
    time.sleep(4)
    for e in EVENTS:
        d = e["data"]
        if e["resource_type"] == "booking" and d.get("booking_id"):
            key = "done" if (e["event"] == "transition" and d.get("to") == "done") else e["event"]
            POSTED.setdefault(d["booking_id"], {}).setdefault(key, e["timestamp"])
        if e["resource_type"] == "driver" and e["event"] == "busy":          # the fake tour claim: next in_progress belongs to it
            pass
        if e["resource_type"] == "tour" and e["event"] == "transition" and d.get("to") == "claimed":
            for b in ("b-live4",):
                POSTED.setdefault(b, {}).setdefault("claimed", e["timestamp"])
        req = urllib.request.Request("http://127.0.0.1:8021/mt/abc", data=json.dumps(e).encode(), headers={"content-type": "application/json"})
        urllib.request.urlopen(req)
    print(f"posted {len(EVENTS)} events")


if __name__ == "__main__":
    import uvicorn
    # point the real client at the fake server
    appmod.mt._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{FAKE_PORT}", timeout=20)
    threading.Thread(target=lambda: uvicorn.run(fake, host="127.0.0.1", port=FAKE_PORT, log_level="warning"), daemon=True).start()
    if os.environ.get("SIM_POST", "1") != "0":
        threading.Thread(target=post_all, daemon=True).start()
    print(f"Dashboard: http://127.0.0.1:8021/dashboard  (password: test)   fake MotionTools: SIM_OPEN={SIM_OPEN}")
    uvicorn.run(appmod.app, host="127.0.0.1", port=8021, log_level="warning")
