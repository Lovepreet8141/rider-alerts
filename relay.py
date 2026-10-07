"""Quickzi webhook relay — the always-on catcher between MotionTools and the dashboard.

MotionTools switches a webhook OFF after 250 failed deliveries.  Every dashboard update restarts the dashboard server
on Railway, and with 25 cities a restart alone fails a few hundred GPS events.  This tiny service never changes, so it
never restarts: it answers MotionTools at once, keeps every event in order, and hands them to the dashboard in batches.
When the dashboard is restarting or busy, events wait here (up to RELAY_MAX) and arrive a moment later — nothing lost,
MotionTools never sees an error.

Railway: second service from the same GitHub repo — NO variables needed
  Start command   uvicorn relay:app --host 0.0.0.0 --port $PORT
  Watch paths     relay.py            (so dashboard updates never redeploy the relay)
MotionTools webhook URL → https://<relay-domain>/mt/<the same secret as before>
The relay accepts whatever secret is in the URL and passes it on to the dashboard, which checks it. A wrong secret
shows up in the relay's /health (last_error) — MotionTools still never sees a failure, so it can never block.
"""
from __future__ import annotations

import asyncio
import collections
import os
import secrets
import time

import httpx
from fastapi import FastAPI, HTTPException, Request

SECRET = os.environ.get("WEBHOOK_PATH_SECRET", "").strip()          # optional: otherwise the one in MotionTools' URL
TARGET = (os.environ.get("DASHBOARD_URL", "").strip() or "https://web-production-a68a4d.up.railway.app").rstrip("/")
MAX = int(os.environ.get("RELAY_MAX", "300000"))
BATCH = int(os.environ.get("RELAY_BATCH", "300"))

app = FastAPI(title="Quickzi webhook relay")
BUF: collections.deque = collections.deque()
ST = {"received": 0, "forwarded": 0, "dropped_full": 0, "rejected": 0, "fail_streak": 0, "last_ok": None, "last_error": "",
      "started": time.strftime("%Y-%m-%d %H:%M:%S")}
WAKE = asyncio.Event()


@app.post("/mt/{secret}")
async def catch(secret: str, request: Request):
    if SECRET and not secrets.compare_digest(secret, SECRET):
        ST["rejected"] += 1                 # counted, but still answered 200: MotionTools must never see a failure
        return {"ok": True}
    ST["path"] = secret
    try:
        p = await request.json()
    except Exception:
        return {"ok": True}
    if len(BUF) >= MAX:
        BUF.popleft()                       # never answer MotionTools with an error; the oldest event goes first
        ST["dropped_full"] += 1
    BUF.append(p)
    ST["received"] += 1
    WAKE.set()
    return {"ok": True}


@app.get("/")
@app.get("/health")
def health():
    return {"ok": True, "role": "relay", "waiting": len(BUF), "target": TARGET,
            **{k: v for k, v in ST.items() if k != "path"}, "secret_seen": bool(SECRET or ST.get("path"))}


async def forwarder():
    backoff = 1.0
    async with httpx.AsyncClient(timeout=30) as cli:
        while True:
            if not BUF:
                WAKE.clear()
                try:
                    await asyncio.wait_for(WAKE.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue
            key = SECRET or ST.get("path") or ""
            if not key:
                await asyncio.sleep(2)
                continue
            n = min(BATCH, len(BUF))
            chunk = [BUF[i] for i in range(n)]
            try:
                r = await cli.post(f"{TARGET}/mt/{key}/batch", json=chunk)
                if r.status_code in (404, 405):
                    # dashboard still on an older version without /batch: hand them over one by one
                    ok = 0
                    for p in chunk:
                        r1 = await cli.post(f"{TARGET}/mt/{key}", json=p)
                        if r1.status_code != 200:
                            break
                        ok += 1
                    for _ in range(ok):
                        BUF.popleft()
                    ST["forwarded"] += ok
                    if ok == n:
                        ST["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        ST["fail_streak"], backoff = 0, 1.0
                        continue
                    r = r1
                if r.status_code == 200:
                    for _ in range(n):
                        BUF.popleft()
                    ST["forwarded"] += n
                    ST["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    ST["fail_streak"], backoff = 0, 1.0
                    ST["last_error"] = ""
                    if len(BUF) < BATCH:
                        await asyncio.sleep(0.5)          # collect a little — fewer, bigger calls
                    continue
                ST["last_error"] = f"dashboard answered {r.status_code}" + (" — the secret in the MotionTools URL does not match WEBHOOK_PATH_SECRET of the dashboard" if r.status_code == 404 else "")
            except Exception as e:
                ST["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            ST["fail_streak"] += 1
            await asyncio.sleep(backoff)                 # dashboard restarting / busy: keep the events, try again
            backoff = min(backoff * 2, 15.0)


@app.on_event("startup")
async def start():
    asyncio.create_task(forwarder())
