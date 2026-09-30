"""Quickzi ops platform — server.

API mode:     truth comes from MotionTools' API every SYNC_SECONDS (all active orders + all riders), 24/7;
              the MotionTools webhook only wakes the sync early.
Webhook mode: the account is in "restricted API mode" — orders are rebuilt from the events MotionTools pushes,
              and every endpoint that is still open (booking detail, rider detail, restaurant detail) is used
              to fill in what the events do not carry.  The server probes the endpoints itself, every hour.
Everything is written to SQLite on the Railway volume, so nothing is lost while nobody is watching.

Env:  MT_API_TOKEN, DASHBOARD_PASSWORD, WEBHOOK_PATH_SECRET, DATA_DIR (/data),
      MUNICH_SERVICE_AREA_ID (comma-separated area ids to keep; empty = all), SYNC_SECONDS (30), CITY_NAME
Run:  uvicorn app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from events import Projector
from mt import ACTIVE_STATUSES, PLACE_PATH, USER_PATH, MotionTools
from orders import BERLIN, UTC, RiderTracker, Rules, evaluate, iso, mins, parse_booking, phase_minutes
from store import Store, day_key, day_start

log = logging.getLogger("quickzi")
logging.basicConfig(level=logging.INFO)


def env(name, default=""):
    val = os.environ.get(name)
    if val is None:
        for k, v in os.environ.items():
            if k.strip(" `'\"") == name:
                val = v
                break
    return (val if val is not None else default).strip().strip("`'\"").strip()


MT_TOKEN = env("MT_API_TOKEN")
PATH_SECRET = env("WEBHOOK_PATH_SECRET", "change-me")
DASH_PASSWORD = env("DASHBOARD_PASSWORD")
DATA_DIR = Path(env("DATA_DIR", ".") or ".")
DATA_DIR.mkdir(parents=True, exist_ok=True)
AREAS = [a.strip() for a in env("MUNICH_SERVICE_AREA_ID").split(",") if a.strip()]
SYNC_SECONDS = int(env("SYNC_SECONDS", "30") or 30)
CITY = env("CITY_NAME", "Munich") or "Munich"
STARTED = datetime.now(UTC)

mt = MotionTools(MT_TOKEN)
store = Store(str(DATA_DIR / "quickzi.db"))
rules = Rules()
rules.apply(store.get_settings())
tracker = RiderTracker()
app = FastAPI()
basic = HTTPBasic()

STATE = {"orders": {}, "riders": {}, "open_alerts": {}, "sev": {}, "raw_samples": {},
         "sync": {"mode": "api", "last_ok": None, "last_error": None, "orders_seen": 0, "riders_seen": 0, "runs": 0,
                  "webhook_events": 0, "last_webhook": None, "backfilled": 0, "last_snapshot": None,
                  "api_retry_at": None, "events": {}, "enriched": 0, "probe_at": None}}
WAKE = asyncio.Event()
projector = Projector(STATE, store, tracker, AREAS)
ENRICHED_RIDERS: dict = {}          # rider_id -> when we last read it through the API
ENRICH_LOCK = asyncio.Lock()


def api_restricted() -> bool:
    return "restricted_endpoint" in (mt.stats.get("last_error") or "")

PHASE_LABEL = {"unassigned": "Waiting for rider", "accepted": "Accepted, not started", "to_restaurant": "Riding to restaurant",
               "at_restaurant": "At restaurant", "to_customer": "Delivering", "at_customer": "At customer",
               "delivered": "Delivered", "cancelled": "Cancelled"}


def require_login(creds: HTTPBasicCredentials = Depends(basic)):
    typed = creds.password.strip()
    if not DASH_PASSWORD or not secrets.compare_digest(typed.encode(), DASH_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})


# ====================================================================== sync
def rider_name(u: dict) -> str:
    p = u.get("profile") or {}
    return " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x).strip() or u.get("email") or "Rider"


async def sync_riders(now: datetime):
    rows = await mt.list_drivers(AREAS)
    if rows is None:
        return
    if rows and "rider" not in STATE["raw_samples"]:
        STATE["raw_samples"]["rider"] = rows[0]
    busy_order = {o["rider_id"]: o["id"] for o in STATE["orders"].values() if o["rider_id"]}
    for u in rows:
        rid = u.get("id")
        if not rid:
            continue
        loc = u.get("location") or {}
        online = u.get("status") == "online"
        mt_phone = (u.get("profile") or {}).get("phone_number") or ""
        info = {"id": rid, "name": rider_name(u), "phone": projector.phone_for(rid, mt_phone), "mt_phone": mt_phone,
                "online": online, "lat": loc.get("lat"), "lng": loc.get("lng"),
                "active_ids": u.get("active_hailing_booking_ids") or u.get("active_booking_ids") or []}
        STATE["riders"][rid] = info
        store.upsert_rider(rid, info["name"], info["phone"], online, info["lat"], info["lng"], info["active_ids"], now)
        if online:
            tracker.push(rid, info["lat"], info["lng"], now)
            if rid in busy_order or info["active_ids"]:
                store.record_position(rid, info["lat"], info["lng"], busy_order.get(rid), now)
    STATE["sync"]["riders_seen"] = len(rows)


async def sync_orders(now: datetime):
    rows = await mt.list_bookings(AREAS, ACTIVE_STATUSES)
    if rows is None:
        STATE["sync"]["last_error"] = mt.stats["last_error"]
        return False
    if rows and "booking" not in STATE["raw_samples"]:
        STATE["raw_samples"]["booking"] = rows[0]
    parsed = [parse_booking(b) for b in rows]
    per_rider = {}
    for o in parsed:
        if o["rider_id"]:
            per_rider[o["rider_id"]] = per_rider.get(o["rider_id"], 0) + 1
    seen = set()
    for o in parsed:
        if not o["id"]:
            continue
        seen.add(o["id"])
        STATE["orders"][o["id"]] = o
        store.upsert_order(o, now, stacked=per_rider.get(o["rider_id"], 0) >= 2)
        if o["rider_id"] and o["rider_lat"] is not None:
            tracker.push(o["rider_id"], o["rider_lat"], o["rider_lng"], now)
            store.record_position(o["rider_id"], o["rider_lat"], o["rider_lng"], o["id"], now)
    for oid in [k for k in STATE["orders"] if k not in seen]:
        b = await mt.get_booking(oid)
        o = parse_booking(b) if b else None
        if o and o["id"]:
            if o["phase"] not in ("delivered", "cancelled"):
                o["phase"] = "delivered" if o["delivered_at"] else "cancelled"
            store.upsert_order(o, now, stacked=False)
            why = "delivered" if o["phase"] == "delivered" else "cancelled"
        else:
            why = "order closed"
        for key in [k for k in STATE["open_alerts"] if k[0] == oid]:
            store.resolve_alert(STATE["open_alerts"].pop(key), why, now)
            STATE["sev"].pop(key, None)
        STATE["orders"].pop(oid, None)
    store.close_missing(seen, now)
    STATE["sync"]["orders_seen"] = len(seen)
    STATE["sync"]["last_ok"] = iso(now)
    STATE["sync"]["last_error"] = None
    return True


async def backfill_done(day: datetime) -> int:
    """Pull every finished order of one (Berlin) day, so reports are complete even after downtime."""
    date = day.astimezone(BERLIN).strftime("%Y-%m-%d")
    n = 0
    for statuses in (["done", "paid", "processing_payment"], ["cancelled"]):
        rows = await mt.list_bookings(AREAS, statuses, extra={"local_done_at": date}, history=True)
        if rows is None:
            rows = await mt.list_bookings(AREAS, statuses, extra={"date": date}, history=True)
        for b in rows or []:
            o = parse_booking(b)
            if o["id"] and (o["delivered_at"] or o["phase"] == "cancelled"):
                store.upsert_order(o, datetime.now(UTC), stacked=False)
                n += 1
    STATE["sync"]["backfilled"] += n
    return n


# ====================================================================== webhook mode: use whatever the API still allows
async def probe_endpoints(now: datetime, quiet: bool = False):
    """Test every MotionTools endpoint once (startup + hourly). Restricted ones are skipped until the next probe."""
    if not mt.enabled:
        return
    sample_order = next(iter(STATE["orders"].values()), None) or next(iter(store.orders_in("week", now)), None)
    sample_place = next((o.get("place_id") for o in [sample_order] if o and o.get("place_id")), None) \
        or next(iter(projector.places), None)
    sample_rider = (sample_order or {}).get("rider_id") or next(iter(STATE["riders"]), None)
    await mt.probe(booking_id=(sample_order or {}).get("id"), place_id=sample_place, user_id=sample_rider)
    STATE["sync"]["probe_at"] = iso(now)
    if not quiet:
        store.log("info", "MotionTools endpoint check: " + mt.endpoint_summary())


async def enrich_order(bid: str, now: datetime = None) -> bool:
    """Webhook mode: read one booking through the API (if the detail endpoint is open) and fill the gaps."""
    if not mt.enabled or not mt.detail_available():
        return False
    b = await mt.get_booking(bid)
    if not b:
        return False
    now = now or datetime.now(UTC)
    if "booking" not in STATE["raw_samples"]:
        STATE["raw_samples"]["booking"] = b
    async with ENRICH_LOCK:
        o = STATE["orders"].get(bid) or store.order(bid)
        if o is None:
            return False
        o.setdefault("stop_types", {})
        p = parse_booking(b)
        if AREAS and p.get("area") and p["area"] not in AREAS:
            return False
        projector.merge_api(o, p, now)
        STATE["sync"]["enriched"] += 1
        if p.get("rider_id"):
            r = STATE["riders"].get(p["rider_id"])
            if r is not None and p.get("rider") and not r.get("name"):
                r["name"] = p["rider"]
    return True


async def enrich_rider(rid: str, now: datetime = None) -> bool:
    """Webhook mode: name + phone of a rider through /api/users/{id} (once a day per rider, if open)."""
    if not rid or not mt.enabled or mt.blocked(USER_PATH):
        return False
    now = now or datetime.now(UTC)
    last = ENRICHED_RIDERS.get(rid)
    if last and now - last < timedelta(hours=24):
        return False
    ENRICHED_RIDERS[rid] = now
    u = await mt.get_user(rid)
    if not u:
        return False
    r = projector.rider(rid)
    name = rider_name(u)
    if name and name != "Rider":
        r["name"] = name
    mt_phone = (u.get("profile") or {}).get("phone_number") or u.get("phone_number") or ""
    if mt_phone:
        r["mt_phone"] = mt_phone
        r["phone"] = projector.phone_for(rid, mt_phone)
    if "rider" not in STATE["raw_samples"]:
        STATE["raw_samples"]["rider"] = u
    store.upsert_rider(rid, r["name"] or "Rider", r["phone"], r["online"], r.get("lat"), r.get("lng"),
                       [o["id"] for o in STATE["orders"].values() if o["rider_id"] == rid], now)
    for o in STATE["orders"].values():
        if o["rider_id"] == rid and not o["rider"]:
            o["rider"] = r["name"]
    return True


async def enrich_place(pid: str) -> bool:
    """Webhook mode: restaurant name through /api/places/{id} (if open) — otherwise typed in Settings."""
    if not pid or projector.places.get(pid) or not mt.enabled or mt.blocked(PLACE_PATH):
        return False
    p = await mt.get_place(pid)
    if not p:
        return False
    name = p.get("name") or p.get("title") or ""
    addr = p.get("formatted_address") or p.get("address") or ""
    if isinstance(addr, dict):
        addr = " ".join(str(x) for x in [addr.get("street"), addr.get("house_number"), addr.get("zip_code"), addr.get("city")] if x)
    if name:
        projector.set_place(pid, name)
        store.log("info", f"restaurant named from MotionTools: {name}" + (f" ({addr})" if addr else ""))
        return True
    return False


async def enrich_after_event(p: dict):
    """Runs in the background after each webhook event so the webhook answers MotionTools immediately."""
    d = p.get("data") or {}
    rtype, ev = str(p.get("resource_type") or ""), str(p.get("event") or "")
    try:
        if rtype == "booking" and ev == "created":
            pids = d.get("place_ids") or []
            await enrich_place(pids[0] if isinstance(pids, list) and pids else (pids if isinstance(pids, str) else ""))
            if await enrich_order(d.get("booking_id")):
                evaluate_all(datetime.now(UTC))
        elif rtype == "booking" and d.get("driver_id"):
            await enrich_rider(d.get("driver_id"))
        elif rtype == "driver" and d.get("driver_id"):
            await enrich_rider(d.get("driver_id"))
        elif rtype == "tour" and ev == "transition" and d.get("to") == "claimed":
            users = d.get("affected_user_ids") or []
            await enrich_rider(users[0] if isinstance(users, list) and users else users if isinstance(users, str) else "")
    except Exception as e:
        log.exception("enrichment failed: %s", e)


async def refresh_live_orders(now: datetime):
    """Webhook mode, once a minute: re-read every live order through the open detail endpoint —
    gives rider GPS, ETAs and status even without the GPS webhook."""
    if not mt.stats.get("detail_path"):
        return
    changed = False
    for bid in list(STATE["orders"])[:40]:
        if await enrich_order(bid, now):
            changed = True
    if changed:
        evaluate_all(datetime.now(UTC))


def alert_payload(o: dict, c: dict) -> dict:
    r = STATE["riders"].get(o["rider_id"] or "", {})
    lat, lng = (r.get("lat"), r.get("lng")) if r.get("lat") is not None else (o["rider_lat"], o["rider_lng"])
    return {"order_id": o["id"], "order_ref": o["ref"], "rider_id": o["rider_id"], "rider": o["rider"] or r.get("name", ""),
            "kind": c["kind"], "severity": c["severity"], "headline": c["headline"], "action": c["action"],
            "restaurant": o["restaurant"], "phone": r.get("phone") or "", "restaurant_phone": o["restaurant_phone"],
            "map_url": f"https://maps.google.com/?q={lat:.5f},{lng:.5f}" if lat is not None else ""}


def evaluate_all(now: datetime):
    for o in STATE["orders"].values():
        r = STATE["riders"].get(o["rider_id"] or "")
        conds = {c["kind"]: c for c in evaluate(o, now, rules, tracker, r["online"] if r else None)}
        for kind, c in conds.items():
            key = (o["id"], kind)
            payload = alert_payload(o, c)
            STATE["sev"][key] = c["severity"]
            if key in STATE["open_alerts"]:
                store.update_alert(STATE["open_alerts"][key], payload, now)
            else:
                STATE["open_alerts"][key] = store.open_alert(payload, now)
        for key in [k for k in STATE["open_alerts"] if k[0] == o["id"] and k[1] not in conds]:
            store.resolve_alert(STATE["open_alerts"].pop(key), PHASE_LABEL.get(o["phase"], o["phase"]).lower(), now)
            STATE["sev"].pop(key, None)


def snapshot_yesterday(now: datetime):
    """Freeze yesterday's report once per day (after 04:05 Berlin)."""
    local = now.astimezone(BERLIN)
    yday = day_key(now - timedelta(days=1))
    if STATE["sync"]["last_snapshot"] == yday or (local.hour == 4 and local.minute < 5):
        return
    if store.daily(yday) is None or STATE["sync"]["last_snapshot"] is None:
        data = store.insights(yday, now, rules)
        data["day"] = yday
        store.save_daily(yday, data)
        store.log("info", f"daily report frozen for {yday}: {data['delivered']} delivered, {data['within_pct']}% within target")
    STATE["sync"]["last_snapshot"] = yday


