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
        self.hint_lookup = None          # rider id -> Intercom email / contact id typed in Settings
        self._link_attr = None           # resolved lazily: the "Worker dashboard profile link" attribute
        self.on_match = None             # (rider_id, contact) -> remember the Intercom username for the dashboard
        self.on_incoming = None          # (rider_id, text, conversation_id) -> the app answers common questions
        self.auto_team_name = ""         # Intercom team inbox for automatic messages (Settings); "" = same chat as manual
        self.auto_close = False          # close automatic conversations after sending (Settings; off = stay open until a rider replies)
        self._auto_team = None           # resolved team id

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
        try:
            await self.link_attribute()
        except Exception:
            pass
        team = ""
        try:
            team = await self.auto_team()
        except Exception:
            pass
        self.status.update(checked=time.time(), region=self.region, host=self.base, token_len=len(self.token), token_hint=self.token[:4] + "…" if self.token else "",
                           link_attr=self._link_attr or "", auto_team=team, auto_team_name=self.auto_team_name, auto_close=self.auto_close)
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

    @staticmethod
    def phone_variants(phone: str) -> list:
        """+49 151 2000001 · 0049151… · 0151… all mean the same rider; Intercom matches phones exactly."""
        raw = (phone or "").strip()
        digits = re.sub(r"\D", "", raw)
        if not digits:
            return []
        if digits.startswith("00"):
            e164 = "+" + digits[2:]
        elif digits.startswith("0"):
            e164 = "+49" + digits[1:]
        else:
            e164 = "+" + digits
        out = [e164, raw, digits, "00" + e164[1:]]
        if e164.startswith("+49"):
            out.append("0" + e164[3:])
        return list(dict.fromkeys(v for v in out if v))

    async def contact_for(self, rider_id: str, name: str, phone: str, hint: str = "") -> str:
        """The rider's Intercom contact: (1) the email / contact id typed in Settings, (2) external_id rider:<id>,
        (3) the phone number in any format — and only if nothing matches, a new contact. A match found by phone or
        email gets the external_id so the next lookup is direct."""
        ext = f"rider:{rider_id}"
        hint = (hint or "").strip()
        found = None
        if hint:
            if "@" in hint:
                res = await self.call("POST", "/contacts/search", {"query": {"field": "email", "operator": "=", "value": hint.lower()}})
                found = (res.get("data") or [None])[0]
            else:
                try:
                    found = await self.call("GET", f"/contacts/{hint}")
                except HTTPException:
                    found = None
            if not found:
                raise HTTPException(404, f"Intercom contact '{hint}' not found — check the email / id in Settings → Rider phone numbers")
        if not found:
            found = await self.find_contact(rider_id, name, phone)
        if found:
            email = (found.get("email") or "").strip()
            self.thread(rider_id)["user"] = email.split("@")[0] if email else (found.get("name") or "")
            if self.on_match:
                try:
                    self.on_match(rider_id, found)
                except Exception:
                    pass
            if not found.get("external_id"):            # never touch an id the rider app already uses for its messenger
                try:
                    await self.call("PUT", f"/contacts/{found['id']}", {"external_id": ext})
                except HTTPException:
                    pass
            link = str((found.get("custom_attributes") or {}).get((self._link_attr or "").split(".", 1)[-1], "") or "")
            return found["id"], ("hint" if hint else "link" if (rider_id and rider_id in link) else "other")
        body = {"role": "user", "external_id": ext, "name": name or "Rider"}
        pv = self.phone_variants(phone)
        if pv:
            body["phone"] = pv[0]
        return (await self.call("POST", "/contacts", body))["id"], "created"

    def relink(self, rider_id: str):
        """The Intercom contact of a rider changed (Settings): forget the cached contact / conversation."""
        t = self.threads.get(rider_id)
        if t:
            t["contact_id"], t["conversation_id"] = "", ""
            self._save()

    async def link_attribute(self) -> str:
        """The contact attribute MotionTools fills with the driver's profile link ('Worker dashboard profile link',
        https://<tenant>.motiontools.io/drivers/<MotionTools id>): found once in the workspace's data attributes."""
        if self._link_attr is not None:
            return self._link_attr
        self._link_attr = ""
        try:
            res = await self.call("GET", "/data_attributes?model=contact&include_archived=false")
            for a in res.get("data") or []:
                nm = (a.get("name") or "").lower()
                if "motiontools" in nm or ("worker" in nm and "link" in nm) or ("profile" in nm and "link" in nm and "dashboard" in nm):
                    self._link_attr = a.get("full_name") or f"custom_attributes.{a.get('name')}"
                    break
        except HTTPException:
            pass
        return self._link_attr

    async def find_contact(self, rider_id: str, name: str, phone: str):
        """Existing contact for a rider: the MotionTools profile link attribute (…/drivers/<id>) — the contact the
        rider app's messenger belongs to —, then external_id, any phone format, then the exact name (one match)."""
        ext = f"rider:{rider_id}"
        attr = await self.link_attribute()
        if attr and rider_id:
            try:
                res = await self.call("POST", "/contacts/search", {"query": {"field": attr, "operator": "~", "value": rider_id}})
                rows = [c for c in (res.get("data") or []) if rider_id in str((c.get("custom_attributes") or {}).get(attr.split(".", 1)[-1], "") or "") or True]
                if rows:
                    rows.sort(key=lambda c: c.get("role") != "user")
                    return rows[0]
            except HTTPException:
                pass
        # the MotionTools rider app logs the rider into Intercom's messenger with MotionTools' own user id -> that contact
        # (external_id = the bare id) is the one that receives in-app messages; try it first
        cond = [{"field": "external_id", "operator": "=", "value": rider_id}, {"field": "external_id", "operator": "=", "value": ext}]
        for v in self.phone_variants(phone):
            cond.append({"field": "phone", "operator": "=", "value": v})
        res = await self.call("POST", "/contacts/search", {"query": {"operator": "OR", "value": cond}})
        rows = res.get("data") or []
        rows.sort(key=lambda c: (c.get("external_id") not in (rider_id, ext), not c.get("external_id"), c.get("role") != "user"))
        if rows:
            return rows[0]
        nm = " ".join((name or "").split())
        if len(nm) >= 3:
            res = await self.call("POST", "/contacts/search", {"query": {"field": "name", "operator": "~", "value": nm}})
            cands = [c for c in (res.get("data") or []) if " ".join((c.get("name") or "").split()).lower() == nm.lower()]
            if len(cands) == 1:
                return cands[0]
            # "Lena W." in MotionTools vs "Lena Wagner" in Intercom: first name + initial of the last name, one match only
            parts = nm.split()
            if len(parts) >= 2 and parts[-1].endswith("."):
                first, ini = parts[0].lower(), parts[-1][0].lower()
                cands = [c for c in (res.get("data") or []) if (c.get("name") or "").lower().startswith(first + " ") and (c.get("name") or "").lower().split()[-1][:1] == ini]
                if len(cands) == 1:
                    return cands[0]
        return None

    async def latest_conversation(self, contact_id: str) -> str:
        """The rider's most recent Intercom conversation (open first), so our message lands in the chat he already
        has with the team instead of opening a new one."""
        try:
            res = await self.call("POST", "/conversations/search", {"query": {"field": "contact_ids", "operator": "=", "value": contact_id},
                                                                    "pagination": {"per_page": 10}})
        except HTTPException:
            return ""
        convs = res.get("conversations") or []
        if not convs:
            return ""
        convs.sort(key=lambda c: (c.get("state") != "open", -(c.get("updated_at") or 0)))
        return str(convs[0].get("id") or "")

    async def auto_team(self) -> str:
        """Team inbox for the automatic messages: found by name once (GET /teams); empty = not used."""
        name = (self.auto_team_name or "").strip().lower()
        if not name:
            self._auto_team = None
            return ""
        if self._auto_team and self._auto_team[0] == name:
            return self._auto_team[1]
        try:
            teams = (await self.call("GET", "/teams")).get("teams") or []
        except HTTPException:
            return ""
        tid = next((str(t["id"]) for t in teams if (t.get("name") or "").strip().lower() == name), "")
        self._auto_team = (name, tid)
        return tid

    async def send(self, rider_id: str, name: str, phone: str, text: str, order_ref: str = "", auto: bool = False) -> dict:
        """auto=True (rules): the message goes into the rider's *automation* conversation — one per rider and day,
        assigned to the automation team inbox and closed right away, so it stays out of the main inbox until the
        rider answers (his answer re-opens it in that inbox). Manual messages use the rider's normal chat."""
        if auto and (self.auto_team_name or "").strip():
            return await self._send_auto(rider_id, name, phone, text, order_ref)
        t = self.thread(rider_id, name, phone)
        hint = self.hint_lookup(rider_id) if self.hint_lookup else ""
        if hint != (t.get("hint") or ""):
            t["contact_id"], t["conversation_id"], t["hint"] = "", "", hint
        msg = {"id": f"ops-{time.time_ns()}", "from": "ops", "body": text, "at": int(time.time()), "status": "sending", "order_ref": order_ref}
        self._push(t, msg)
        try:
            if not await self.resolve_admin():
                raise HTTPException(503, "no teammate to send as — set INTERCOM_ADMIN_ID in Railway")
            # a contact cached from an older build may be a duplicate: re-check until it is the profile-link or
            # email-verified one (one search per message until then — cheap)
            if not t.get("contact_id") or t.get("contact_src") not in ("link", "hint"):
                cid, src = await self.contact_for(rider_id, name, phone, hint)
                if cid != t.get("contact_id"):
                    t["conversation_id"] = ""
                t["contact_id"], t["contact_src"] = cid, src
            if not t.get("conversation_id"):
                t["conversation_id"] = await self.latest_conversation(t["contact_id"])   # continue the rider's existing chat
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

    async def close(self, conv: str) -> bool:
        """Close a conversation after an automatic message (only when switched on in Settings) — and never over
        a rider's unanswered message: if the last thing in the conversation came from the rider, it stays open."""
        if not self.auto_close:
            return False
        try:
            c = await self.call("GET", f"/conversations/{conv}")
            parts = ((c.get("conversation_parts") or {}).get("conversation_parts")) or []
            said = [p for p in parts if p.get("part_type") in ("comment", "open", "assignment", "close", "note")]
            last = next((p for p in reversed(said) if p.get("part_type") == "comment"), None)
            author = ((last or {}).get("author") or {}).get("type") or ((c.get("source") or {}).get("author") or {}).get("type")
            if author in ("user", "lead", "contact"):
                return False                                   # the rider is waiting for an answer — leave it open
            await self.call("POST", f"/conversations/{conv}/parts", {"message_type": "close", "type": "admin", "admin_id": self.admin})
            return True
        except Exception:
            return False

    async def assignees(self, conv: str) -> dict:
        c = await self.call("GET", f"/conversations/{conv}")
        return {"admin": str(c.get("admin_assignee_id") or ""), "team": str(c.get("team_assignee_id") or ""), "state": c.get("state") or ""}

    async def route_to_team(self, conv: str, team: str) -> str:
        """Put a conversation into the automation team inbox only. Intercom keeps the sending teammate as
        assignee, so we assign the team, read the conversation back, drop the teammate if still set and
        re-assign the team if the unassign cleared it. Returns a short human-readable result."""
        part = lambda typ, who: self.call("POST", f"/conversations/{conv}/parts",
                                          {"message_type": "assignment", "type": typ, "admin_id": self.admin, "assignee_id": who})
        try:
            st = await self.assignees(conv)
            for _ in range(3):
                if st["team"] != str(team):
                    await part("team", str(team)); st = await self.assignees(conv)
                if st["admin"]:
                    await part("admin", "0"); st = await self.assignees(conv)
                if st["team"] == str(team) and not st["admin"]:
                    break
            if st["team"] == str(team) and not st["admin"]:
                return "automation inbox only"
            if st["team"] == str(team):
                return "automation inbox, but Intercom keeps the teammate assigned — turn off automatic assignment in the Automation team inbox settings"
            return f"NOT in automation inbox (team={st['team'] or '-'}, teammate={st['admin'] or '-'})"
        except HTTPException as e:
            return f"could not route: {e.detail}"[:160]
        except Exception as e:
            return f"could not route: {e}"[:160]

    async def _send_auto(self, rider_id: str, name: str, phone: str, text: str, order_ref: str) -> dict:
        t = self.thread(rider_id, name, phone)
        hint = self.hint_lookup(rider_id) if self.hint_lookup else ""
        msg = {"id": f"ops-{time.time_ns()}", "from": "ops", "body": text, "at": int(time.time()), "status": "sending", "order_ref": order_ref, "auto": True}
        self._push(t, msg)
        today = time.strftime("%Y-%m-%d")
        try:
            if not t.get("contact_id") or t.get("contact_src") not in ("link", "hint"):
                cid, src = await self.contact_for(rider_id, name, phone, hint)
                if cid != t.get("contact_id"):
                    t["conversation_id"] = ""
                t["contact_id"], t["contact_src"] = cid, src
            if not await self.resolve_admin():
                raise HTTPException(503, "no teammate to send as — set INTERCOM_ADMIN_ID in Railway")
            team = await self.auto_team()
            conv = t.get("auto_conversation_id") if t.get("auto_day") == today else ""
            if conv:
                await self.call("POST", f"/conversations/{conv}/reply", {"message_type": "comment", "type": "admin", "admin_id": self.admin, "body": _html(text)})
                if team:   # a reply re-assigns the conversation to the replying teammate — push it back to the team every time
                    msg["routing"] = await self.route_to_team(conv, team)
                await self.close(conv)
            else:
                r = await self.call("POST", "/messages", {"message_type": "inapp", "body": _html(text), "from": {"type": "admin", "id": self.admin},
                                                         "to": {"type": "user", "id": t["contact_id"]}, "create_conversation_without_contact_reply": True})
                conv = str(r.get("conversation_id") or "")
                t["auto_conversation_id"], t["auto_day"] = conv, today
                if conv and team:
                    msg["routing"] = await self.route_to_team(conv, team)
                if conv:
                    await self.close(conv)           # closed = nothing to read; a rider reply re-opens it in the same inbox
            msg["status"] = "sent"                         # stays open in the automation inbox so it is visible there
        except HTTPException as e:
            msg["status"], msg["error"] = "failed", str(e.detail)
        except Exception as e:
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
            has_photo = bool(user_parts[-1].get("attachments"))
        else:
            src = item.get("source") or {}
            body, author, at = src.get("body"), src.get("author") or {}, item.get("created_at")
            has_photo = bool(src.get("attachments"))
        if not has_photo and "<img" in (body or ""):
            has_photo = True
        t = next((x for x in self.threads.values() if conv_id and conv_id in (x.get("conversation_id"), x.get("auto_conversation_id"))), None) \
            or next((x for x in self.threads.values() if cid and x.get("contact_id") == cid), None)
        if t is None:
            rid = ext[6:] if ext.startswith("rider:") else f"contact:{cid}"
            t = self.thread(rid, author.get("name") or contact.get("name") or "Rider")
            t["contact_id"] = cid
        if conv_id:
            t["conversation_id"] = conv_id
        self._push(t, {"id": f"in-{time.time_ns()}", "from": "rider", "body": _text(body or "") or ("📷 photo" if has_photo else ""), "at": int(at or time.time()), "status": "received"})
        t["unread"] = int(t.get("unread") or 0) + 1
        self._save()
        if self.on_incoming and not str(t["rider_id"]).startswith("contact:"):
            try:
                self.on_incoming(t["rider_id"], _text(body or ""), conv_id, conv_id == t.get("auto_conversation_id"), has_photo)
            except Exception:
                pass
        return True

    async def escalate(self, conversation_id: str, note: str, team_id: str = "") -> bool:
        """A rider's reply needs a human: hand the conversation to the main inbox (a team if given, otherwise the
        sending teammate's own inbox), re-open it and leave an internal note so the dispatcher sees why."""
        if not conversation_id or not self.enabled:
            return False
        try:
            if not await self.resolve_admin():
                return False
            if team_id:
                await self.call("POST", f"/conversations/{conversation_id}/parts", {"message_type": "assignment", "type": "team", "admin_id": self.admin, "assignee_id": team_id})
            else:
                # "0" clears teammate AND team (leaves the automation inbox), then the sending teammate takes it
                await self.call("POST", f"/conversations/{conversation_id}/parts", {"message_type": "assignment", "type": "admin", "admin_id": self.admin, "assignee_id": "0"})
                await self.call("POST", f"/conversations/{conversation_id}/parts", {"message_type": "assignment", "type": "admin", "admin_id": self.admin, "assignee_id": self.admin})
            await self.call("POST", f"/conversations/{conversation_id}/parts", {"message_type": "open", "type": "admin", "admin_id": self.admin})
            if note:
                await self.call("POST", f"/conversations/{conversation_id}/reply", {"message_type": "note", "type": "admin", "admin_id": self.admin, "body": _html(note)})
            return True
        except HTTPException:
            return False

    async def reply_in(self, conversation_id: str, rider_id: str, name: str, text: str, close: bool = False) -> dict:
        """Answer inside the conversation the rider just wrote in (close=True: the answer settles it)."""
        t = self.thread(rider_id, name)
        msg = {"id": f"ops-{time.time_ns()}", "from": "ops", "body": text, "at": int(time.time()), "status": "sending", "auto": True}
        self._push(t, msg)
        try:
            if not await self.resolve_admin():
                raise HTTPException(503, "no teammate to send as")
            await self.call("POST", f"/conversations/{conversation_id}/reply", {"message_type": "comment", "type": "admin", "admin_id": self.admin, "body": _html(text)})
            msg["status"] = "sent"
            if close:
                await self.close(conversation_id)
        except HTTPException as e:
            msg["status"], msg["error"] = "failed", str(e.detail)
        except Exception as e:
            msg["status"], msg["error"] = "failed", str(e)[:200]
        self._save()
        return msg

    def summary(self) -> list:
        out = []
        for t in self.threads.values():
            last = t["messages"][-1] if t["messages"] else None
            out.append({"rider_id": t["rider_id"], "name": t.get("name") or "Rider", "phone": t.get("phone") or "", "user": t.get("user") or "",
                        "unread": t.get("unread") or 0, "last": last})
        out.sort(key=lambda x: -(x["last"] or {}).get("at", 0))
        return out


