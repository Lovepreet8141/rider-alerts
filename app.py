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
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from events import DISPATCHED, Projector
from mt import ACTIVE_STATUSES, PLACE_PATH, USER_PATH, MotionTools
from orders import BERLIN, UTC, RiderTracker, Rules, evaluate, hhmm, iso, mins, on_time, parse_booking, phase_from, phase_minutes, planned_at, ts
import store as store_mod
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
GPS_EVENTS = ("driver_location_updated",)        # 90 % of all events; processed live, never written to disk
EVENT_FILE_DAYS = 3                               # daily raw-event files kept on the volume


def event_file(now: datetime) -> Path:
    return DATA_DIR / f"events-{now.astimezone(BERLIN).strftime('%Y-%m-%d')}.jsonl"


def event_files() -> list:
    """Today's and the previous days' event files, oldest first (plus the legacy single file if still present)."""
    files = sorted(DATA_DIR.glob("events-*.jsonl"))
    legacy = DATA_DIR / "events.jsonl"
    return ([legacy] if legacy.exists() else []) + files


def tail_lines(path: Path, n: int, max_bytes: int = 80 * 1024 * 1024) -> list:
    """Last n lines of a file without loading all of it (the legacy file can be hundreds of MB)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
        lines = data.decode("utf-8", "ignore").splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]                     # the first line is probably cut in half
        return lines[-n:]
    except Exception:
        return []


def recent_event_lines(n: int = 60000) -> list:
    out = []
    for p in reversed(event_files()):
        out = tail_lines(p, n - len(out)) + out
        if len(out) >= n:
            break
    return out[-n:]


def disk_report() -> dict:
    try:
        u = shutil.disk_usage(str(DATA_DIR))
        total, used, free = u.total, u.used, u.free
    except Exception:
        total = used = free = 0
    files = {p.name: p.stat().st_size for p in DATA_DIR.glob("*") if p.is_file()}
    return {"total_mb": round(total / 1e6), "used_mb": round(used / 1e6), "free_mb": round(free / 1e6),
            "pct": round(100 * used / total) if total else None,
            "db_mb": round(store.db_size_bytes() / 1e6, 1),
            "files_mb": {k: round(v / 1e6, 1) for k, v in sorted(files.items(), key=lambda x: -x[1])[:8]}}


def housekeeping(now: datetime, startup: bool = False):
    """Keep the volume small: drop old daily event files, shrink the legacy event file (GPS lines out, last 60k
    kept), purge old GPS points, and give the space back with VACUUM when the disk has room for it."""
    freed = 0
    keep = {event_file(now - timedelta(days=i)).name for i in range(EVENT_FILE_DAYS)}
    for p in DATA_DIR.glob("events-*.jsonl"):
        age_days = (now.timestamp() - p.stat().st_mtime) / 86400
        if p.name not in keep and age_days > EVENT_FILE_DAYS:
            freed += p.stat().st_size
            p.unlink(missing_ok=True)
    legacy = DATA_DIR / "events.jsonl"
    if legacy.exists():
        size = legacy.stat().st_size
        kept = []
        try:
            from collections import deque
            dq = deque(maxlen=60000)
            with open(legacy, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"driver_location_updated"' in line:
                        continue
                    dq.append(line.rstrip("\n"))
            kept = list(dq)
            tmp = DATA_DIR / "events-legacy.jsonl.tmp"
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            legacy.unlink()
            tmp.rename(DATA_DIR / "events-0000-legacy.jsonl")
            freed += size - sum(len(x) + 1 for x in kept)
        except Exception as e:
            log.warning("could not shrink the legacy event file: %s", e)
    store.cleanup(now)
    rep = disk_report()
    if rep["free_mb"] > rep["db_mb"] * 1.3 + 20 and (startup or rep["pct"] and rep["pct"] >= 70):
        if store.vacuum():
            rep = disk_report()
    store.log("info", f"housekeeping: freed {freed / 1e6:.0f} MB of event files · DB {rep['db_mb']} MB · volume {rep['used_mb']}/{rep['total_mb']} MB ({rep['pct']}%)")
    return rep
SYNC_SECONDS = int(env("SYNC_SECONDS", "30") or 30)
CITY = env("CITY_NAME", "Munich") or "Munich"
VERSION = "5.2"
STARTED = datetime.now(UTC)

mt = MotionTools(MT_TOKEN)
store = Store(str(DATA_DIR / "quickzi.db"))
rules = Rules()
rules.apply(store.get_settings())
store_mod.set_day_start(rules.day_start_hour)
tracker = RiderTracker()
app = FastAPI()
basic = HTTPBasic()

STATE = {"orders": {}, "riders": {}, "open_alerts": {}, "sev": {}, "heads": {}, "hidden": {}, "stack": {}, "raw_samples": {},
         "sync": {"mode": "api", "last_ok": None, "last_error": None, "orders_seen": 0, "riders_seen": 0, "runs": 0,
                  "webhook_events": 0, "last_webhook": None, "backfilled": 0, "last_snapshot": None,
                  "api_retry_at": None, "events": {}, "enriched": 0, "probe_at": None,
                  "silent_min": 0, "webhook_silent": False, "silent_logged": False}}
WAKE = asyncio.Event()
projector = Projector(STATE, store, tracker, AREAS)
projector.lead_min = rules.release_lead_min
ENRICHED_RIDERS: dict = {}          # rider_id -> when we last read it through the API
DISK_CACHE: dict = {}
ENRICH_LOCK = asyncio.Lock()


def api_restricted() -> bool:
    return "restricted_endpoint" in (mt.stats.get("last_error") or "")

PHASE_LABEL = {"on_hold": "On hold (not dispatched yet)", "unassigned": "Waiting for rider", "accepted": "Accepted, not started",
               "to_restaurant": "Riding to restaurant", "at_restaurant": "At restaurant", "to_customer": "Delivering",
               "at_customer": "At customer", "delivered": "Delivered", "cancelled": "Cancelled", "closed": "Closed (no events)"}


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
        prev = STATE["orders"].get(o["id"]) or {}
        o["promised_at"] = prev.get("promised_at") or o.get("promised_at") or o.get("eta_customer")   # first ETA = the plan
        STATE["orders"][o["id"]] = o
        store.upsert_order(o, now, stacked=per_rider.get(o["rider_id"], 0) >= 2)
        if o["rider_id"] and o["rider_lat"] is not None:
            tracker.push(o["rider_id"], o["rider_lat"], o["rider_lng"], now)
            store.record_position(o["rider_id"], o["rider_lat"], o["rider_lng"], o["id"], now)
    for oid in [k for k in STATE["orders"] if k not in seen]:
        b = await mt.get_booking(oid)
        o = parse_booking(b) if b else None
        if o and o["id"]:
            o["promised_at"] = STATE["orders"][oid].get("promised_at") or o.get("promised_at")   # keep the plan it had
            if o["phase"] not in ("delivered", "cancelled"):
                o["phase"] = "delivered" if o["delivered_at"] else "cancelled"
            store.upsert_order(o, now, stacked=False)
            why = "delivered" if o["phase"] == "delivered" else "cancelled"
        else:
            why = "order closed"
        for key in [k for k in STATE["open_alerts"] if k[0] == oid]:
            store.resolve_alert(STATE["open_alerts"].pop(key), why, now)
            STATE["sev"].pop(key, None)
            STATE["heads"].pop(key, None)
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


async def enrich_rider(rid: str, now: datetime = None, force: bool = False) -> bool:
    """Webhook mode: name + phone of a rider through /api/users/{id} (once a day per rider, if open)."""
    if not rid or not mt.enabled or mt.blocked(USER_PATH):
        return False
    now = now or datetime.now(UTC)
    last = ENRICHED_RIDERS.get(rid)
    if last and now - last < timedelta(hours=24) and not force:
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
    if u.get("status") in ("online", "offline", "busy", "available"):
        r["online"] = u["status"] != "offline"
        r["api_status_at"] = iso(now)
        if r["online"]:
            r["last_seen"] = iso(now)
    loc = u.get("location") or {}
    if loc.get("lat") is not None:
        r["lat"], r["lng"] = loc.get("lat"), loc.get("lng")
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
            # the hourly quota goes to dispatched orders only — a pre-order on hold for 3 hours can wait
            if (d.get("status") or "") in DISPATCHED and await enrich_order(d.get("booking_id")):
                evaluate_all(datetime.now(UTC))
        elif rtype == "booking" and ev == "transition" and str(d.get("to") or "") in DISPATCHED:
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


SWEEP_POS = {"i": 0}


async def sweep_rider_status(now: datetime):
    """Every 2 min, 2 riders: round-robin through every rider known today and ask MotionTools whether they are
    online — the only way to count riders who are online but had no order and no online event. ~60 calls/hour;
    if the hourly quota ends, the client stops by itself until the next hour."""
    if mt.blocked(USER_PATH):
        return
    cutoff = iso(now - timedelta(minutes=30))
    rids = sorted(rid for rid, r in STATE["riders"].items() if (r.get("last_seen") or "") < cutoff and (r.get("api_status_at") or "") < cutoff)
    if not rids:
        return
    changed = False
    for _ in range(2):
        rid = rids[SWEEP_POS["i"] % len(rids)]
        SWEEP_POS["i"] += 1
        if await enrich_rider(rid, now, force=True):
            changed = True
    if changed:
        evaluate_all(datetime.now(UTC))


async def recheck_offline_riders(now: datetime):
    """Every 10 min: riders we show as OFFLINE while they hold an order — ask MotionTools (rider detail is open)
    whether that is still true, at most 6 per run. A wrong 'offline' would otherwise raise a red alert for nothing."""
    if mt.blocked(USER_PATH):
        return
    holding = {o["rider_id"] for o in STATE["orders"].values() if o["rider_id"] and o["phase"] not in ("on_hold",)}
    todo = [rid for rid in holding if STATE["riders"].get(rid, {}).get("online") is False][:6]
    changed = False
    for rid in todo:
        if await enrich_rider(rid, now, force=True):
            changed = True
    if changed:
        evaluate_all(datetime.now(UTC))


async def refresh_live_orders(now: datetime):
    """Webhook mode, every 5 min: re-read a few live orders through the detail endpoint if it is open.
    MotionTools gives restricted accounts a small HOURLY quota per endpoint, so the budget goes to the
    orders that matter: those with open alerts, oldest first, at most 5 per run."""
    tpl = mt.stats.get("detail_path")
    if not tpl or mt.blocked(tpl):
        return
    with_alerts = {k[0] for k in STATE["open_alerts"]}
    todo = sorted((o for o in STATE["orders"].values() if o["id"] in with_alerts and o["phase"] != "on_hold"),
                  key=lambda o: o.get("dispatched_at") or now)[:5]
    changed = False
    for o in todo:
        if mt.blocked(tpl):
            break
        if await enrich_order(o["id"], now):
            changed = True
    if changed:
        evaluate_all(datetime.now(UTC))


class _NullStore:
    """Store stand-in for replays: the projector's side effects go nowhere."""
    def get_settings(self): return {}
    def set_settings(self, v): pass
    def order(self, bid): return None
    def upsert_order(self, *a, **k): pass
    def record_position(self, *a, **k): pass
    def upsert_rider(self, *a, **k): pass
    def resolve_alert(self, *a, **k): pass
    def log(self, *a, **k): pass


