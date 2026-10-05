"""Intercom messaging for the Quickzi ops dashboard — message riders, broadcast, receive their replies.

Mount in app.py (after `app = FastAPI()` and `require_login` are defined — e.g. just above the `/health` route):

    from intercom_msg import make_router
    app.include_router(make_router(require_login, DATA_DIR, PATH_SECRET, lambda rid: STATE["riders"].get(rid) or {}))

Railway variables:
    INTERCOM_TOKEN      Access token of your Intercom app (Developer Hub → your app → Authentication)
    INTERCOM_ADMIN_ID   optional — the teammate (admin) id messages are sent as; without it the token's own teammate
                        (GET /me) sends, or the first teammate of the workspace
    INTERCOM_REGION     us | eu | au   (optional — detected automatically; set it only to pin a region)

Intercom webhook (Developer Hub → your app → Webhooks), so rider replies show up in the dashboard:
    URL     https://<railway-url>/intercom/<WEBHOOK_PATH_SECRET>
    Topics  conversation.user.replied, conversation.user.created

Riders are Intercom *users* with external_id "rider:<MotionTools id>" (found by that or by phone, created if missing).
Messages go out as in-app messages; the rider reads and answers them in the Intercom messenger of the rider app.
Threads are cached in DATA_DIR/intercom_threads.json so a restart keeps the conversation history.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

HOSTS = {"us": "https://api.intercom.io", "eu": "https://api.eu.intercom.io", "au": "https://api.au.intercom.io"}
API_VERSION = "2.11"
MAX_PER_THREAD = 200


def _text(h: str) -> str:
    h = re.sub(r"<br\s*/?>|</p>\s*<p>", "\n", h or "", flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def _html(t: str) -> str:
    return "".join(f"<p>{html.escape(line)}</p>" for line in (t or "").split("\n"))


class Intercom:
    def __init__(self, data_dir: Path):
        self.token = os.environ.get("INTERCOM_TOKEN", "").strip().strip('"').strip("'")
        self.admin = os.environ.get("INTERCOM_ADMIN_ID", "").strip()
        self.region_fixed = bool(os.environ.get("INTERCOM_REGION", "").strip())
        self.region = (os.environ.get("INTERCOM_REGION", "us").strip().lower() or "us")
        self.base = HOSTS.get(self.region, HOSTS["us"])
        self.path = Path(data_dir) / "intercom_threads.json"
        self.lock = threading.Lock()
        self.threads: dict = self._load()
        self.status = {"checked": 0, "ok": False, "admin_name": "", "error": ""}

    @property
    def enabled(self) -> bool:
        return bool(self.token)          # the admin id is optional: without it the token's own teammate sends

    async def resolve_admin(self) -> str:
        """INTERCOM_ADMIN_ID not set: messages go out as the teammate who owns the token (GET /me); if that gives no
        admin, the first teammate of the workspace (GET /admins)."""
        if self.admin:
            return self.admin
        try:
            me = await self.call("GET", "/me")
            if me.get("type") == "admin" and me.get("id"):
                self.admin = str(me["id"])
                self.status["admin_name"] = me.get("name") or ""
        except HTTPException:
            pass
        if not self.admin:
            admins = (await self.call("GET", "/admins")).get("admins") or []
            if admins:
                self.admin = str(admins[0]["id"])
                self.status["admin_name"] = admins[0].get("name") or ""
        return self.admin

    # ------------------------------------------------------------------ storage
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.threads, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def thread(self, rider_id: str, name: str = "", phone: str = "") -> dict:
        t = self.threads.setdefault(rider_id, {"rider_id": rider_id, "name": name, "phone": phone, "contact_id": "",
                                               "conversation_id": "", "messages": [], "unread": 0})
        if name:
            t["name"] = name
        if phone:
            t["phone"] = phone
        return t

    def _push(self, t: dict, msg: dict):
        t["messages"].append(msg)
        del t["messages"][:-MAX_PER_THREAD]

    # ------------------------------------------------------------------ Intercom API
    async def call(self, method: str, path: str, body: dict = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                   "Content-Type": "application/json", "Intercom-Version": API_VERSION}
        try:
            async with httpx.AsyncClient(base_url=self.base, timeout=15, headers=headers) as c:
                r = await c.request(method, path, json=body)
        except httpx.HTTPError as e:                      # DNS, proxy, timeout — never a crash, always a readable status
            raise HTTPException(502, f"Intercom unreachable ({self.base}): {str(e)[:120]}")
        if r.status_code >= 400:
            try:
                errs = r.json().get("errors") or []
                detail = "; ".join(e.get("message", "") for e in errs) or r.text[:300]
            except Exception:
                detail = r.text[:300]
            raise HTTPException(502, f"Intercom {r.status_code}: {detail}")
        return r.json() if r.content else {}

    async def check(self, force: bool = False) -> dict:
        if not self.enabled:
            self.status.update(ok=False, error="INTERCOM_TOKEN not set")
            return self.status
        if not force and time.time() - self.status["checked"] < 600:
            return self.status
        try:
            me = await self._me_any_region()
            admin = await self.resolve_admin()
            self.status.update(ok=bool(admin), admin_name=self.status.get("admin_name") or me.get("name") or "",
                               error="" if admin else "no teammate found to send as — set INTERCOM_ADMIN_ID")
        except HTTPException as e:
            self.status.update(ok=False, error=str(e.detail))
        self.status.update(checked=time.time(), region=self.region, host=self.base, token_len=len(self.token), token_hint=self.token[:4] + "…" if self.token else "")
        return self.status

    async def _me_any_region(self) -> dict:
        """GET /me on the configured region; on 401 try the other regions (a token only works on its workspace's
        data-hosting region) and keep the one that answers, unless INTERCOM_REGION pins it."""
        try:
            return await self.call("GET", "/me")
        except HTTPException as e:
            if self.region_fixed or "401" not in str(e.detail):
                raise
            first = e
        for reg, host in HOSTS.items():
            if host == self.base:
                continue
            self.base = host
            try:
                me = await self.call("GET", "/me")
                self.region = reg
                return me
            except HTTPException:
                continue
        self.base = HOSTS.get(self.region, HOSTS["us"])
        raise HTTPException(502, f"{first.detail} (tried us, eu and au — the token is not valid on any Intercom region: copy the Access token again from Configure → Authentication)")

    async def contact_for(self, rider_id: str, name: str, phone: str) -> str:
        ext = f"rider:{rider_id}"
        cond = [{"field": "external_id", "operator": "=", "value": ext}]
        if phone:
            cond.append({"field": "phone", "operator": "=", "value": phone})
        res = await self.call("POST", "/contacts/search", {"query": {"operator": "OR", "value": cond}})
        if res.get("data"):
            return res["data"][0]["id"]
        body = {"role": "user", "external_id": ext, "name": name or "Rider"}
        if phone:
            body["phone"] = phone
        return (await self.call("POST", "/contacts", body))["id"]

    async def send(self, rider_id: str, name: str, phone: str, text: str, order_ref: str = "") -> dict:
        t = self.thread(rider_id, name, phone)
        msg = {"id": f"ops-{time.time_ns()}", "from": "ops", "body": text, "at": int(time.time()), "status": "sending", "order_ref": order_ref}
        self._push(t, msg)
        try:
            if not await self.resolve_admin():
                raise HTTPException(503, "no teammate to send as — set INTERCOM_ADMIN_ID in Railway")
            if not t.get("contact_id"):
                t["contact_id"] = await self.contact_for(rider_id, name, phone)
            if t.get("conversation_id"):
                await self.call("POST", f"/conversations/{t['conversation_id']}/reply",
                                {"message_type": "comment", "type": "admin", "admin_id": self.admin, "body": _html(text)})
            else:
                r = await self.call("POST", "/messages", {"message_type": "inapp", "body": _html(text),
                                                         "from": {"type": "admin", "id": self.admin},
                                                         "to": {"type": "user", "id": t["contact_id"]},
                                                         "create_conversation_without_contact_reply": True})
                t["conversation_id"] = str(r.get("conversation_id") or "")
            msg["status"] = "sent"
        except HTTPException as e:
            msg["status"], msg["error"] = "failed", str(e.detail)
        except Exception as e:  # network etc.
            msg["status"], msg["error"] = "failed", str(e)[:200]
        self._save()
        return msg

    def incoming(self, payload: dict) -> bool:
        """Intercom webhook: a rider replied (or started a conversation)."""
        topic = payload.get("topic") or ""
        if topic not in ("conversation.user.replied", "conversation.user.created"):
            return False
        item = (payload.get("data") or {}).get("item") or {}
        conv_id = str(item.get("id") or "")
        contacts = ((item.get("contacts") or {}).get("contacts")) or []
        contact = contacts[0] if contacts else {}
        cid, ext = contact.get("id") or "", contact.get("external_id") or ""
        parts = ((item.get("conversation_parts") or {}).get("conversation_parts")) or []
        user_parts = [p for p in parts if (p.get("author") or {}).get("type") in ("user", "lead", "contact")]
        if user_parts:
            body, author, at = user_parts[-1].get("body"), user_parts[-1].get("author") or {}, user_parts[-1].get("created_at")
        else:
            src = item.get("source") or {}
            body, author, at = src.get("body"), src.get("author") or {}, item.get("created_at")
        t = next((x for x in self.threads.values() if conv_id and x.get("conversation_id") == conv_id), None) \
            or next((x for x in self.threads.values() if cid and x.get("contact_id") == cid), None)
        if t is None:
            rid = ext[6:] if ext.startswith("rider:") else f"contact:{cid}"
            t = self.thread(rid, author.get("name") or contact.get("name") or "Rider")
            t["contact_id"] = cid
        if conv_id:
            t["conversation_id"] = conv_id
        self._push(t, {"id": f"in-{time.time_ns()}", "from": "rider", "body": _text(body or ""), "at": int(at or time.time()), "status": "received"})
        t["unread"] = int(t.get("unread") or 0) + 1
        self._save()
        return True

    def summary(self) -> list:
        out = []
        for t in self.threads.values():
            last = t["messages"][-1] if t["messages"] else None
            out.append({"rider_id": t["rider_id"], "name": t.get("name") or "Rider", "phone": t.get("phone") or "",
                        "unread": t.get("unread") or 0, "last": last})
        out.sort(key=lambda x: -(x["last"] or {}).get("at", 0))
        return out


def make_router(require_login, data_dir, path_secret: str, rider_lookup=lambda rid: {}) -> APIRouter:
    ic = Intercom(data_dir)
    r = APIRouter()
    login = [Depends(require_login)]

    def who(body: dict) -> tuple:
        rid = str(body.get("rider_id") or "").strip()
        if not rid:
            raise HTTPException(400, "rider_id missing")
        known = rider_lookup(rid) or {}
        return rid, (body.get("name") or known.get("name") or ""), (body.get("phone") or known.get("phone") or "")

    @r.get("/api/intercom/status", dependencies=login)
    async def status(force: int = 0):
        s = await ic.check(bool(force))
        return {"enabled": ic.enabled, "ok": s["ok"], "admin_name": s["admin_name"], "error": s["error"], "region": ic.region, "host": ic.base,
                "token_len": len(ic.token), "token_hint": ic.token[:4] + "…" if ic.token else "", "admin_id": ic.admin,
                "unread": sum(int(t.get("unread") or 0) for t in ic.threads.values())}

    @r.get("/api/intercom/threads", dependencies=login)
    def threads():
        return {"threads": ic.summary()}

    @r.get("/api/intercom/thread/{rider_id}", dependencies=login)
    def thread(rider_id: str, read: int = 1):
        t = ic.threads.get(rider_id)
        if not t:
            return {"rider_id": rider_id, "messages": [], "unread": 0}
        if read and t.get("unread"):
            t["unread"] = 0
            ic._save()
        return t

    @r.post("/api/intercom/send", dependencies=login)
    async def send(request: Request):
        body = await request.json()
        text = str(body.get("body") or "").strip()[:2000]
        if not text:
            raise HTTPException(400, "empty message")
        if not ic.enabled:
            raise HTTPException(503, "Intercom is not configured (INTERCOM_TOKEN missing in Railway)")
        rid, name, phone = who(body)
        return await ic.send(rid, name, phone, text, str(body.get("order_ref") or ""))

    @r.post("/api/intercom/broadcast", dependencies=login)
    async def broadcast(request: Request):
        body = await request.json()
        text = str(body.get("body") or "").strip()[:2000]
        riders = body.get("riders") or []
        if not text or not riders:
            raise HTTPException(400, "message and riders required")
        if not ic.enabled:
            raise HTTPException(503, "Intercom is not configured (INTERCOM_TOKEN missing in Railway)")
        results = []
        for x in riders[:200]:
            rid, name, phone = who(x)
            m = await ic.send(rid, name, phone, text)
            results.append({"rider_id": rid, "status": m["status"], "error": m.get("error", "")})
        return {"sent": sum(1 for x in results if x["status"] == "sent"), "failed": sum(1 for x in results if x["status"] == "failed"), "results": results}

    @r.post("/intercom/{secret}")
    async def webhook(secret: str, request: Request):
        if not secrets.compare_digest(secret, path_secret):
            raise HTTPException(404)
        try:
            payload = await request.json()
        except Exception:
            return {"ok": True}
        return {"ok": True, "stored": ic.incoming(payload)}

    @r.head("/intercom/{secret}")
    def webhook_head(secret: str):
        return {}

    r.ic = ic            # the app's automation rules send through the same client
    return r
