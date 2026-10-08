"""Quickzi webhook relay — the always-on catcher between MotionTools and the dashboard.

MotionTools switches a webhook OFF after 250 failed deliveries in a row. The dashboard restarts on every update and
can be busy for a moment; this tiny service never changes, never touches a database and answers every call at once
with 200. It keeps the events in order and hands them to the dashboard in batches; while the dashboard is
restarting or busy they wait here and arrive a few seconds later — nothing lost, MotionTools never sees an error.

Railway: its own service — no volume needed
  Start command    uvicorn relay:app --host 0.0.0.0 --port $PORT
  Healthcheck path /health           (a relay update then has no gap: Railway waits for the new one)
  Networking       Generate domain
  Variable         MT_WEBHOOK_PUBLIC_KEY = the public key shown on the webhook's page in MotionTools
MotionTools webhook URL → https://<relay-domain>/mt/<the same secret as the dashboard>
Optional variables: DASHBOARD_URL (default below), WEBHOOK_PATH_SECRET (default: the secret in MotionTools' URL).

MotionTools' webhook rules (docs.motiontools.io → Webhooks), all kept here:
  * answer 2xx within 2 seconds, process later   → every call is answered at once, events are handed on afterwards
  * verify the X-Mtools-Signature (Ed25519)      → checked on the raw body with MT_WEBHOOK_PUBLIC_KEY; a call
    without a valid signature is answered 200 but never reaches the dashboard. Safe switch-on: the relay starts in
    "checking" (forwards everything, counts) and enforces only once 20 real signatures matched the key — a mistyped
    key can never blind the dashboard. If 50 calls in a row fail while enforcing (MotionTools issued a new key), it
    goes back to "checking" and says so on /health.
  * at-least-once delivery (repeats possible)    → the dashboard ignores an event it has already processed.

v2: every event remembers the address it came in on, so a stray call to a wrong address can never hold up the
real ones; /health shows why a call could not be read (never its content).
v3: signature check.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import os
import secrets
import time

import httpx
from fastapi import FastAPI, Request

VERSION = "3"
SECRET = os.environ.get("WEBHOOK_PATH_SECRET", "").strip()
TARGET = (os.environ.get("DASHBOARD_URL", "").strip() or "https://web-production-a68a4d.up.railway.app").rstrip("/")
MAX = int(os.environ.get("RELAY_MAX", "500000"))
BATCH = int(os.environ.get("RELAY_BATCH", "300"))

app = FastAPI(title="Quickzi webhook relay")
BUF: collections.deque = collections.deque()          # (received_at, address, event)
ST = {"received": 0, "forwarded": 0, "dropped_full": 0, "rejected": 0, "bad_body": 0, "empty_body": 0,
      "wrong_address_dropped": 0, "fail_streak": 0, "last_ok": None, "last_error": "", "last_bad": None,
      "good": "", "started": time.strftime("%Y-%m-%d %H:%M:%S")}
WAKE = asyncio.Event()
STOP = {"now": False}
SEND_LOCK = asyncio.Lock()                            # one hand-over at a time (forwarder and shutdown never overlap)
REFUSED: set = set()                                  # addresses the dashboard refused since the last success

# ---------------- signature (MotionTools: "The signature must be validated on your side") ----------------
LEARN = 20                    # matching signatures needed before calls without one are refused
GIVE_UP = 50                  # failed checks in a row while enforcing → MotionTools probably has a new key
SIG = {"mode": "off", "ok": 0, "bad": 0, "missing": 0, "refused": 0, "fail_run": 0, "note": ""}
VERIFY = None
HELD: collections.deque = collections.deque(maxlen=GIVE_UP)   # refused calls of the current failing run (secret, body)


def _load_key():
    """MT_WEBHOOK_PUBLIC_KEY as MotionTools shows it (base64 of the 32-byte key); PEM / DER base64 work too."""
    global VERIFY
    raw = os.environ.get("MT_WEBHOOK_PUBLIC_KEY", "").strip()
    if not raw:
        SIG["note"] = "off — add MT_WEBHOOK_PUBLIC_KEY (MotionTools → Webhooks → your webhook → public key) to switch it on"
        return
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key, load_pem_public_key
    except Exception:
        SIG["note"] = "off — the cryptography package is missing: add it to requirements.txt"
        return
    try:
        if "BEGIN PUBLIC KEY" in raw:
            key = load_pem_public_key(raw.encode())
        else:
            b64 = "".join(raw.split())
            b = base64.b64decode(b64 + "=" * (-len(b64) % 4))
            key = Ed25519PublicKey.from_public_bytes(b) if len(b) == 32 else load_der_public_key(b)
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("not an Ed25519 key")
    except Exception as e:
        SIG["note"] = f"off — MT_WEBHOOK_PUBLIC_KEY could not be read ({type(e).__name__}): copy it again from MotionTools"
        return
    VERIFY = key.verify
    SIG["mode"], SIG["note"] = "checking", f"checking — refusing unsigned calls once {LEARN} signatures have matched"


def _signature_lets_through(request: Request, body: bytes) -> bool:
    """True = hand the event on. Never raises; the caller answers 200 either way."""
    if VERIFY is None:
        return True
    sig = (request.headers.get("x-mtools-signature") or "").strip()
    good = False
    if not sig:
        SIG["missing"] += 1
    else:
        try:
            VERIFY(base64.b64decode(sig + "=" * (-len(sig) % 4)), body)
            good = True
        except Exception:
            SIG["bad"] += 1
    if good:
        SIG["ok"] += 1
        SIG["fail_run"] = 0
        HELD.clear()
        if SIG["mode"] == "checking" and SIG["ok"] >= LEARN:
            SIG["mode"], SIG["note"] = "enforcing", "on — calls without a valid MotionTools signature are refused"
        return True
    SIG["fail_run"] += 1
    if SIG["mode"] == "enforcing":
        if SIG["fail_run"] >= GIVE_UP:
            SIG["mode"], SIG["ok"] = "checking", 0
            SIG["note"] = (f"{GIVE_UP} calls in a row did not match the key — MotionTools may have issued a new one: "
                           "update MT_WEBHOOK_PUBLIC_KEY. Until then every event is handed on.")
            held = list(HELD)                            # the run that tripped this was most likely real: hand it on too
            HELD.clear()
            SIG["refused"] -= len(held)
            for sec, b in held:
                _enqueue(sec, b)
            return True
        SIG["refused"] += 1
        HELD.append((request.path_params.get("secret", ""), body))
        return False
    if SIG["ok"] == 0 and SIG["bad"] + SIG["missing"] >= 200:
        SIG["note"] = ("checking — no signature has matched MT_WEBHOOK_PUBLIC_KEY yet: copy the key again from "
                       "MotionTools. Every event is still handed on.")
    return True


_load_key()


def _bad(reason: str, secret: str, request: Request, size: int):
    """Remember why a call could not be read — never its content (this page is public)."""
    ST["bad_body"] += 1
    ST["last_bad"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason, "bytes": size,
                      "content_type": (request.headers.get("content-type") or "")[:60],
                      "encoding": (request.headers.get("content-encoding") or "")[:30],
                      "on_motiontools_address": bool(ST["good"]) and secret == ST["good"]}


def _enqueue(secret: str, body: bytes) -> bool:
    """Queue the events of one call in arrival order; False when the body is not JSON."""
    try:
        p = json.loads(body)
    except Exception:
        return False
    items = p if isinstance(p, list) else [p]
    now = time.time()
    for e in items:
        if not isinstance(e, dict):
            continue
        if len(BUF) >= MAX:
            BUF.popleft()                   # only after hours of outage: the oldest event goes first
            ST["dropped_full"] += 1
        BUF.append((now, secret, e))
        ST["received"] += 1
    WAKE.set()
    return True


@app.api_route("/mt/{secret}", methods=["POST", "PUT", "PATCH"])
async def catch(secret: str, request: Request):
    """Every call is answered 200 immediately — whatever it carries."""
    if SECRET and not secrets.compare_digest(secret, SECRET):
        ST["rejected"] += 1
        return {"ok": True}
    try:
        body = await request.body()
    except Exception as e:                              # the sender hung up before the body arrived
        _bad(f"connection closed while sending ({type(e).__name__})", secret, request, 0)
        return {"ok": True}
    if not body.strip():
        ST["empty_body"] += 1
        return {"ok": True}
    if not _signature_lets_through(request, body):
        return {"ok": True}                             # forged or unsigned: answered, never handed on
    if not _enqueue(secret, body):
        _bad("not JSON", secret, request, len(body))
    return {"ok": True}


@app.api_route("/mt/{secret}", methods=["GET", "HEAD"])
def probe(secret: str):
    return {"ok": True}


@app.get("/")
@app.get("/health")
def health():
    oldest = round(time.time() - BUF[0][0]) if BUF else 0
    return {"ok": True, "role": "relay", "version": VERSION, "waiting": len(BUF), "oldest_waiting_s": oldest,
            "target": TARGET, "secret_seen": bool(ST["good"] or BUF),
            "signature": {k: v for k, v in SIG.items() if k != "fail_run"},
            **{k: v for k, v in ST.items() if k != "good"}}


def _refused_address(r: httpx.Response) -> bool:
    """The dashboard itself said 'no such webhook address' (FastAPI's 404) — not Railway's edge or a restart."""
    if r.status_code != 404:
        return False
    try:
        return r.json() == {"detail": "Not Found"}
    except Exception:
        return False


def _move_back(addr: str):
    """Put every event of one address behind the others, each group keeping its own order."""
    keep = [x for x in BUF if x[1] != addr]
    mine = [x for x in BUF if x[1] == addr]
    BUF.clear()
    BUF.extend(keep)
    BUF.extend(mine)


async def _send(cli: httpx.AsyncClient, n: int) -> bool:
    """Hand the first waiting events (same address, at most n) to the dashboard; True when the queue moved on."""
    async with SEND_LOCK:
        if not BUF:
            return True
        addr = BUF[0][1]
        k = 0
        while k < min(n, len(BUF)) and BUF[k][1] == addr:
            k += 1
        chunk = [BUF[i][2] for i in range(k)]
        r = await cli.post(f"{TARGET}/mt/{addr}/batch", json=chunk)
        if r.status_code == 200:
            for _ in range(k):
                BUF.popleft()
            ST["forwarded"] += k
            ST["good"] = addr
            REFUSED.clear()
            ST["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
            ST["fail_streak"], ST["last_error"] = 0, ""
            return True
        if _refused_address(r):
            if ST["good"] and addr != ST["good"]:
                # a stray call to an address the dashboard does not know — MotionTools' own events use ST["good"]
                for _ in range(k):
                    BUF.popleft()
                ST["wrong_address_dropped"] += k
                return True
            if any(BUF[i][1] != addr for i in range(k, len(BUF))):
                # no address proven yet: let the other address go first so this one cannot hold the queue up
                _move_back(addr)
                if addr not in REFUSED:
                    REFUSED.add(addr)
                    return True
        ST["last_error"] = f"dashboard answered {r.status_code}" + (
            " — the secret in the MotionTools URL does not match WEBHOOK_PATH_SECRET of the dashboard" if r.status_code == 404 else "")
        return False


async def forwarder():
    backoff = 1.0
    async with httpx.AsyncClient(timeout=60) as cli:
        while not STOP["now"]:
            if not BUF:
                WAKE.clear()
                try:
                    await asyncio.wait_for(WAKE.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if await _send(cli, BATCH):
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
        while BUF and time.monotonic() - t0 < 20:
            try:
                if not await _send(cli, BATCH):
                    await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)
