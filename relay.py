"""Quickzi webhook relay — the always-on catcher between MotionTools and the dashboard.

MotionTools switches a webhook OFF after 250 failed deliveries in a row. The dashboard restarts on every update and
can be busy for a moment; this tiny service never changes, never touches a database and answers every call at once
with 200. It keeps the events in order and hands them to the dashboard in batches; while the dashboard is
restarting or busy they wait here and arrive a few seconds later — nothing lost, MotionTools never sees an error.

Railway: a second service from the same GitHub repo — NO variables, NO volume needed
  Start command    uvicorn relay:app --host 0.0.0.0 --port $PORT
  Watch paths      relay.py          (dashboard updates never redeploy the relay)
  Healthcheck path /health           (a relay update itself then has no gap: Railway waits for the new one)
  Networking       Generate domain
MotionTools webhook URL → https://<relay-domain>/mt/<the same secret as before>
Optional variables: DASHBOARD_URL (default below), WEBHOOK_PATH_SECRET (default: the secret in MotionTools' URL).
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import secrets
import time

import httpx
from fastapi import FastAPI, Request

SECRET = os.environ.get("WEBHOOK_PATH_SECRET", "").strip()
TARGET = (os.environ.get("DASHBOARD_URL", "").strip() or "https://web-production-a68a4d.up.railway.app").rstrip("/")
MAX = int(os.environ.get("RELAY_MAX", "500000"))
BATCH = int(os.environ.get("RELAY_BATCH", "300"))

app = FastAPI(title="Quickzi webhook relay")
BUF: collections.deque = collections.deque()          # (received_at, event)
ST = {"received": 0, "forwarded": 0, "dropped_full": 0, "rejected": 0, "bad_body": 0, "fail_streak": 0,
      "last_ok": None, "last_error": "", "started": time.strftime("%Y-%m-%d %H:%M:%S")}
WAKE = asyncio.Event()
STOP = {"now": False}


def _key() -> str:
    return SECRET or ST.get("path") or ""


@app.api_route("/mt/{secret}", methods=["POST", "PUT", "PATCH"])
async def catch(secret: str, request: Request):
    """Every call is answered 200 immediately — whatever it carries."""
    if SECRET and not secrets.compare_digest(secret, SECRET):
        ST["rejected"] += 1
        return {"ok": True}
    ST["path"] = secret
    try:
        body = await request.body()
        p = json.loads(body) if body else None
    except Exception:
        ST["bad_body"] += 1
        return {"ok": True}
    items = p if isinstance(p, list) else [p]
    now = time.time()
    for e in items:
        if not isinstance(e, dict):
            continue
        if len(BUF) >= MAX:
            BUF.popleft()                   # only after hours of outage: the oldest event goes first
            ST["dropped_full"] += 1
        BUF.append((now, e))
        ST["received"] += 1
    WAKE.set()
    return {"ok": True}


@app.api_route("/mt/{secret}", methods=["GET", "HEAD"])
def probe(secret: str):
    return {"ok": True}


@app.get("/")
@app.get("/health")
def health():
    oldest = round(time.time() - BUF[0][0]) if BUF else 0
    return {"ok": True, "role": "relay", "waiting": len(BUF), "oldest_waiting_s": oldest, "target": TARGET,
            "secret_seen": bool(_key()), **{k: v for k, v in ST.items() if k != "path"}}


async def _send(cli: httpx.AsyncClient, n: int) -> bool:
    """Hand the first n waiting events to the dashboard; True when it took them."""
    key = _key()
    chunk = [BUF[i][1] for i in range(n)]
    r = await cli.post(f"{TARGET}/mt/{key}/batch", json=chunk)
    if r.status_code == 405:
        # a dashboard older than 6.8 has no /batch: hand them over one by one
        for i, e in enumerate(chunk):
            r1 = await cli.post(f"{TARGET}/mt/{key}", json=e)
            if r1.status_code != 200:
                for _ in range(i):
                    BUF.popleft()
                ST["forwarded"] += i
                ST["last_error"] = f"dashboard answered {r1.status_code}"
                return False
        r = r1
    if r.status_code == 200:
        for _ in range(n):
            BUF.popleft()
        ST["forwarded"] += n
        ST["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ST["fail_streak"], ST["last_error"] = 0, ""
        return True
    ST["last_error"] = f"dashboard answered {r.status_code}" + (
        " — the secret in the MotionTools URL does not match WEBHOOK_PATH_SECRET of the dashboard" if r.status_code == 404 else "")
    return False


async def forwarder():
    backoff = 1.0
    async with httpx.AsyncClient(timeout=60) as cli:
        while not STOP["now"]:
            if not BUF or not _key():
                WAKE.clear()
                try:
                    await asyncio.wait_for(WAKE.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if await _send(cli, min(BATCH, len(BUF))):
                    backoff = 1.0
                    if len(BUF) < BATCH:
                        await asyncio.sleep(0.5)          # collect a little — fewer, bigger calls
                    continue
            except Exception as e:
                ST["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            ST["fail_streak"] += 1
            await asyncio.sleep(backoff)                 # dashboard restarting / busy: keep the events, try again
            backoff = min(backoff * 2, 10.0)


@app.on_event("startup")
async def start():
    asyncio.create_task(forwarder())


@app.on_event("shutdown")
async def stop():
    """A relay update: Railway starts the new relay first, then stops this one — hand over what is still waiting."""
    STOP["now"] = True
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=10) as cli:
        while BUF and _key() and time.monotonic() - t0 < 20:
            try:
                if not await _send(cli, min(BATCH, len(BUF))):
                    await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)