def replay_events(max_lines: int = 60000) -> dict:
    """Rebuild every order of the stored raw events from scratch (same rules as live), finished ones included."""
    lines = recent_event_lines(max_lines)
    if not lines:
        return {}
    state = {"orders": {}, "riders": {}, "open_alerts": {}, "sev": {}, "heads": {}}
    pj = Projector(state, _NullStore(), RiderTracker(), AREAS, keep_finished=True)
    pj.places = dict(projector.places)
    pj.lead_min = rules.release_lead_min
    for line in lines:
        try:
            p = json.loads(line)
        except Exception:
            continue
        try:
            pj.apply(p)
        except Exception as e:            # one bad event must not stop the replay
            log.warning("replay skipped an event: %s", e)
    pj.release_due(datetime.now(UTC))
    return state["orders"]


COPY_TIMES = ("created_at", "dispatched_at", "accepted_at", "started_at", "at_restaurant_at", "picked_up_at",
              "at_customer_at", "delivered_at", "scheduled_at", "last_event_at", "eta_at", "promised_at")


def repair_from_events(now: datetime) -> int:
    """At startup: replay the stored events and give every order of the last 7 days the full, correct story —
    real dispatch moment (on hold until pickable), every rider who had it (accepted / handed back / arrived / ...),
    and the timestamps of the rider who actually delivered.  Enrichment (names, addresses, phones) is kept."""
    replayed = replay_events()
    if not replayed:
        return 0
    targets = {o["id"]: o for o in store.orders_in("week", now)}
    targets.update(STATE["orders"])
    n = 0
    for bid, o in targets.items():
        r = replayed.get(bid)
        if r is None:
            continue
        finished = o["phase"] in ("delivered", "cancelled", "closed")
        if r.get("partial") and not r.get("history"):
            continue                                        # we saw almost nothing of this order — leave it
        for k in COPY_TIMES:
            if r.get(k) is not None or (not r.get("partial") and not finished and k in ("dispatched_at", "accepted_at", "started_at")):
                o[k] = r.get(k)                             # a finished order never loses a timestamp to a gap in the event log
        for k in ("history", "reassigned", "tour_id", "stop_types", "eta_restaurant", "eta_customer", "status"):
            if r.get(k) not in (None, [], {}):
                o[k] = r[k]
        if r.get("rider_id") or (r.get("reassigned") and not finished):
            o["rider_id"], o["rider"] = r.get("rider_id"), r.get("rider") or ""
        if r.get("place_id") and not o.get("place_id"):
            o["place_id"] = r["place_id"]
            o["restaurant"] = projector.restaurant_name(r["place_id"])
        if r.get("cancelled"):
            o["cancelled"] = True
            o["cancel_reason"] = o.get("cancel_reason") or r.get("cancel_reason") or ""
        if not finished or r["phase"] in ("delivered", "cancelled"):
            o["phase"] = phase_from(r) if not finished else r["phase"]
        store.upsert_order(o, now, stacked=bool(o.get("stacked")), force=True)
        n += 1
    return n