def make_router(require_login, data_dir, path_secret: str, rider_lookup=lambda rid: {}) -> APIRouter:
    ic = Intercom(data_dir)
    ic.hint_lookup = lambda rid: str((rider_lookup(rid) or {}).get("intercom") or "")
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

    @r.post("/api/intercom/match-all", dependencies=login)
    async def match_all(request: Request):
        """Settings button: look every known rider up in Intercom once and remember the username (the part before @)."""
        body = await request.json()
        riders = body.get("riders") or []
        out = {"matched": 0, "missing": [], "errors": 0}
        for x in riders[:1000]:
            rid = str(x.get("rider_id") or "")
            if not rid:
                continue
            try:
                c = await ic.find_contact(rid, x.get("name") or "", x.get("phone") or "")
            except HTTPException:
                out["errors"] += 1
                continue
            if c:
                out["matched"] += 1
                if ic.on_match:
                    ic.on_match(rid, c)
            else:
                out["missing"].append(x.get("name") or rid)
        return out

    @r.get("/api/intercom/match/{rider_id}", dependencies=login)
    async def match(rider_id: str):
        """Settings: which Intercom contact a rider's messages would go to (without sending anything)."""
        if not ic.enabled:
            return {"ok": False, "error": "Intercom not configured"}
        known = rider_lookup(rider_id) or {}
        hint = str(known.get("intercom") or "")
        try:
            if hint:
                c = (await ic.call("POST", "/contacts/search", {"query": {"field": "email", "operator": "=", "value": hint.lower()}})).get("data") if "@" in hint else [await ic.call("GET", f"/contacts/{hint}")]
                c = (c or [None])[0]
                how = "email typed in Settings"
            else:
                c = await ic.find_contact(rider_id, known.get("name") or "", known.get("phone") or "")
                how = "phone / name"
        except HTTPException as e:
            return {"ok": False, "error": str(e.detail)}
        if c and ic.on_match:
            ic.on_match(rider_id, c)
        if c:
            t = ic.thread(rider_id, known.get("name") or "", known.get("phone") or "")
            if t.get("contact_id") != c.get("id"):
                t["contact_id"], t["conversation_id"] = c.get("id"), ""
            link = str((c.get("custom_attributes") or {}).get((ic._link_attr or "").split(".", 1)[-1], "") or "")
            t["contact_src"] = "hint" if hint else ("link" if rider_id in link else "other")
            ic._save()
        if not c:
            return {"ok": True, "found": False, "phone": known.get("phone") or "", "message": "no existing contact matches this rider's MotionTools link, phone or name — a message would create a new one; type the contact's email in Settings"}
        link = str((c.get("custom_attributes") or {}).get((ic._link_attr or "").split(".", 1)[-1], "") or "")
        if link and rider_id in link:
            how = "MotionTools profile link"
        return {"ok": True, "found": True, "how": how, "contact": {"id": c.get("id"), "name": c.get("name"), "email": c.get("email"), "phone": c.get("phone"), "role": c.get("role"), "external_id": c.get("external_id"), "link": link}}

    @r.delete("/api/intercom/thread/{rider_id}", dependencies=login)
    def thread_relink(rider_id: str):
        ic.relink(rider_id)
        return {"ok": True}

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
