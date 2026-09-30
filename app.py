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


async def enrich_rider(driver_id: str):
    """Fetch name/phone once per rider using the API token."""
    r = det.rider(driver_id)
    if r.name != "Unknown rider" or not MT_TOKEN:
        return
    headers = {"Authorization": f"Bearer {MT_TOKEN}", "Accept": "application/json", "Accept-Language": "en"}
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            res = await c.get(f"{MT_API}/api/users/{driver_id}", headers=headers)
            u = res.json().get("user", {}) if res.status_code == 200 else {}
            prof = u.get("profile") or u
            name = " ".join(x for x in [prof.get("first_name"), prof.get("last_name")] if x)
            r.name = name or u.get("email") or r.name
            r.phone = prof.get("phone_number") or r.phone
        except Exception as e:
            log.warning("rider lookup failed for %s: %s", driver_id, e)


# ---------- webhook ----------
@app.post("/mt/{secret}")
async def webhook(secret: str, request: Request):
    if not secrets.compare_digest(secret, PATH_SECRET):
        raise HTTPException(404)
    p = await request.json()
    with open(DATA_DIR / "events.jsonl", "a") as f:     # raw log: used to confirm field names
        f.write(json.dumps(p) + "\n")

    event = dig(p, "event", "type", "name") or request.headers.get("X-Event-Type", "")
    now = ts(dig(p, "created_at", "occurred_at", "timestamp")) or datetime.now(UTC)
    did = driver_id_of(p)
    if not did:
        return {"ok": True, "ignored": "no driver"}
    await enrich_rider(did)

    if event.endswith("driver_location_updated"):
        lat = dig(p, "data.location.lat", "data.driver_location.lat", "data.lat")
        lng = dig(p, "data.location.lng", "data.driver_location.lng", "data.lng")
        if lat is not None and lng is not None:
            det.on_location(did, float(lat), float(lng), now)

    elif event.endswith("etas_recalculated") or event in ("tour.created", "tour.modified", "tour.force_assigned"):
        ref = dig(p, "data.booking.reference", "data.booking.id", "data.tour.id", "data.id") or ""
        for s in stops_of(p):
            addr = dig(s, "location.address", "address", "location.name", "place.name") or ""
            lat = dig(s, "location.lat", "lat", "location.latitude")
            lng = dig(s, "location.lng", "lng", "location.longitude")
            kind = "pickup" if "pick" in str(dig(s, "type", "kind", "stop_type") or "").lower() else "dropoff"
            det.on_stop_eta(did, Stop(
                stop_id=str(s.get("id")), kind=kind,
                booking_ref=str(dig(s, "booking.reference", "booking_id") or ref),
                address=str(addr),
                lat=float(lat) if lat is not None else None, lng=float(lng) if lng is not None else None,
                deadline=ts(s.get("latest_arrival_at")),
                eta=ts(dig(s, "eta", "estimated_arrival_at", "expected_arrival_at"))), now)

    elif event.endswith("stop_arrived"):
        det.on_stop_arrived(did, str(dig(p, "data.stop.id", "data.stop_id")), now)

    elif event.endswith("stop_completed"):
        det.on_stop_completed(did, str(dig(p, "data.stop.id", "data.stop_id")), now)
    elif event.endswith("stop_failed"):
        det.on_stop_completed(did, str(dig(p, "data.stop.id", "data.stop_id")), now, failed=True)

    elif event == "driver.online":
        det.on_online(did, True, now)
    elif event == "driver.offline":
        det.on_online(did, False, now)
    elif event == "driver.service_area_changed":
        det.rider(did).service_area_id = dig(p, "data.service_area.id", "data.service_area_id")

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
                      "data_dir": str(DATA_DIR)}}


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