def alert_payload(o: dict, c: dict) -> dict:
    r = STATE["riders"].get(o["rider_id"] or "", {})
    lat, lng = (r.get("lat"), r.get("lng")) if r.get("lat") is not None else (o["rider_lat"], o["rider_lng"])
    return {"order_id": o["id"], "order_ref": o["ref"], "rider_id": o["rider_id"], "rider": o["rider"] or r.get("name", ""),
            "kind": c["kind"], "severity": c["severity"], "headline": c["headline"], "action": c["action"],
            "restaurant": o["restaurant"], "phone": r.get("phone") or "", "restaurant_phone": o["restaurant_phone"],
            "map_url": f"https://maps.google.com/?q={lat:.5f},{lng:.5f}" if lat is not None else ""}


PROGRESS = {"accepted": 0, "to_restaurant": 1, "at_restaurant": 2, "to_customer": 3, "at_customer": 4}


def compute_stacks(now: datetime):
    """Double orders: the rider works one order at a time. The order whose next stop has the earliest ETA is the
    'current' one (fallback: the one further along, then the earlier dispatched); the others are queued behind it."""
    by_rider = {}
    for o in STATE["orders"].values():
        if o["rider_id"] and o["phase"] in PROGRESS:
            by_rider.setdefault(o["rider_id"], []).append(o)
    stack = {}
    for rid, rs in by_rider.items():
        if len(rs) < 2:
            continue

        def rank(o):
            eta = o.get("eta_customer") if o.get("picked_up_at") else o.get("eta_restaurant")
            return (0 if eta else 1, eta or now, -PROGRESS.get(o["phase"], 0), o.get("dispatched_at") or now)
        active = sorted(rs, key=rank)[0]
        for o in rs:
            stack[o["id"]] = {"with": [x["ref"] for x in rs if x is not o], "queued": o is not active,
                              "behind": active["ref"] if o is not active else ""}
    STATE["stack"] = stack