async def sync_loop():
    first = True
    last_backfill = None
    last_refresh = None
    while True:
        now = datetime.now(UTC)
        STATE["sync"]["runs"] += 1
        try:
            if mt.enabled and first:
                await probe_endpoints(now)                 # learn what this token may read before doing anything
            mode = STATE["sync"]["mode"]
            if mt.enabled and mode == "webhook":
                # every 30 min: probe again and retry the list endpoints — the moment MotionTools opens them we switch back
                retry_at = STATE["sync"]["api_retry_at"]
                if retry_at is None or now >= datetime.fromisoformat(retry_at):
                    STATE["sync"]["api_retry_at"] = iso(now + timedelta(minutes=30))
                    if not first:
                        await probe_endpoints(now, quiet=True)
                    if await mt.list_bookings(AREAS, ACTIVE_STATUSES) is not None:
                        STATE["sync"]["mode"] = "api"
                        store.log("info", f"MotionTools bookings endpoint works ({mt.stats['bookings_path']}) — switching to API mode")
                        first = True
                    elif mt.stats.get("detail_path"):
                        for o in list(STATE["orders"].values()):        # finished while we were down? close them now
                            await enrich_order(o["id"], now)
                if last_refresh is None or (now - last_refresh) >= timedelta(seconds=60):
                    await refresh_live_orders(now)
                    last_refresh = now
                STATE["sync"]["orders_seen"] = len(STATE["orders"])
                STATE["sync"]["riders_seen"] = sum(1 for r in STATE["riders"].values() if r.get("online"))
                STATE["sync"]["last_ok"] = iso(now)       # webhook mode is healthy as long as we run
            if mt.enabled and STATE["sync"]["mode"] == "api":
                await sync_riders(now)
                ok = await sync_orders(now)
                if not ok and (api_restricted() or all(mt.blocked(p) for p in ("/api/bookings/active", "/api/bookings", "/api/hailing/bookings"))):
                    STATE["sync"]["mode"] = "webhook"
                    STATE["sync"]["last_error"] = None
                    STATE["sync"]["api_retry_at"] = iso(now + timedelta(minutes=30))
                    store.log("error", "MotionTools has this account in restricted API mode — running in WEBHOOK mode "
                                       "(orders rebuilt from events; open endpoints: " + mt.endpoint_summary() + ")")
                    ok = False
                    first = False
                if first and ok:
                    # first run on an empty database: pull the last 7 days so week/month views are populated
                    days = 7 if not store.orders_in("week", now) else 2
                    total = 0
                    for i in range(days):
                        total += await backfill_done(now - timedelta(days=i))
                    store.log("info", f"backfilled {total} finished orders from the last {days} days")
                    last_backfill = now
                elif last_backfill is None or (now - last_backfill) > timedelta(minutes=10):
                    await backfill_done(now)
                    last_backfill = now
                if not ok and STATE["sync"]["mode"] == "api":
                    store.log("error", f"MotionTools sync failed: {mt.stats['last_error']}")
            evaluate_all(datetime.now(UTC))
            snapshot_yesterday(now)
            if now.minute == 30 and now.second < SYNC_SECONDS:
                store.cleanup(now)
            first = False
        except Exception as e:
            log.exception("sync failed: %s", e)
            STATE["sync"]["last_error"] = str(e)[:300]
            store.log("error", f"sync exception: {e}"[:300])
        try:
            await asyncio.wait_for(WAKE.wait(), timeout=SYNC_SECONDS)
            await asyncio.sleep(2)
        except asyncio.TimeoutError:
            pass
        WAKE.clear()


