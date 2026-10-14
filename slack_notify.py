"""Slack messages for orders of PRIORITY RESTAURANTS (Settings → "Slack — priority restaurants").

  * Where: one Slack channel of your own team (Slack "Incoming Webhook"). The webhook URL is a secret and lives only
    in Railway as SLACK_WEBHOOK_URL — never in the dashboard, never on GitHub. Never point it at a channel shared with
    MotionTools: the dashboard never contacts MotionTools.
  * Which orders: a restaurant name containing one of the names in the list (case and ®/™ ignored),
    e.g. "REWE" matches "REWE Markt Göttingen".
  * What: 🆕 new order · ⚠️ no rider after N minutes · ❌ cancelled — each once per order. No customer data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("slack")
UTC = timezone.utc
WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "").strip()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[®™©.,'’`´\"()\-–_/]", " ", (s or "").lower())).strip()


class SlackNotifier:
    def __init__(self, store, mt_link, city_of, hm):
        self.store, self.mt_link, self.city_of, self.hm = store, mt_link, city_of, hm
        self.posted: dict = {}              # order id -> {"new": t, "norider": t, "cancel": t}
        self.stats = {"day": "", "posted": 0, "failed": 0, "last_error": "", "last_post": None}
        self._last_send = 0.0
        self._dirty = False
        try:
            self.posted = json.loads(store.get_settings().get("slack_posted") or "{}")
        except Exception:
            self.posted = {}

    # ---- settings
    def cfg(self) -> dict:
        s = self.store.get_settings()
        names = [n.strip() for n in (s.get("slack_restaurants") or "").splitlines() if n.strip()]
        try:
            norider = max(1, int(s.get("slack_no_rider_min") or 5))
        except ValueError:
            norider = 5
        events = set((s.get("slack_events") or "new,norider,cancel").split(","))
        return {"names": names, "no_rider_min": norider, "events": events, "on": bool(WEBHOOK) and bool(names)}

    def match(self, restaurant: str, names: list) -> str:
        r = _norm(restaurant)
        if not r or r in ("restaurant", "?"):
            return ""
        for n in names:
            if _norm(n) and _norm(n) in r:
                return n
        return ""

    # ---- sending
    async def send(self, text: str) -> bool:
        if not WEBHOOK:
            return False
        wait = 1.1 - (time.time() - self._last_send)           # Slack: about one message per second per webhook
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send = time.time()
        today = datetime.now(UTC).date().isoformat()
        if self.stats["day"] != today:
            self.stats.update(day=today, posted=0, failed=0)
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(WEBHOOK, json={"text": text, "unfurl_links": False, "unfurl_media": False})
            if r.status_code == 429:
                await asyncio.sleep(min(30, float(r.headers.get("Retry-After", "2") or 2)))
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(WEBHOOK, json={"text": text, "unfurl_links": False, "unfurl_media": False})
            if r.status_code >= 300:
                raise RuntimeError(f"Slack {r.status_code}: {r.text[:120]}")
            self.stats["posted"] += 1
            self.stats["last_post"] = datetime.now(UTC).isoformat(timespec="seconds")
            return True
        except Exception as e:
            self.stats["failed"] += 1
            self.stats["last_error"] = str(e)[:200]
            log.warning("slack post failed: %s", e)
            return False

    # ---- what to post
    def _line(self, o: dict) -> str:
        link = self.mt_link(o)
        city = self.city_of(o) or ""
        return f"*{o.get('restaurant')}* · `{o.get('ref')}`" + (f" · {city}" if city else "") + (f"  <{link}|MotionTools ↗>" if link else "")

    async def tick(self, orders: list, now: datetime):
        c = self.cfg()
        if not c["on"]:
            return
        for o in orders:
            name = self.match(o.get("restaurant") or "", c["names"])
            if not name:
                continue
            oid = o.get("id")
            p = self.posted.setdefault(oid, {})
            seen = o.get("created_at") or o.get("dispatched_at")
            fresh = seen is not None and now - seen < timedelta(minutes=30)
            ph = o.get("phase")
            if "new" in c["events"] and not p.get("new") and fresh and ph not in ("cancelled", "delivered", "closed"):
                plan = self.hm(o.get("promised_at") or o.get("eta_customer") or o.get("scheduled_at"))
                sched = o.get("scheduled_at")
                pre = sched is not None and sched - now > timedelta(minutes=45)
                text = (f"🆕 New order — {self._line(o)}\n"
                        + (f"Pre-order, scheduled {self.hm(sched)}" if pre else f"Planned delivery {plan}" if plan else "ASAP")
                        + (f" · rider {o.get('rider')}" if o.get("rider") else " · waiting for a rider"))
                if await self.send(text):
                    p["new"] = time.time(); self._dirty = True
            if "norider" in c["events"] and not p.get("norider") and ph == "unassigned" and o.get("dispatched_at"):
                waited = (now - o["dispatched_at"]).total_seconds() / 60
                if c["no_rider_min"] <= waited < 180:
                    if await self.send(f"⚠️ No rider for {int(waited)} min — {self._line(o)}"):
                        p["norider"] = time.time(); self._dirty = True
            if "cancel" in c["events"] and not p.get("cancel") and ph == "cancelled" and (p.get("new") or p.get("norider")):
                why = o.get("cancel_reason") or ""
                if await self.send(f"❌ Cancelled — {self._line(o)}" + (f"\nReason: {why}" if why else "")):
                    p["cancel"] = time.time(); self._dirty = True
        if self._dirty:
            if len(self.posted) > 1500:
                for k in sorted(self.posted, key=lambda k: max(self.posted[k].values() or [0]))[:len(self.posted) - 1000]:
                    self.posted.pop(k, None)
            self.store.set_settings({"slack_posted": json.dumps(self.posted)})
            self._dirty = False

    def status(self, orders: list) -> dict:
        c = self.cfg()
        seen: dict = {}
        for o in orders:
            n = self.match(o.get("restaurant") or "", c["names"])
            if n:
                seen.setdefault(o.get("restaurant"), 0)
                seen[o.get("restaurant")] += 1
        return {"webhook_set": bool(WEBHOOK), "names": c["names"], "no_rider_min": c["no_rider_min"], "events": sorted(c["events"]),
                "matching_now": sorted(({"restaurant": k, "orders": v} for k, v in seen.items()), key=lambda x: -x["orders"])[:30],
                **self.stats}