def evaluate_all(now: datetime):
    compute_stacks(now)
    for key in [k for k in STATE["open_alerts"] if k[0] not in STATE["orders"]]:
        store.resolve_alert(STATE["open_alerts"].pop(key), "order completed", now)
        STATE["sev"].pop(key, None)
        STATE["heads"].pop(key, None)
    for o in STATE["orders"].values():
        r = STATE["riders"].get(o["rider_id"] or "")
        queued = STATE["stack"].get(o["id"], {}).get("queued", False)
        conds = {c["kind"]: c for c in evaluate(o, now, rules, tracker, r["online"] if r else None, queued=queued)}
        for kind, c in conds.items():
            key = (o["id"], kind)
            payload = alert_payload(o, c)
            STATE["sev"][key] = c["severity"]
            STATE["heads"][key] = c["headline"]
            if key in STATE["open_alerts"]:
                store.update_alert(STATE["open_alerts"][key], payload, now)
            else:
                STATE["open_alerts"][key] = store.open_alert(payload, now)
        for key in [k for k in STATE["open_alerts"] if k[0] == o["id"] and k[1] not in conds]:
            store.resolve_alert(STATE["open_alerts"].pop(key), PHASE_LABEL.get(o["phase"], o["phase"]).lower(), now)
            STATE["sev"].pop(key, None)
            STATE["heads"].pop(key, None)