@app.on_event("startup")
async def startup():
    now = datetime.now(UTC)
    store.close_stale_sessions(now)
    for o in store.open_orders():
        STATE["orders"][o["id"]] = o
    for a in store.open_alerts():
        STATE["open_alerts"][(a["order_id"], a["kind"])] = a["id"]
        STATE["sev"][(a["order_id"], a["kind"])] = a["severity"]
    for r in store.riders():
        STATE["riders"][r["id"]] = {"id": r["id"], "name": r["name"], "phone": projector.phone_for(r["id"], r["phone"]),
                                    "mt_phone": r["phone"] if r["phone"] != projector.phones.get(r["id"]) else "",
                                    "online": bool(r["online"]), "lat": r["lat"], "lng": r["lng"], "active_ids": r["active_ids"]}
    store.log("info", f"server started — {len(STATE['orders'])} open orders, {len(STATE['open_alerts'])} open alerts restored")
    if not DASH_PASSWORD:
        store.log("error", "DASHBOARD_PASSWORD not set — dashboard refuses all logins")
    if not mt.enabled:
        store.log("error", "MT_API_TOKEN not set — no data will be pulled from MotionTools")
    asyncio.create_task(sync_loop())


# ====================================================================== webhook (wakes the sync)
@app.post("/mt/{secret}")
async def webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, PATH_SECRET):
        raise HTTPException(404)
    try:
        p = await request.json()
    except Exception:
        p = {}
    STATE["sync"]["webhook_events"] += 1
    STATE["sync"]["last_webhook"] = iso(datetime.now(UTC))
    with open(DATA_DIR / "events.jsonl", "a") as f:
        f.write(json.dumps(p) + "\n")
    if len(STATE["raw_samples"].get("events", [])) < 12:
        STATE["raw_samples"].setdefault("events", []).append(p)
    if STATE["sync"]["mode"] == "webhook":
        try:
            name = projector.apply(p)
            STATE["sync"]["events"][name] = STATE["sync"]["events"].get(name, 0) + 1
            evaluate_all(datetime.now(UTC))
            if mt.enabled and name != "other area":
                asyncio.create_task(enrich_after_event(p))      # fill names / phones / GPS through open endpoints
        except Exception as e:
            log.exception("event failed: %s", e)
            store.log("error", f"event {p.get('resource_type')}.{p.get('event')} failed: {e}"[:300])
    else:
        WAKE.set()
    return {"ok": True}


