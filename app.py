"""Quickzi Munich rider alerts — webhook receiver, checks every minute, and a dashboard.

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Env:  MT_API_TOKEN          MotionTools API token (Rider-Alerts)
      DASHBOARD_PASSWORD    password for the dashboard (any username)
      WEBHOOK_PATH_SECRET   random word that goes at the end of the webhook URL
      DATA_DIR              where the database is kept (Railway volume, e.g. /data)
      MUNICH_SERVICE_AREA_ID  optional; empty = track all riders

NOTE: MotionTools' exact webhook payload fields could not be fully confirmed from the
public docs. The `dig(...)` calls accept the common shapes; every raw event is saved
to events.jsonl so the field paths can be checked against real data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from detector import UTC, Config, Detector, Stop
from store import Store

log = logging.getLogger("rider-alerts")
logging.basicConfig(level=logging.INFO)

MT_API = "https://api.motiontools.io"
def env(name, default=""):
    """Read a setting, forgiving stray spaces/quotes/backticks pasted into Railway,
    and variable names that were saved with extra characters around them."""
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

store = Store(str(DATA_DIR / "rider_alerts.db"))
cfg = Config(munich_service_area_id=os.environ.get("MUNICH_SERVICE_AREA_ID") or None)
det = Detector(cfg, store)
app = FastAPI()
basic = HTTPBasic()
# lightweight counters so setup can be checked from /health without exposing data
STATS = {"events_received": 0, "events_without_driver": 0, "last_event_at": None, "event_types": {}}
RECENT: list = []          # last 30 raw events, visible on /api/events (password protected)


def require_login(creds: HTTPBasicCredentials = Depends(basic)):
    typed = creds.password.strip()
    if not DASH_PASSWORD or not secrets.compare_digest(typed.encode(), DASH_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password", headers={"WWW-Authenticate": "Basic"})


# ---------- helpers to read payloads defensively ----------
def dig(d, *paths):
    for p in paths:
        cur = d
        for k in p.split("."):
            cur = cur.get(k) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is not None:
            return cur
    return None


def ts(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def driver_id_of(p):
    return dig(p, "data.driver.id", "data.driver_id", "data.tour.driver.id",
               "data.booking.driver.id", "data.user.id", "driver.id", "driver_id")


def stops_of(p):
    return dig(p, "data.stops", "data.tour.stops", "data.booking.stops", "stops") or []


AUTH = {"Authorization": f"Bearer {MT_TOKEN}", "Accept": "application/json", "Accept-Language": "en"}
PHONE_LOOKED_UP: set = set()
BOOKINGS: dict = {}        # booking_id -> {driver_id, ref, area, first_seen, stops: {stop_id: Stop}, fetched}
SAMPLES: list = []         # first few raw booking API responses, for checking field names


async def enrich_rider(driver_id: str, name: str = ""):
    """Use the name MotionTools sends; look the phone number up once per rider."""
    r = det.rider(driver_id)
    if name:
        r.name = name
    if driver_id in PHONE_LOOKED_UP or not MT_TOKEN:
        return
    PHONE_LOOKED_UP.add(driver_id)
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            res = await c.get(f"{MT_API}/api/users/{driver_id}", headers=AUTH)
            u = res.json().get("user", {}) if res.status_code == 200 else {}
            prof = u.get("profile") or u
            full = " ".join(x for x in [prof.get("first_name"), prof.get("last_name")] if x)
            if full and r.name == "Unknown rider":
                r.name = full
            r.phone = prof.get("phone_number") or u.get("phone_number") or r.phone
        except Exception as e:
            log.warning("rider lookup failed for %s: %s", driver_id, e)


async def fetch_booking(bid: str, b: dict):
    """Once per booking: ask MotionTools for driver, creation time and stop addresses/coordinates."""
    if b.get("fetched") or not MT_TOKEN:
        return
    b["fetched"] = True
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            res = await c.get(f"{MT_API}/api/bookings/{bid}", headers=AUTH)
            if res.status_code != 200:
                log.warning("booking lookup %s -> %s", bid, res.status_code)
                return
            raw = res.json()
        except Exception as e:
            log.warning("booking lookup failed for %s: %s", bid, e)
            return
    if len(SAMPLES) < 3:
        SAMPLES.append(raw)
    bk = raw.get("booking", raw) if isinstance(raw, dict) else {}
    did = dig(bk, "driver.id", "driver_id", "tour.driver.id", "assigned_driver.id")
    if did and not b.get("driver_id"):
        b["driver_id"] = str(did)
    created = ts(dig(bk, "created_at", "requested_at", "placed_at"))
    if created:
        b["first_seen"] = min(b["first_seen"], created)
    for st in dig(bk, "stops") or []:
        sid = str(st.get("id"))
        stop = b["stops"].setdefault(sid, Stop(sid))
        kind = str(dig(st, "type", "stop_type") or "").lower()
        if kind:
            stop.kind = "pickup" if "pick" in kind else "dropoff"
        lat = dig(st, "location.lat", "location.latitude", "lat", "place.location.lat")
        lng = dig(st, "location.lng", "location.longitude", "lng", "place.location.lng")
        if lat is not None and lng is not None:
            stop.lat, stop.lng = float(lat), float(lng)
        addr = dig(st, "location.address", "address", "location.formatted_address", "place.name", "location.name")
        if addr:
            stop.address = str(addr)
        stop.deadline = stop.deadline or ts(st.get("latest_arrival_at"))


def push_booking(bid: str, now):
    """Hand a booking's stops to the rider's tracker once we know who the rider is."""
    b = BOOKINGS.get(bid)
    if not b or not b.get("driver_id"):
        return None
    did = b["driver_id"]
    for stop in b["stops"].values():
        if stop.done:
            continue
        stop.booking_ref = b["ref"]
        stop.assigned_at = b["first_seen"]
        det.on_stop_eta(did, stop, now)
    r = det.rider(did)
    if b.get("area"):
        r.service_area_id = b["area"]
    return did


# ---------- webhook ----------
@app.post("/mt/{secret}")
async def webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, PATH_SECRET):
        raise HTTPException(404)
    p = await request.json()
    with open(DATA_DIR / "events.jsonl", "a") as f:     # raw log
        f.write(json.dumps(p) + "\n")

    # MotionTools sends {"resource_type": "booking", "event": "stop_arrived", "data": {...}}
    rtype, ev = str(p.get("resource_type") or ""), str(p.get("event") or p.get("type") or "")
    event = ev if "." in ev or not rtype else f"{rtype}.{ev}"
    d = p.get("data") or {}
    now = ts(p.get("timestamp") or d.get("timestamp")) or datetime.now(UTC)

    STATS["events_received"] += 1
    STATS["last_event_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    STATS["event_types"][event or "?"] = STATS["event_types"].get(event or "?", 0) + 1
    area = d.get("service_area_id")
    if area:
        STATS.setdefault("service_areas", {})
        STATS["service_areas"][area] = STATS["service_areas"].get(area, 0) + 1
    RECENT.append({"received_at": STATS["last_event_at"], "body": p})
    del RECENT[:-30]

    # ---- booking events: tie everything to the booking, then to its rider ----
    bid = d.get("booking_id") or dig(d, "booking.id")
    if bid:
        b = BOOKINGS.setdefault(bid, {"driver_id": None, "ref": "", "area": None, "first_seen": now,
                                      "stops": {}, "fetched": False})
        b["ref"] = d.get("external_id") or b["ref"] or bid[:8]
        b["area"] = area or b["area"]
        if d.get("driver_id"):
            b["driver_id"] = d["driver_id"]
            await enrich_rider(d["driver_id"], d.get("driver_name") or "")
        await fetch_booking(bid, b)

        if event.endswith("etas_recalculated"):
            for s in d.get("unfinished_stops_info") or []:
                sid = str(s.get("id"))
                stop = b["stops"].setdefault(sid, Stop(sid))
                stop.kind = "pickup" if "pick" in str(s.get("type", "")).lower() else "dropoff"
                stop.eta = ts(s.get("eta")) or stop.eta
        elif d.get("stop_id"):
            sid = str(d["stop_id"])
            stop = b["stops"].setdefault(sid, Stop(sid))
            if d.get("stop_type"):
                stop.kind = "pickup" if "pick" in str(d["stop_type"]).lower() else "dropoff"

        did = push_booking(bid, now)
        if not did:
            STATS["events_without_driver"] += 1
            return {"ok": True, "waiting_for_driver": True}

        sid = str(d.get("stop_id") or "")
        if event.endswith("stop_arrived") and sid:
            det.on_stop_arrived(did, sid, now)
        elif event.endswith("stop_completed") and sid:
            det.on_stop_completed(did, sid, now)
            if all(st.done for st in b["stops"].values()):
                BOOKINGS.pop(bid, None)
        elif event.endswith("stop_failed") and sid:
            det.on_stop_completed(did, sid, now, failed=True)
        return {"ok": True}

    # ---- driver / tour events ----
    did = dig(d, "driver_id", "driver.id", "tour.driver.id", "user_id", "id" if rtype == "driver" else "_")
    if not did:
        STATS["events_without_driver"] += 1
        return {"ok": True, "ignored": "no driver"}
    did = str(did)
    await enrich_rider(did, d.get("driver_name") or dig(d, "driver.name") or "")
    if area:
        det.rider(did).service_area_id = area

    if event.endswith("driver_location_updated"):
        lat = dig(d, "driver_location.lat", "location.lat", "driver_location.latitude", "location.latitude", "lat")
        lng = dig(d, "driver_location.lng", "location.lng", "driver_location.longitude", "location.longitude", "lng")
        if lat is not None and lng is not None:
            det.on_location(did, float(lat), float(lng), now)
    elif event.endswith(".online"):
        det.on_online(did, True, now)
    elif event.endswith(".offline"):
        det.on_online(did, False, now)
    return {"ok": True}


# ---------- dashboard ----------
@app.get("/health")
def health():
    # setup check without revealing any secret values
    return {"ok": True, "riders_tracked": len(det.riders), "open_issues": len(det.open_issues),
            "setup": {"dashboard_password_set": bool(DASH_PASSWORD),
                      "dashboard_password_length": len(DASH_PASSWORD),
                      "motiontools_token_set": bool(MT_TOKEN),
                      "webhook_secret_set": PATH_SECRET != "change-me",
                      "data_dir": str(DATA_DIR)},
            "events": STATS}


@app.get("/api/events", dependencies=[Depends(require_login)])
def api_events():
    """Last 30 raw webhook events + first booking lookups — used to check MotionTools' field names."""
    return {"booking_lookups": SAMPLES, "events": RECENT[::-1]}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_login)])
@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_login)])
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/live", dependencies=[Depends(require_login)])
def api_live():
    return {"incidents": store.live(datetime.now(UTC))}


@app.get("/api/performance", dependencies=[Depends(require_login)])
def api_performance(period: str = "today"):
    if period not in ("today", "week", "month"):
        raise HTTPException(400, "period must be today, week or month")
    return {"period": period, "riders": store.performance(period, datetime.now(UTC))}


@app.get("/api/rider/{driver_id}", dependencies=[Depends(require_login)])
def api_rider(driver_id: str):
    return {"incidents": store.rider_history(driver_id, datetime.now(UTC))}


# ---------- background check every minute ----------
async def checker():
    while True:
        try:
            det.check(datetime.now(UTC))
        except Exception as e:
            log.exception("check failed: %s", e)
        await asyncio.sleep(60)


@app.on_event("startup")
async def start():
    if not DASH_PASSWORD:
        log.warning("DASHBOARD_PASSWORD is not set — the dashboard will refuse all logins")
    asyncio.create_task(checker())