def webhook_watch(now: datetime):
    """Webhook mode: MotionTools pauses a webhook when our server answered with errors for a while (it happened when the
    volume was full). Nothing then arrives — the board would silently freeze. So: no event for 15 min while there are
    live orders (or riders online during opening hours) = a warning banner + a log line."""
    s = STATE["sync"]
    last = ts(s["last_webhook"]) if s["last_webhook"] else None
    ref = max(STARTED, last) if last else STARTED
    s["silent_min"] = int((now - ref).total_seconds() // 60)
    live = sum(1 for o in STATE["orders"].values() if o["phase"] not in ("on_hold", "delivered", "cancelled", "closed"))
    online = sum(1 for r in STATE["riders"].values() if r.get("online"))
    hour = now.astimezone(BERLIN).hour
    matters = live > 0 or (online > 0 and 11 <= hour < 23)
    s["webhook_silent"] = s["mode"] == "webhook" and s["silent_min"] >= 15 and matters
    if s["webhook_silent"] and not s["silent_logged"]:
        s["silent_logged"] = True
        store.log("error", f"no MotionTools events for {s['silent_min']} min while {live} orders are live / {online} riders online — "
                           "is the webhook still active in MotionTools (Settings → Webhooks)?")
    elif not s["webhook_silent"] and s["silent_logged"] and s["silent_min"] < 15:
        s["silent_logged"] = False
        store.log("info", "MotionTools events are arriving again")


def snapshot_yesterday(now: datetime):
    """Freeze yesterday's report once per day (5 min after the operating day rolls over)."""
    local = now.astimezone(BERLIN)
    yday = day_key(now - timedelta(days=1))
    if STATE["sync"]["last_snapshot"] == yday or (local.hour == store_mod.DAY_STARTS_AT and local.minute < 5):
        return
    if store.daily(yday) is None or STATE["sync"]["last_snapshot"] is None:
        data = store.insights(yday, now, rules, sessions_ok=STATE['sync']['mode'] == 'api')
        data["day"] = yday
        store.save_daily(yday, data)
        store.log("info", f"daily report frozen for {yday}: {data['delivered']} delivered, {data['within_pct']}% within target")
    STATE["sync"]["last_snapshot"] = yday


async def sync_loop():
    first = True
    last_backfill = None
    last_refresh = None
    last_sweep = None
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
                if last_sweep is None or (now - last_sweep) >= timedelta(minutes=2):
                    await sweep_rider_status(now)
                    last_sweep = now
                if last_refresh is None or (now - last_refresh) >= timedelta(minutes=5):
                    await refresh_live_orders(now)
                    if int(now.timestamp() // 300) % 2 == 0:
                        await recheck_offline_riders(now)
                    last_refresh = now
                    n = projector.expire(now)
                    if n:
                        store.log("info", f"{n} order(s) closed automatically — no MotionTools events for hours")
                if projector.release_due(now):
                    evaluate_all(now)
                webhook_watch(now)
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
                await asyncio.get_event_loop().run_in_executor(None, housekeeping, now, False)
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
        STATE["heads"][(a["order_id"], a["kind"])] = a["headline"]
        if a.get("dismissed_at"):
            STATE["hidden"][a["id"]] = True
        elif a.get("snoozed_until") and ts(a["snoozed_until"]) and ts(a["snoozed_until"]) > now:
            STATE["hidden"][a["id"]] = ts(a["snoozed_until"])
    for r in store.riders():
        STATE["riders"][r["id"]] = {"id": r["id"], "name": r["name"], "phone": projector.phone_for(r["id"], r["phone"]),
                                    "mt_phone": r["phone"] if r["phone"] != projector.phones.get(r["id"]) else "",
                                    "online": bool(r["online"]), "lat": r["lat"], "lng": r["lng"], "active_ids": r["active_ids"],
                                    "persisted": True}
    for o in STATE["orders"].values():                 # a rider holding a live order is on the road, whatever the DB says
        if o.get("rider_id"):
            r = projector.rider(o["rider_id"], o.get("rider") or "", now)
            if not r.get("online") and o["phase"] not in ("on_hold",):
                r["online"] = True
    store.log("info", f"server started — {len(STATE['orders'])} open orders, {len(STATE['open_alerts'])} open alerts restored")
    try:
        await asyncio.get_event_loop().run_in_executor(None, housekeeping, now, True)
    except Exception as e:
        log.exception("housekeeping failed: %s", e)
    try:
        backup = DATA_DIR / f"quickzi-backup-{now.strftime('%Y%m%d-%H%M')}.db"
        store.backup_to(str(backup))
        for old_b in sorted(DATA_DIR.glob("quickzi-backup-*.db"))[:-3]:
            old_b.unlink(missing_ok=True)
    except Exception as e:
        log.warning("backup failed: %s", e)
    try:
        n = repair_from_events(now)
        if n:
            on_hold = sum(1 for o in STATE["orders"].values() if o["phase"] == "on_hold")
            for o in [x for x in STATE["orders"].values() if x["phase"] in ("delivered", "cancelled")]:
                projector.finish(o, now)              # finished while we were down -> out of the live board
            store.log("info", f"rebuilt {n} orders of the last 7 days from the stored events ({on_hold} live orders on hold)")
    except Exception as e:
        log.exception("repair failed: %s", e)
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
    STATE["sync"]["silent_min"], STATE["sync"]["webhook_silent"] = 0, False
    if str(p.get("event") or "") not in GPS_EVENTS:
        with open(event_file(datetime.now(UTC)), "a") as f:
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


PHASE_START = {"unassigned": "dispatched_at", "accepted": "accepted_at", "to_restaurant": "started_at", "at_restaurant": "at_restaurant_at",
               "to_customer": "picked_up_at", "at_customer": "at_customer_at", "on_hold": "created_at"}


def order_view(o: dict, now: datetime) -> dict:
    r = STATE["riders"].get(o["rider_id"] or "", {})
    live = o["phase"] not in ("delivered", "cancelled", "closed")
    end = o["delivered_at"] or o.get("cancelled_at") or now
    elapsed = mins(o["dispatched_at"], end if not live else now) if o.get("dispatched_at") and o["phase"] != "closed" else None
    stage_since = o.get(PHASE_START.get(o["phase"], "")) or (o.get("accepted_at") if o["phase"] == "to_restaurant" else None) or o.get("dispatched_at")
    in_stage = mins(stage_since, now) if (live and stage_since) else None
    def shown(key):                      # "Handled" / snoozed alerts disappear from the card
        until = STATE["hidden"].get(STATE["open_alerts"][key])
        return until is None or (until is not True and until < now)
    kinds = [k[1] for k in STATE["open_alerts"] if k[0] == o["id"] and shown(k)] if live else []
    sevs = [STATE["sev"].get((o["id"], k)) for k in kinds]
    heads = [STATE["heads"].get((o["id"], k), "") for k in kinds]
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
            "rider_online": r.get("online"), "live": live, "in_stage": int(in_stage) if in_stage is not None else None,
            "alert_heads": [h for h in heads if h], "stack": STATE["stack"].get(o["id"]) if live else None,
            "reason": o.get("reason") or "", "note": o.get("note") or "", "warn": rules.ptod_warn_min,
            "history": o.get("history") or [], "reassigned": o.get("reassigned") or 0,
            "created": hm(o.get("created_at")), "scheduled": hm(o.get("scheduled_at")),
            "planned": hm(o.get("eta_customer") or o.get("scheduled_at")),
            "plan": hm(planned_at(o)), "plan_grace": rules.plan_grace_min, "on_time": on_time(o, rules.plan_grace_min),
            "release": hm((o.get("eta_customer") or o.get("scheduled_at")) - timedelta(minutes=rules.release_lead_min)) if (o.get("eta_customer") or o.get("scheduled_at")) and o["phase"] == "on_hold" else None,
            "waiting_min": int(mins(o.get("created_at"), now) or 0) if o["phase"] == "on_hold" else None}


@app.get("/api/state", dependencies=[Depends(require_login)])
def api_state():
    now = datetime.now(UTC)
    alerts = store.alerts_for_ui(now)
    orders = sorted((order_view(o, now) for o in STATE["orders"].values()),
                    key=lambda v: (v["phase"] == "on_hold", -(v["elapsed"] or 0)))
    busy = {}
    for o in STATE["orders"].values():
        if o["rider_id"]:
            busy[o["rider_id"]] = busy.get(o["rider_id"], 0) + 1
    riders = []
    for r in STATE["riders"].values():
        n = busy.get(r["id"], 0) or (len(r.get("active_ids") or []) if STATE["sync"]["mode"] == "api" else 0)
        fix = tracker.last_fix(r["id"])
        riders.append({"id": r["id"], "name": r["name"], "phone": r["phone"], "online": r["online"], "orders": n,
                       "status": "offline" if not r["online"] else ("busy" if n else "idle"),
                       "map_url": f"https://maps.google.com/?q={r['lat']:.5f},{r['lng']:.5f}" if r.get("lat") is not None else "",
                       "still_min": tracker.stationary_minutes(r["id"], now, rules.stationary_radius_m) if r["online"] else None,
                       "last_fix_min": int((now - fix[0]).total_seconds() // 60) if fix else None})
    riders.sort(key=lambda x: ({"busy": 0, "idle": 1, "offline": 2}[x["status"]], -x["orders"], x["name"]))
    today = store.delivered("today", now)
    ptods = [o["phases"]["ptod"] for o in today if o["phases"]["ptod"] is not None]
    plan = [o["phases"]["vs_plan"] for o in today if o["phases"]["vs_plan"] is not None]
    open_alerts = [a for a in alerts if a["resolved_at"] is None]
    last_ok = STATE["sync"]["last_ok"]
    stale = (not last_ok) or (now - datetime.fromisoformat(last_ok)).total_seconds() > max(180, SYNC_SECONDS * 4)
    pulse = {"delivered": len(today),
             "within_pct": round(100 * sum(1 for p in ptods if p <= rules.ptod_target_min) / len(ptods)) if ptods else None,
             "target_within_pct": rules.target_within_pct, "target": rules.ptod_target_min,
             "avg_ptod": round(mean(ptods)) if ptods else None,
             "on_time_pct": round(100 * sum(1 for v in plan if v <= rules.plan_grace_min) / len(plan)) if plan else None,
             "plan_known": len(plan), "late_vs_plan": sum(1 for v in plan if v > rules.plan_grace_min), "plan_grace": rules.plan_grace_min,
             "live_orders": sum(1 for o in orders if o["phase"] != "on_hold"), "on_hold": sum(1 for o in orders if o["phase"] == "on_hold"),
             "unassigned": sum(1 for o in orders if o["phase"] == "unassigned"),
             "riders_online": sum(1 for r in riders if r["online"]), "riders_idle": sum(1 for r in riders if r["status"] == "idle"),
             "riders_busy": sum(1 for r in riders if r["status"] == "busy"),
             "red": sum(1 for a in open_alerts if a["severity"] == "red"), "amber": sum(1 for a in open_alerts if a["severity"] == "amber")}
    disk = DISK_CACHE.get("rep") if (now - DISK_CACHE.get("at", now - timedelta(hours=1))) < timedelta(minutes=2) else None
    if disk is None:
        disk = DISK_CACHE["rep"] = disk_report()
        DISK_CACHE["at"] = now
    return {"now": iso(now), "city": CITY, "pulse": pulse, "alerts": alerts, "orders": orders, "riders": riders, "reasons": REASONS,
            "disk": {"pct": disk["pct"], "free_mb": disk["free_mb"], "total_mb": disk["total_mb"]},
            "sync": {**STATE["sync"], "stale": stale and mt.enabled, "api": mt.stats, "areas": AREAS, "started": iso(STARTED)}}


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
    try:
        return store.insights(period, datetime.now(UTC), rules, sessions_ok=STATE['sync']['mode'] == 'api')
    except Exception as e:
        log.exception("insights failed")
        store.log("error", f"insights failed: {e}"[:300])
        raise HTTPException(500, f"insights failed: {e}")


@app.get("/api/staffing", dependencies=[Depends(require_login)])
def api_staffing():
    """Tomorrow's riders per hour, from the last 7 operating days."""
    return store.staffing_plan(datetime.now(UTC), rules)


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


REASONS = ["Restaurant late", "No rider available", "Rider slow / detour", "Double order", "Wrong address / customer unreachable",
           "Pre-order released late", "App / GPS problem", "Traffic / weather", "Other"]


@app.post("/api/orders/{oid}/reason", dependencies=[Depends(require_login)])
async def api_order_reason(oid: str, request: Request):
    body = await request.json()
    reason, note = str(body.get("reason") or "").strip()[:60], str(body.get("note") or "").strip()[:300]
    ok = store.set_reason(oid, reason, note, datetime.now(UTC))
    o = STATE["orders"].get(oid)
    if o is not None:
        o["reason"], o["note"] = reason, note
    return {"ok": ok}


@app.get("/api/orders/{oid}/events", dependencies=[Depends(require_login)])
def api_order_events(oid: str):
    """Every raw MotionTools event that touched this order (booking events + its tour's events) — the ground truth."""
    tours = {tid for tid, ids in projector.tours.items() if oid in ids}
    out = []
    for line in recent_event_lines(60000):
        if oid not in line and not any(t in line for t in tours):
            continue
        try:
            p = json.loads(line)
        except Exception:
            continue
        d = p.get("data") or {}
        ids = d.get("dispatched_booking_ids") or []
        hit = d.get("booking_id") == oid or (isinstance(ids, list) and oid in ids) or (d.get("tour_id") in tours)
        if not hit:
            continue
        if d.get("tour_id") and p.get("resource_type") == "tour":
            tours.add(d["tour_id"])
        users = d.get("affected_user_ids") or []
        out.append({"at": p.get("timestamp"), "name": f"{p.get('resource_type')}.{p.get('event')}",
                    "detail": " ".join(x for x in [
                        f"{d['from']} → {d['to']}" if d.get("to") else "",
                        f"({d['event']})" if d.get("event") else "",
                        f"status={d['status']}" if d.get("status") and not d.get("to") else "",
                        f"stop={d['stop_type']}" if d.get("stop_type") else "",
                        f"driver={d.get('driver_name') or d.get('driver_id')}" if d.get("driver_id") or d.get("driver_name") else "",
                        f"users={','.join(users) if isinstance(users, list) else users}" if users else "",
                        f"tour={str(d['tour_id'])[:8]}" if d.get("tour_id") else ""] if x)})
    return {"events": out[-80:]}


@app.get("/api/riders/{rid}", dependencies=[Depends(require_login)])
def api_rider(rid: str, period: str = "today"):
    _check_period(period)
    now = datetime.now(UTC)
    ins = store.insights(period, now, rules, sessions_ok=STATE['sync']['mode'] == 'api')
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
        data = store.insights(day, now, rules, sessions_ok=STATE['sync']['mode'] == 'api')
        data["day"] = day
    data["frozen"] = frozen
    data["trend"] = store.daily_trend(14)
    try:
        data["staffing"] = store.staffing_plan(now, rules)
    except Exception as e:
        log.exception("staffing plan failed")
        data["staffing"] = None
    data["brief"] = daily_brief(data, data.get("staffing"))
    return data


def daily_brief(d: dict, staffing: dict = None) -> str:
    """Short text for the team chat."""
    lines = [f"Quickzi {CITY} — {d.get('day', '')}",
             f"Orders: {d['delivered']} delivered, {d['cancelled']} cancelled",
             f"Within {rules.ptod_target_min} min: {d['within_pct'] if d['within_pct'] is not None else '–'}% (target {rules.target_within_pct}%) · avg PTOD {d['avg_ptod'] or '–'} min · late: {d['late']}"]
    if d.get("on_time_pct") is not None:
        lines.append(f"On time for the customer (planned time +{d.get('plan_grace', rules.plan_grace_min)} min): {d['on_time_pct']}% of {d.get('plan_known', 0)} orders · {d.get('late_vs_plan', 0)} late vs plan")
    ph = d["phases"]
    if ph.get("to_accept") is not None:
        lines.append(f"Avg minutes: accept {ph['to_accept']} · to restaurant {ph['to_restaurant']} · at restaurant {ph['at_restaurant']} · to customer {ph['to_customer']} · handover {ph['handover']}")
    if d.get("late_reasons"):
        lines.append("Late orders — reasons: " + " · ".join(f"{r} {n}" for r, n in d["late_reasons"].items())
                     + (f" · {d['late_without_reason']} without reason" if d.get("late_without_reason") else ""))
    if d["riders"]:
        best = d["riders"][0]
        lines.append(f"Best rider: {best['rider']} ({best['within_pct']}% within target, {best['delivered']} orders)")
    for f in d["focus"][:3]:
        lines.append(f"• {f['title']}")
    if staffing and staffing.get("hours"):
        peak = [h for h in staffing["hours"] if h["hour"] in staffing["peak_hours"]]
        lines.append(f"Riders for {staffing['tomorrow']} (from the last {staffing['days']} days): "
                     + " · ".join(f"{h['hour']:02d}–{(h['hour'] + 1) % 24:02d}h {h['plan']}" for h in peak)
                     + " — full plan in Insights")
    return "\n".join(lines)


@app.get("/api/settings", dependencies=[Depends(require_login)])
def api_settings_get():
    return {"rules": rules.as_dict(), "reasons": REASONS, "labels": {
        "ptod_target_min": "PTOD target (minutes from dispatch to delivered)", "ptod_warn_min": "PTOD warning at (minutes)",
        "target_within_pct": "Goal: % of orders within target", "accept_limit_min": "Alert if nobody accepted after (min)",
        "start_limit_min": "Alert if accepted but not started after (min)", "stationary_min": "Alert if not moving for (min)",
        "wrong_way_m": "Alert if further from next stop by (metres)", "late_grace_min": "Alert if behind ETA by (min)",
        "wait_restaurant_min": "Alert if waiting at restaurant (min)", "wait_customer_min": "Alert if waiting at customer (min)",
        "release_lead_min": "Pre-orders are released to riders this many min before the planned delivery (MotionTools auto-scheduling)",
        "plan_grace_min": "On time for the customer = delivered no later than the planned time + (min)",
        "riders_capacity_per_hour": "Staffing plan: orders one rider delivers per hour",
        "day_start_hour": "Operating day starts at this hour (Berlin): 0 = midnight, 4 = orders after midnight count for the evening before"}}


@app.post("/api/settings", dependencies=[Depends(require_login)])
async def api_settings_set(request: Request):
    values = await request.json()
    rules.apply(values)
    projector.lead_min = rules.release_lead_min
    if store_mod.set_day_start(rules.day_start_hour) != rules.day_start_hour:
        rules.day_start_hour = store_mod.DAY_STARTS_AT
    STATE["sync"]["last_snapshot"] = None                 # the day boundary may have moved — re-freeze yesterday
    store.set_settings({k: v for k, v in values.items() if k in Rules.EDITABLE})
    store.log("info", "thresholds changed: " + ", ".join(f"{k}={v}" for k, v in values.items() if k in Rules.EDITABLE))
    return {"ok": True, "rules": rules.as_dict()}


@app.post("/api/admin/delete-day", dependencies=[Depends(require_login)])
async def api_delete_day(request: Request):
    """Settings → System: wipe one day that was recorded wrongly. {"day": "YYYY-MM-DD", "dry_run": true} only counts."""
    body = await request.json()
    day = str(body.get("day") or "").strip()
    if not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
        raise HTTPException(400, "day must be YYYY-MM-DD")
    now = datetime.now(UTC)
    res = store.delete_day(day, now, dry_run=bool(body.get("dry_run")))
    if res["deleted"]:
        for key in [k for k in STATE["open_alerts"] if k[0] not in STATE["orders"]]:   # alerts of deleted orders
            STATE["open_alerts"].pop(key, None); STATE["sev"].pop(key, None); STATE["heads"].pop(key, None)
        if STATE["sync"]["last_snapshot"] == day:
            STATE["sync"]["last_snapshot"] = None
    return res


@app.get("/api/system", dependencies=[Depends(require_login)])
def api_system():
    s = STATE["sync"]
    return {"version": VERSION, "started": iso(STARTED), "uptime_min": int((datetime.now(UTC) - STARTED).total_seconds() // 60), "sync": s,
            "mode": s["mode"], "event_counts": projector.counts, "endpoint_summary": mt.endpoint_summary(), "disk": disk_report(),
            "api": mt.stats, "areas": AREAS, "sync_seconds": SYNC_SECONDS, "log": store.syslog(40),
            "db_orders": len(store.orders_in("month", datetime.now(UTC))), "samples": STATE["raw_samples"]}


@app.post("/api/alerts/{aid}/dismiss", dependencies=[Depends(require_login)])
def api_dismiss(aid: int):
    STATE["hidden"][aid] = True
    return {"ok": store.dismiss_alert(aid, datetime.now(UTC))}


@app.post("/api/alerts/{aid}/snooze", dependencies=[Depends(require_login)])
def api_snooze(aid: int, minutes: int = 10):
    until = datetime.now(UTC) + timedelta(minutes=max(1, min(minutes, 120)))
    STATE["hidden"][aid] = until
    return {"ok": store.snooze_alert(aid, until)}


@app.get("/export.csv", dependencies=[Depends(require_login)])
def export_csv(period: str = "today"):
    _check_period(period)
    body = store.export_csv(period, datetime.now(UTC), rules)
    return PlainTextResponse(body, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="quickzi-{CITY.lower()}-{period}.csv"'})


@app.get("/export-events.jsonl", dependencies=[Depends(require_login)])
def export_events(n: int = 300):
    """The last raw webhook events, for checking field names and event flows."""
    lines = recent_event_lines(max(1, min(n, 2000)))
    return PlainTextResponse("\n".join(lines), media_type="application/json",
                             headers={"Content-Disposition": 'attachment; filename="motiontools-events.jsonl"'})


@app.get("/health")
def health():
    s = STATE["sync"]
    return {"ok": True, "version": VERSION, "city": CITY, "live_orders": len(STATE["orders"]), "riders_known": len(STATE["riders"]),
            "open_alerts": len(STATE["open_alerts"]), "uptime_min": int((datetime.now(UTC) - STARTED).total_seconds() // 60),
            "setup": {"dashboard_password_set": bool(DASH_PASSWORD), "motiontools_token_set": mt.enabled,
                      "webhook_secret_set": PATH_SECRET != "change-me", "data_dir": str(DATA_DIR), "areas": AREAS},
            "sync": {k: s[k] for k in ("last_ok", "last_error", "runs", "orders_seen", "riders_seen", "backfilled", "webhook_events", "last_snapshot",
                                       "last_webhook", "silent_min", "webhook_silent", "mode")},
            "api": mt.stats}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard():
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