# ====================================================================== API
def hm(dt):
    return dt.astimezone(BERLIN).strftime("%H:%M") if dt else None


def order_view(o: dict, now: datetime) -> dict:
    r = STATE["riders"].get(o["rider_id"] or "", {})
    live = o["phase"] not in ("delivered", "cancelled")
    end = o["delivered_at"] or o.get("cancelled_at") or now
    elapsed = mins(o["dispatched_at"], end if not live else now)
    kinds = [k[1] for k in STATE["open_alerts"] if k[0] == o["id"]] if live else []
    sevs = [STATE["sev"].get((o["id"], k)) for k in kinds]
    lat, lng = (r.get("lat"), r.get("lng")) if r.get("lat") is not None else (o.get("rider_lat"), o.get("rider_lng"))
    return {"id": o["id"], "ref": o["ref"], "rider": o["rider"] or r.get("name") or "", "rider_id": o["rider_id"],
            "phone": r.get("phone") or "", "restaurant": o["restaurant"], "restaurant_phone": o.get("restaurant_phone", ""),
            "customer_addr": o["customer_addr"], "customer_zip": o.get("customer_zip", ""), "phase": o["phase"],
            "phase_label": PHASE_LABEL.get(o["phase"], o["phase"]), "elapsed": int(elapsed) if elapsed is not None else None,
            "target": rules.ptod_target_min, "eta_customer": hm(o.get("eta_customer")), "eta_restaurant": hm(o.get("eta_restaurant")),
            "timeline": {k: hm(o.get(k + "_at")) for k in ("dispatched", "accepted", "started", "at_restaurant", "picked_up", "at_customer", "delivered", "cancelled")},
            "phases": phase_minutes(o), "alerts": kinds, "severity": "red" if "red" in sevs else ("amber" if kinds else ""),
            "stacked": bool(o.get("stacked")), "cancel_reason": o.get("cancel_reason", ""),
            "map_url": f"https://maps.google.com/?q={lat:.5f},{lng:.5f}" if lat is not None and live else "",
            "rider_online": r.get("online")}


