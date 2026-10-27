"""Slack messages for orders of PRIORITY RESTAURANTS (Settings → "Slack — priority restaurants").

  * Where: one Slack channel of your own team (Slack "Incoming Webhook"). The webhook URL is a secret and lives only
    in Railway as SLACK_WEBHOOK_URL — never in the dashboard, never on GitHub. Never point it at a channel shared with
    MotionTools: the dashboard never contacts MotionTools.
  * Which orders: a restaurant name containing one of the names in the list (case and ®/™ ignored),
    e.g. "REWE" matches "REWE Markt Göttingen".
  * What: 🆕 new order · ⚠️ no rider after N minutes · 🕐 rider accepted but not at the restaurant N minutes later ·
    ❌ cancelled — each once per order. No customer data.
    Every message carries the timeline: Received (arrived in our system) · Dispatched (offered to riders) ·
    Pickup · Planned delivery, plus the rider (and when he accepted) and the distance when known.
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
        s = store.get_settings()                  # 9.1.5, once: the new "no rider at the restaurant" message starts switched on
        if s.get("slack_events") and s.get("mig_slack_rest") != "1" and "rest" not in s["slack_events"].split(","):
            store.set_settings({"slack_events": s["slack_events"] + ",rest", "mig_slack_rest": "1"})
        elif s.get("mig_slack_rest") != "1":
            store.set_settings({"mig_slack_rest": "1"})
        s = store.get_settings()                  # 9.5, once: the Monday waiting-time report starts switched on
        if s.get("slack_events") and s.get("mig_slack_weekly") != "1" and "weekly" not in s["slack_events"].split(","):
            store.set_settings({"slack_events": s["slack_events"] + ",weekly", "mig_slack_weekly": "1"})
        elif s.get("mig_slack_weekly") != "1":
            store.set_settings({"mig_slack_weekly": "1"})

    # ---- settings
    def cfg(self) -> dict:
        s = self.store.get_settings()
        names = [n.strip() for n in (s.get("slack_restaurants") or "").splitlines() if n.strip()]
        try:
            norider = max(1, int(s.get("slack_no_rider_min") or 5))
        except ValueError:
            norider = 5
        try:
            rest = max(1, int(s.get("slack_rest_min") or 10))
        except ValueError:
            rest = 10
        events = set((s.get("slack_events") or "new,norider,rest,cancel,weekly").split(","))
        return {"names": names, "no_rider_min": norider, "rest_min": rest, "events": events, "on": bool(WEBHOOK) and bool(names)}

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

    @staticmethod
    def _plan(o: dict):
        return o.get("promised_at") or o.get("eta_customer") or o.get("scheduled_at")

    def _pre_order(self, o: dict, now: datetime) -> bool:
        """Still on hold and delivery planned far ahead: MotionTools releases it to riders later (≈45 min before)."""
        plan = self._plan(o)
        return not o.get("dispatched_at") and plan is not None and plan - now > timedelta(minutes=45)

    def _times(self, o: dict, now: datetime, cancelled: bool = False) -> str:
        """Received (arrived in our system) · Dispatched (offered to riders) · Pickup · Delivery."""
        t = []
        if o.get("created_at"):
            t.append(f"Received {self.hm(o['created_at'])}")
        if o.get("dispatched_at"):
            t.append(f"Dispatched {self.hm(o['dispatched_at'])}")
        elif not cancelled:
            t.append("On hold (not dispatched yet)")
        if o.get("picked_up_at"):
            t.append(f"Picked up {self.hm(o['picked_up_at'])}")
        elif o.get("eta_restaurant") and not cancelled:              # on hold: when the food is planned to be ready
            t.append(f"Pickup ~{self.hm(o['eta_restaurant'])}")
        plan = self._plan(o)
        if plan:
            left = int((plan - now).total_seconds() // 60)
            t.append(f"Planned delivery {self.hm(plan)}" + (f" (in {left} min)" if not cancelled and 0 <= left <= 90 else
                                                             f" ({-left} min ago)" if not cancelled and left < 0 else ""))
        return " · ".join(t)

    def _rider(self, o: dict) -> str:
        if not o.get("rider"):
            return "Waiting for a rider"
        return f"Rider {o['rider']}" + (f" (accepted {self.hm(o['accepted_at'])})" if o.get("accepted_at") else "")

    @staticmethod
    def _km(o: dict) -> str:
        m = o.get("est_distance_m")
        return f" · {m / 1000:.1f} km" if isinstance(m, (int, float)) and m > 0 else ""

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
                pre = self._pre_order(o, now)
                # post as soon as the order arrives — only wait for MotionTools' first ETAs (seconds after creation)
                # so pickup / planned delivery are in the message; never longer than 1 minute. An order still
                # "On hold" (food preparation, pre-order) says so; ⚠️ later carries its dispatch time.
                has_eta = self._plan(o) or o.get("eta_restaurant")
                ready = has_eta or o.get("dispatched_at") or (o.get("created_at") and now - o["created_at"] >= timedelta(minutes=1))
                if ready:
                    text = (f"🆕 {'Pre-order' if pre else 'New order'} — {self._line(o)}\n"
                            f"{self._times(o, now)}\n{self._rider(o)}{self._km(o)}")
                    if await self.send(text):
                        p["new"] = time.time(); self._dirty = True
            if "norider" in c["events"] and not p.get("norider") and ph == "unassigned" and o.get("dispatched_at"):
                waited = (now - o["dispatched_at"]).total_seconds() / 60
                if c["no_rider_min"] <= waited < 180:
                    if await self.send(f"⚠️ No rider for {int(waited)} min — {self._line(o)}\n{self._times(o, now)}"):
                        p["norider"] = time.time(); self._dirty = True
            # a rider ACCEPTED the order but is still not at the restaurant N min later (once per rider: a new rider who
            # takes the order over gets his own N minutes)
            rkey = f"rest:{o.get('rider_id')}"
            if ("rest" in c["events"] and o.get("rider_id") and o.get("accepted_at") and not p.get(rkey)
                    and not o.get("at_restaurant_at") and not o.get("picked_up_at") and ph in ("accepted", "to_restaurant")):
                waited = (now - o["accepted_at"]).total_seconds() / 60
                if c["rest_min"] <= waited < 180:
                    eta = f" · his ETA at the restaurant {self.hm(o['eta_restaurant'])}" if o.get("eta_restaurant") else ""
                    if await self.send(f"🕐 Not at the restaurant {int(waited)} min after accepting — {self._line(o)}\n"
                                       f"Rider {o.get('rider') or '?'} accepted {self.hm(o['accepted_at'])}{eta}\n{self._times(o, now)}"):
                        p[rkey] = time.time(); self._dirty = True
            if "cancel" in c["events"] and not p.get("cancel") and ph == "cancelled" and any(p.get(k) for k in p if k != "cancel"):
                why = o.get("cancel_reason") or ""
                text = (f"❌ Cancelled {self.hm(now)} — {self._line(o)}\n{self._times(o, now, cancelled=True)}"
                        + (f" · Rider {o['rider']}" if o.get("rider") else "") + (f"\nReason: {why}" if why else ""))
                if await self.send(text):
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
        return {"webhook_set": bool(WEBHOOK), "names": c["names"], "no_rider_min": c["no_rider_min"], "rest_min": c["rest_min"], "events": sorted(c["events"]),
                "matching_now": sorted(({"restaurant": k, "orders": v} for k, v in seen.items()), key=lambda x: -x["orders"])[:30],
                **self.stats}