@app.get("/api/state", dependencies=[Depends(require_login)])
def api_state():
    now = datetime.now(UTC)
    alerts = store.alerts_for_ui(now)
    orders = sorted((order_view(o, now) for o in STATE["orders"].values()), key=lambda v: -(v["elapsed"] or 0))
    busy = {}
    for o in STATE["orders"].values():
        if o["rider_id"]:
            busy[o["rider_id"]] = busy.get(o["rider_id"], 0) + 1
    riders = []
    for r in STATE["riders"].values():
        n = busy.get(r["id"], 0) or len(r.get("active_ids") or [])
        fix = tracker.last_fix(r["id"])
        riders.append({"id": r["id"], "name": r["name"], "phone": r["phone"], "online": r["online"], "orders": n,
                       "status": "offline" if not r["online"] else ("busy" if n else "idle"),
                       "map_url": f"https://maps.google.com/?q={r['lat']:.5f},{r['lng']:.5f}" if r.get("lat") is not None else "",
                       "still_min": tracker.stationary_minutes(r["id"], now, rules.stationary_radius_m) if r["online"] else None,
                       "last_fix_min": int((now - fix[0]).total_seconds() // 60) if fix else None})
    riders.sort(key=lambda x: ({"busy": 0, "idle": 1, "offline": 2}[x["status"]], -x["orders"], x["name"]))
    today = store.delivered("today", now)
    ptods = [o["phases"]["ptod"] for o in today if o["phases"]["ptod"] is not None]
    open_alerts = [a for a in alerts if a["resolved_at"] is None]
    last_ok = STATE["sync"]["last_ok"]
    stale = (not last_ok) or (now - datetime.fromisoformat(last_ok)).total_seconds() > max(180, SYNC_SECONDS * 4)
    pulse = {"delivered": len(today),
             "within_pct": round(100 * sum(1 for p in ptods if p <= rules.ptod_target_min) / len(ptods)) if ptods else None,
             "target_within_pct": rules.target_within_pct, "target": rules.ptod_target_min,
             "avg_ptod": round(mean(ptods)) if ptods else None,
             "live_orders": len(orders), "unassigned": sum(1 for o in orders if o["phase"] == "unassigned"),
             "riders_online": sum(1 for r in riders if r["online"]), "riders_idle": sum(1 for r in riders if r["status"] == "idle"),
             "riders_busy": sum(1 for r in riders if r["status"] == "busy"),
             "red": sum(1 for a in open_alerts if a["severity"] == "red"), "amber": sum(1 for a in open_alerts if a["severity"] == "amber")}
    return {"now": iso(now), "city": CITY, "pulse": pulse, "alerts": alerts, "orders": orders, "riders": riders,
            "sync": {**STATE["sync"], "stale": stale and mt.enabled, "api": mt.stats, "areas": AREAS}}


@app.get("/api/places", dependencies=[Depends(require_login)])
def api_places():
    """Restaurants seen as MotionTools place ids (webhook mode) with the names given in Settings."""
    seen = {}
    for o in store.orders_in("month", datetime.now(UTC)):
        pid = o.get("place_id")
        if pid:
            seen[pid] = seen.get(pid, 0) + 1
    for o in STATE["orders"].values():
        if o.get("place_id"):
            seen.setdefault(o["place_id"], 0)
    return {"places": [{"id": pid, "name": projector.places.get(pid, ""), "orders": n}
                       for pid, n in sorted(seen.items(), key=lambda x: -x[1])]}


@app.post("/api/places", dependencies=[Depends(require_login)])
async def api_places_set(request: Request):
    body = await request.json()
    for pid, name in (body or {}).items():
        if pid and isinstance(name, str):
            projector.set_place(pid, name.strip())
    evaluate_all(datetime.now(UTC))
    return {"ok": True}


@app.get("/api/riders", dependencies=[Depends(require_login)])
def api_riders():
    """Known riders with their phone numbers — MotionTools' number if it sent one, otherwise the one typed in Settings."""
    rows = {r["id"]: r for r in store.riders()}
    for rid, r in STATE["riders"].items():
        rows.setdefault(rid, r)
    out = []
    for rid, r in rows.items():
        live = STATE["riders"].get(rid, {})
        mt_phone = live.get("mt_phone") or ""
        out.append({"id": rid, "name": live.get("name") or r.get("name") or "Rider", "mt_phone": mt_phone,
                    "phone": projector.phones.get(rid, ""), "online": bool(live.get("online", r.get("online")))})
    out.sort(key=lambda x: (not x["online"], x["name"]))
    return {"riders": out}


@app.post("/api/riders", dependencies=[Depends(require_login)])
async def api_riders_set(request: Request):
    body = await request.json()
    for rid, phone in (body or {}).items():
        if rid and isinstance(phone, str):
            projector.set_phone(rid, phone.strip())
            r = STATE["riders"].get(rid)
            if r is not None:
                store.upsert_rider(rid, r["name"] or "Rider", r["phone"], r["online"], r.get("lat"), r.get("lng"),
                                   r.get("active_ids") or [], datetime.now(UTC))
    evaluate_all(datetime.now(UTC))                     # open alerts pick up the new numbers immediately
    return {"ok": True}


@app.post("/api/probe", dependencies=[Depends(require_login)])
async def api_probe():
    """Button in Settings: re-check which MotionTools endpoints this token may read, right now."""
    now = datetime.now(UTC)
    await probe_endpoints(now)
    if STATE["sync"]["mode"] == "webhook":
        STATE["sync"]["api_retry_at"] = iso(now)         # let the next sync run try the list endpoints immediately
        WAKE.set()
    return {"ok": True, "endpoints": mt.stats["endpoints"], "summary": mt.endpoint_summary()}


@app.get("/api/insights", dependencies=[Depends(require_login)])
def api_insights(period: str = "today"):
    _check_period(period)
    return store.insights(period, datetime.now(UTC), rules)


def _check_period(period):
    if period not in ("today", "yesterday", "week", "month") and not (len(period) == 10 and period[4] == "-"):
        raise HTTPException(400, "period must be today, yesterday, week, month or YYYY-MM-DD")


@app.get("/api/orders", dependencies=[Depends(require_login)])
def api_orders(period: str = "today", q: str = ""):
    _check_period(period)
    now = datetime.now(UTC)
    rows = store.orders_in(period, now, q)
    return {"orders": [order_view(o, now) for o in rows]}


@app.get("/api/orders/{oid}", dependencies=[Depends(require_login)])
def api_order(oid: str):
    o = store.order(oid)
    if not o:
        raise HTTPException(404)
    now = datetime.now(UTC)
    pts = store.positions_for_order(o)
    step = max(1, len(pts) // 9)
    way = [f"{p['lat']:.5f},{p['lng']:.5f}" for p in pts[::step]][:10]
    view = order_view(o, now)
    view.update({"alerts_log": store.alerts_for_order(oid), "positions": len(pts), "trail_km": store.trail_km(o),
                 "route_url": "https://www.google.com/maps/dir/" + "/".join(way) if len(way) >= 2 else "",
                 "times": {k: iso(o.get(k + "_at")) for k in ("dispatched", "accepted", "started", "at_restaurant", "picked_up", "at_customer", "delivered", "cancelled")}})
    return view


@app.get("/api/riders/{rid}", dependencies=[Depends(require_login)])
def api_rider(rid: str, period: str = "today"):
    _check_period(period)
    now = datetime.now(UTC)
    ins = store.insights(period, now, rules)
    stats = next((r for r in ins["riders"] if r["rider_id"] == rid), None)
    orders = [order_view(o, now) for o in store.orders_in(period, now) if o["rider_id"] == rid]
    r = STATE["riders"].get(rid, {})
    return {"rider": stats or {"rider_id": rid, "rider": r.get("name", "")}, "phone": r.get("phone", ""),
            "online": r.get("online"), "orders": orders, "period_phases": ins["phases"]}


@app.get("/api/daily", dependencies=[Depends(require_login)])
def api_daily(day: str = ""):
    now = datetime.now(UTC)
    day = day or day_key(now - timedelta(days=1))
    data = store.daily(day)
    frozen = data is not None
    if data is None:
        data = store.insights(day, now, rules)
        data["day"] = day
    data["frozen"] = frozen
    data["trend"] = store.daily_trend(14)
    data["brief"] = daily_brief(data)
    return data


def daily_brief(d: dict) -> str:
    """Short text for the team chat."""
    lines = [f"Quickzi {CITY} — {d.get('day', '')}",
             f"Orders: {d['delivered']} delivered, {d['cancelled']} cancelled",
             f"Within {rules.ptod_target_min} min: {d['within_pct'] if d['within_pct'] is not None else '–'}% (target {rules.target_within_pct}%) · avg PTOD {d['avg_ptod'] or '–'} min · late: {d['late']}"]
    ph = d["phases"]
    if ph.get("to_accept") is not None:
        lines.append(f"Avg minutes: accept {ph['to_accept']} · to restaurant {ph['to_restaurant']} · at restaurant {ph['at_restaurant']} · to customer {ph['to_customer']} · handover {ph['handover']}")
    if d["riders"]:
        best = d["riders"][0]
        lines.append(f"Best rider: {best['rider']} ({best['within_pct']}% within target, {best['delivered']} orders)")
    for f in d["focus"][:3]:
        lines.append(f"• {f['title']}")
    return "\n".join(lines)


@app.get("/api/settings", dependencies=[Depends(require_login)])
def api_settings_get():
    return {"rules": rules.as_dict(), "labels": {
        "ptod_target_min": "PTOD target (minutes from dispatch to delivered)", "ptod_warn_min": "PTOD warning at (minutes)",
        "target_within_pct": "Goal: % of orders within target", "accept_limit_min": "Alert if nobody accepted after (min)",
        "start_limit_min": "Alert if accepted but not started after (min)", "stationary_min": "Alert if not moving for (min)",
        "wrong_way_m": "Alert if further from next stop by (metres)", "late_grace_min": "Alert if behind ETA by (min)",
        "wait_restaurant_min": "Alert if waiting at restaurant (min)", "wait_customer_min": "Alert if waiting at customer (min)"}}


@app.post("/api/settings", dependencies=[Depends(require_login)])
async def api_settings_set(request: Request):
    values = await request.json()
    rules.apply(values)
    store.set_settings({k: v for k, v in values.items() if k in Rules.EDITABLE})
    store.log("info", "thresholds changed: " + ", ".join(f"{k}={v}" for k, v in values.items() if k in Rules.EDITABLE))
    return {"ok": True, "rules": rules.as_dict()}


@app.get("/api/system", dependencies=[Depends(require_login)])
def api_system():
    s = STATE["sync"]
    return {"started": iso(STARTED), "uptime_min": int((datetime.now(UTC) - STARTED).total_seconds() // 60), "sync": s,
            "mode": s["mode"], "event_counts": projector.counts, "endpoint_summary": mt.endpoint_summary(),
            "api": mt.stats, "areas": AREAS, "sync_seconds": SYNC_SECONDS, "log": store.syslog(40),
            "db_orders": len(store.orders_in("month", datetime.now(UTC))), "samples": STATE["raw_samples"]}


@app.post("/api/alerts/{aid}/dismiss", dependencies=[Depends(require_login)])
def api_dismiss(aid: int):
    return {"ok": store.dismiss_alert(aid, datetime.now(UTC))}


@app.post("/api/alerts/{aid}/snooze", dependencies=[Depends(require_login)])
def api_snooze(aid: int, minutes: int = 10):
    return {"ok": store.snooze_alert(aid, datetime.now(UTC) + timedelta(minutes=max(1, min(minutes, 120))))}


@app.get("/export.csv", dependencies=[Depends(require_login)])
def export_csv(period: str = "today"):
    _check_period(period)
    body = store.export_csv(period, datetime.now(UTC))
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="quickzi-{CITY.lower()}-{period}.csv"'})


@app.get("/health")
def health():
    s = STATE["sync"]
    return {"ok": True, "city": CITY, "live_orders": len(STATE["orders"]), "riders_known": len(STATE["riders"]),
            "open_alerts": len(STATE["open_alerts"]), "uptime_min": int((datetime.now(UTC) - STARTED).total_seconds() // 60),
            "setup": {"dashboard_password_set": bool(DASH_PASSWORD), "motiontools_token_set": mt.enabled,
                      "webhook_secret_set": PATH_SECRET != "change-me", "data_dir": str(DATA_DIR), "areas": AREAS},
            "sync": {k: s[k] for k in ("last_ok", "last_error", "runs", "orders_seen", "riders_seen", "backfilled", "webhook_events", "last_snapshot")},
            "api": mt.stats}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
