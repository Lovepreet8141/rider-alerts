"""SQLite storage + analytics for the Quickzi ops platform.

Tables
  orders          every order ever seen (live + delivered + cancelled) with all phase timestamps
  alerts          every alert raised, with resolution / handled / snooze
  riders          last known state per rider
  rider_sessions  online/offline sessions (for hours online, utilisation, staffing per hour)
  positions       GPS trail of riders while they hold an order (kept 7 days)
  daily_stats     frozen report per operating day (written at 04:05, or on demand)
  settings        editable thresholds
  syslog          what the system did / errors (for the System panel)

Operating day = 04:00 -> 04:00 Berlin, so orders after midnight belong to the evening before.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from statistics import mean, median

from orders import BERLIN, UTC, haversine_m, iso, mins, phase_minutes, ts

DAY_STARTS_AT = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, ref TEXT, area TEXT, rider_id TEXT, rider TEXT, restaurant TEXT, place_id TEXT,
  customer_addr TEXT, customer_zip TEXT, status TEXT, phase TEXT, stacked INTEGER DEFAULT 0,
  dispatched_at TEXT, accepted_at TEXT, started_at TEXT, at_restaurant_at TEXT, picked_up_at TEXT,
  at_customer_at TEXT, delivered_at TEXT, cancelled_at TEXT, cancel_reason TEXT, ptod_min REAL,
  closed INTEGER DEFAULT 0, first_seen TEXT, updated_at TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_open ON orders(closed);
CREATE INDEX IF NOT EXISTS ix_orders_disp ON orders(dispatched_at);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT, order_ref TEXT, rider_id TEXT, rider TEXT, kind TEXT, severity TEXT, headline TEXT, action TEXT,
  restaurant TEXT, phone TEXT, restaurant_phone TEXT, map_url TEXT,
  opened_at TEXT, updated_at TEXT, resolved_at TEXT, resolution TEXT, dismissed_at TEXT, snoozed_until TEXT
);
CREATE INDEX IF NOT EXISTS ix_alerts_open ON alerts(resolved_at);
CREATE INDEX IF NOT EXISTS ix_alerts_opened ON alerts(opened_at);
CREATE TABLE IF NOT EXISTS riders (
  id TEXT PRIMARY KEY, name TEXT, phone TEXT, online INTEGER, lat REAL, lng REAL, active_ids TEXT,
  online_since TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rider_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rider_id TEXT, online_at TEXT, offline_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_rider ON rider_sessions(rider_id, online_at);
CREATE TABLE IF NOT EXISTS positions (
  rider_id TEXT, at TEXT, lat REAL, lng REAL, order_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_positions ON positions(rider_id, at);
CREATE TABLE IF NOT EXISTS daily_stats (day TEXT PRIMARY KEY, computed_at TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS syslog (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, msg TEXT);
"""

DT_FIELDS = ("created_at", "dispatched_at", "accepted_at", "started_at", "at_restaurant_at", "picked_up_at",
             "at_customer_at", "delivered_at", "eta_restaurant", "eta_customer", "scheduled_at", "last_event_at", "eta_at")


def day_start(now: datetime) -> datetime:
    local = now.astimezone(BERLIN)
    day = local.replace(hour=DAY_STARTS_AT, minute=0, second=0, microsecond=0)
    if local.hour < DAY_STARTS_AT:
        day -= timedelta(days=1)
    return day.astimezone(UTC)


def period_range(period: str, now: datetime):
    """(start, end) in UTC for today / yesterday / week / month / a YYYY-MM-DD operating day."""
    if len(period) == 10 and period[4] == "-":
        local = datetime.strptime(period, "%Y-%m-%d").replace(hour=DAY_STARTS_AT, tzinfo=BERLIN)
        return local.astimezone(UTC), (local + timedelta(days=1)).astimezone(UTC)
    start = day_start(now)
    if period == "yesterday":
        return start - timedelta(days=1), start
    if period == "week":
        local = start.astimezone(BERLIN)
        start = (local - timedelta(days=local.weekday())).astimezone(UTC)
    elif period == "month":
        local = start.astimezone(BERLIN)
        start = local.replace(day=1).astimezone(UTC)
    return start, now + timedelta(days=1)


def day_key(dt: datetime) -> str:
    return day_start(dt).astimezone(BERLIN).strftime("%Y-%m-%d")


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(mean(vals), 1) if vals else None


def _pct(ok, n):
    return round(100 * ok / n) if n else None


def _count(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items(), key=lambda x: -x[1]))


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)").fetchall()}
            for col in ("reason", "note", "reason_at"):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
            self.db.commit()

    def _rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # ================================================================ system
    def log(self, level: str, msg: str):
        self._exec("INSERT INTO syslog (at, level, msg) VALUES (?,?,?)", (iso(datetime.now(UTC)), level, msg[:500]))
        self._exec("DELETE FROM syslog WHERE id < (SELECT MAX(id) FROM syslog) - 500")

    def syslog(self, n=40):
        return self._rows("SELECT * FROM syslog ORDER BY id DESC LIMIT ?", (n,))

    def get_settings(self) -> dict:
        return {r["key"]: r["value"] for r in self._rows("SELECT key, value FROM settings")}

    def set_settings(self, values: dict):
        for k, v in values.items():
            self._exec("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (k, str(v)))

    # ================================================================ orders
    def upsert_order(self, o: dict, now: datetime, stacked: bool, force: bool = False):
        """force=True (repair from events): the given timestamps/rider replace what is stored, instead of being merged."""
        pm = phase_minutes(o)
        closed = 1 if o["phase"] in ("delivered", "cancelled", "closed") else 0
        existing = self._rows("SELECT first_seen, stacked, cancelled_at FROM orders WHERE id=?", (o["id"],))
        first_seen = existing[0]["first_seen"] if existing else iso(now)
        stacked_flag = 1 if (stacked or (existing and existing[0]["stacked"])) else 0
        cancelled_at = None
        if o["phase"] == "cancelled":
            cancelled_at = (existing[0]["cancelled_at"] if existing and existing[0]["cancelled_at"] else iso(now))
        raw = json.dumps({k: (iso(v) if isinstance(v, datetime) else v) for k, v in o.items()})
        merge = ("rider_id=COALESCE(excluded.rider_id, orders.rider_id), rider=CASE WHEN excluded.rider!='' THEN excluded.rider ELSE orders.rider END, "
                 "accepted_at=COALESCE(excluded.accepted_at, orders.accepted_at), "
                 "started_at=COALESCE(excluded.started_at, orders.started_at), "
                 "at_restaurant_at=COALESCE(excluded.at_restaurant_at, orders.at_restaurant_at), "
                 "picked_up_at=COALESCE(excluded.picked_up_at, orders.picked_up_at), "
                 "at_customer_at=COALESCE(excluded.at_customer_at, orders.at_customer_at), "
                 "delivered_at=COALESCE(excluded.delivered_at, orders.delivered_at), ptod_min=COALESCE(excluded.ptod_min, orders.ptod_min), ")
        if force:
            merge = ("rider_id=excluded.rider_id, rider=excluded.rider, accepted_at=excluded.accepted_at, started_at=excluded.started_at, "
                     "at_restaurant_at=excluded.at_restaurant_at, picked_up_at=excluded.picked_up_at, at_customer_at=excluded.at_customer_at, "
                     "delivered_at=excluded.delivered_at, ptod_min=excluded.ptod_min, ")
        self._exec(
            "INSERT INTO orders (id, ref, area, rider_id, rider, restaurant, place_id, customer_addr, customer_zip, status, "
            "phase, stacked, dispatched_at, accepted_at, started_at, at_restaurant_at, picked_up_at, at_customer_at, "
            "delivered_at, cancelled_at, cancel_reason, ptod_min, closed, first_seen, updated_at, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET ref=excluded.ref, area=excluded.area, " + merge +
            "restaurant=excluded.restaurant, place_id=excluded.place_id, customer_addr=excluded.customer_addr, "
            "customer_zip=excluded.customer_zip, status=excluded.status, phase=excluded.phase, stacked=excluded.stacked, "
            "dispatched_at=excluded.dispatched_at, cancelled_at=excluded.cancelled_at, "
            "cancel_reason=excluded.cancel_reason, "
            "closed=excluded.closed, updated_at=excluded.updated_at, raw=excluded.raw",
            (o["id"], o["ref"], o["area"], o["rider_id"], o["rider"] or "", o["restaurant"], o.get("place_id", ""),
             o["customer_addr"], o.get("customer_zip", ""), o["status"], o["phase"], stacked_flag,
             iso(o["dispatched_at"]), iso(o["accepted_at"]), iso(o["started_at"]), iso(o["at_restaurant_at"]),
             iso(o["picked_up_at"]), iso(o["at_customer_at"]), iso(o["delivered_at"]), cancelled_at,
             o.get("cancel_reason", ""), pm["ptod"], closed, first_seen, iso(now), raw))

    def set_reason(self, oid: str, reason: str, note: str, now: datetime) -> bool:
        """The manager's explanation for a late / problematic order (shown in Orders, Insights, daily report)."""
        return self._exec("UPDATE orders SET reason=?, note=?, reason_at=? WHERE id=?",
                          (reason or "", note or "", iso(now) if (reason or note) else None, oid)).rowcount > 0

    def rename_place(self, place_id: str, name: str) -> int:
        """A restaurant got its name (Settings / API): apply it to every stored order and alert of that place."""
        n = 0
        for r in self._rows("SELECT id, raw FROM orders WHERE place_id=?", (place_id,)):
            try:
                raw = json.loads(r["raw"]) if r["raw"] else {}
            except Exception:
                raw = {}
            raw["restaurant"] = name
            self._exec("UPDATE orders SET restaurant=?, raw=? WHERE id=?", (name, json.dumps(raw), r["id"]))
            self._exec("UPDATE alerts SET restaurant=? WHERE order_id=?", (name, r["id"]))
            n += 1
        return n

    def _hydrate(self, r: dict) -> dict:
        o = json.loads(r["raw"]) if r.get("raw") else {}
        for k in DT_FIELDS:
            o[k] = ts(o.get(k))
        # DB columns win for timestamps we merged (COALESCE), so a later poll can't blank an earlier phase
        for k in ("accepted_at", "started_at", "at_restaurant_at", "picked_up_at", "at_customer_at", "delivered_at"):
            if r.get(k):
                o[k] = ts(r[k])
        o["rider"] = r.get("rider") or o.get("rider") or ""
        o["rider_id"] = r.get("rider_id") or o.get("rider_id")
        o["stacked"] = bool(r.get("stacked"))
        o["cancelled_at"] = ts(r.get("cancelled_at"))
        o["reason"], o["note"] = r.get("reason") or "", r.get("note") or ""
        o["phases"] = phase_minutes(o)
        d = o["dispatched_at"] or o["cancelled_at"]
        o["hour"] = d.astimezone(BERLIN).hour if d else None
        return o

    def open_orders(self) -> list:
        return [self._hydrate(r) for r in self._rows("SELECT * FROM orders WHERE closed=0")]

    def close_missing(self, keep_ids: set, now: datetime):
        for r in self._rows("SELECT id FROM orders WHERE closed=0"):
            if r["id"] not in keep_ids:
                self._exec("UPDATE orders SET closed=1, updated_at=? WHERE id=? AND closed=0", (iso(now), r["id"]))

    def orders_in(self, period: str, now: datetime, q: str = "") -> list:
        start, end = period_range(period, now)
        rows = self._rows("SELECT * FROM orders WHERE (dispatched_at >= ? AND dispatched_at < ?) OR closed=0 "
                          "OR (dispatched_at IS NULL AND first_seen >= ? AND first_seen < ?) "
                          "ORDER BY COALESCE(dispatched_at, first_seen) DESC", (iso(start), iso(end), iso(start), iso(end)))
        out = [self._hydrate(r) for r in rows]
        if q:
            ql = q.lower()
            out = [o for o in out if ql in (o["ref"] or "").lower() or ql in (o["rider"] or "").lower()
                   or ql in (o["restaurant"] or "").lower() or ql in (o["customer_addr"] or "").lower()]
        return out

    def order(self, oid: str):
        rows = self._rows("SELECT * FROM orders WHERE id=?", (oid,))
        return self._hydrate(rows[0]) if rows else None

    def delivered(self, period: str, now: datetime) -> list:
        return [o for o in self.orders_in(period, now) if o["phase"] == "delivered" and o["delivered_at"]]

    # ================================================================ alerts
    def open_alerts(self):
        return self._rows("SELECT * FROM alerts WHERE resolved_at IS NULL")

    def open_alert(self, a: dict, now: datetime) -> int:
        cur = self._exec(
            "INSERT INTO alerts (order_id, order_ref, rider_id, rider, kind, severity, headline, action, restaurant, "
            "phone, restaurant_phone, map_url, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["order_id"], a["order_ref"], a["rider_id"], a["rider"], a["kind"], a["severity"], a["headline"],
             a["action"], a["restaurant"], a["phone"], a["restaurant_phone"], a["map_url"], iso(now), iso(now)))
        return cur.lastrowid

    def update_alert(self, aid: int, a: dict, now: datetime):
        self._exec("UPDATE alerts SET severity=?, headline=?, action=?, phone=?, map_url=?, rider=?, rider_id=?, "
                   "restaurant=?, restaurant_phone=?, updated_at=? WHERE id=?",
                   (a["severity"], a["headline"], a["action"], a["phone"], a["map_url"], a["rider"], a["rider_id"],
                    a["restaurant"], a["restaurant_phone"], iso(now), aid))

    def resolve_alert(self, aid: int, why: str, now: datetime):
        self._exec("UPDATE alerts SET resolved_at=?, resolution=? WHERE id=? AND resolved_at IS NULL", (iso(now), why, aid))

    def dismiss_alert(self, aid: int, now: datetime) -> bool:
        return self._exec("UPDATE alerts SET dismissed_at=? WHERE id=?", (iso(now), aid)).rowcount > 0

    def snooze_alert(self, aid: int, until: datetime) -> bool:
        return self._exec("UPDATE alerts SET snoozed_until=? WHERE id=?", (iso(until), aid)).rowcount > 0

    def alerts_for_ui(self, now: datetime) -> list:
        since = iso(now - timedelta(minutes=20))
        rows = self._rows("SELECT * FROM alerts WHERE (resolved_at IS NULL OR resolved_at >= ?) "
                          "ORDER BY (resolved_at IS NULL) DESC, opened_at DESC LIMIT 80", (since,))
        out = []
        for r in rows:
            if r["dismissed_at"]:
                continue
            if r["snoozed_until"] and r["resolved_at"] is None and ts(r["snoozed_until"]) > now:
                continue
            out.append(r)
        return out

    def alerts_in(self, period: str, now: datetime, limit=300) -> list:
        start, end = period_range(period, now)
        return self._rows("SELECT * FROM alerts WHERE opened_at >= ? AND opened_at < ? ORDER BY opened_at DESC LIMIT ?",
                          (iso(start), iso(end), limit))

    def alerts_for_order(self, oid: str) -> list:
        return self._rows("SELECT * FROM alerts WHERE order_id=? ORDER BY opened_at", (oid,))

    # ================================================================ riders, sessions, positions
    def upsert_rider(self, rid, name, phone, online: bool, lat, lng, active_ids: list, now: datetime):
        prev = self._rows("SELECT online, online_since FROM riders WHERE id=?", (rid,))
        was_online = bool(prev and prev[0]["online"])
        online_since = None
        if online:
            online_since = prev[0]["online_since"] if was_online and prev[0]["online_since"] else iso(now)
        self._exec("INSERT INTO riders (id, name, phone, online, lat, lng, active_ids, online_since, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, phone=excluded.phone, "
                   "online=excluded.online, lat=excluded.lat, lng=excluded.lng, active_ids=excluded.active_ids, "
                   "online_since=excluded.online_since, updated_at=excluded.updated_at",
                   (rid, name, phone, 1 if online else 0, lat, lng, json.dumps(active_ids), online_since, iso(now)))
        # sessions
        if online and not was_online:
            self._exec("INSERT INTO rider_sessions (rider_id, online_at) VALUES (?,?)", (rid, iso(now)))
        elif not online and was_online:
            self._exec("UPDATE rider_sessions SET offline_at=? WHERE rider_id=? AND offline_at IS NULL", (iso(now), rid))
        elif online and not self._rows("SELECT 1 FROM rider_sessions WHERE rider_id=? AND offline_at IS NULL", (rid,)):
            self._exec("INSERT INTO rider_sessions (rider_id, online_at) VALUES (?,?)", (rid, iso(now)))

    def close_stale_sessions(self, now: datetime, max_gap_min=180):
        """After a restart: sessions left open by a rider whose last update is hours old are closed at that update.
        (In webhook mode a rider only 'updates' on events, so a short gap would log everyone off at every restart;
        in API mode the next poll corrects the flag within 30 s anyway.)"""
        for r in self._rows("SELECT id, name, updated_at FROM riders"):
            if r["updated_at"] and (now - ts(r["updated_at"])).total_seconds() > max_gap_min * 60:
                self._exec("UPDATE rider_sessions SET offline_at=? WHERE rider_id=? AND offline_at IS NULL",
                           (r["updated_at"], r["id"]))
                self._exec("UPDATE riders SET online=0 WHERE id=?", (r["id"],))

    def riders(self) -> list:
        rows = self._rows("SELECT * FROM riders ORDER BY online DESC, name")
        for r in rows:
            r["active_ids"] = json.loads(r["active_ids"] or "[]")
        return rows

    POSITION_EVERY_S = 30          # one stored GPS point per rider per 30 s is plenty for trails and "not moving"
    POSITION_KEEP_DAYS = 2

    def record_position(self, rid, lat, lng, order_id, now: datetime):
        if lat is None or lng is None:
            return
        last_at = getattr(self, "_last_pos", None)
        if last_at is None:
            last_at = self._last_pos = {}
        prev = last_at.get(rid)
        if prev and (now - prev).total_seconds() < self.POSITION_EVERY_S:
            return
        last = self._rows("SELECT lat, lng FROM positions WHERE rider_id=? ORDER BY at DESC LIMIT 1", (rid,))
        if last and last[0]["lat"] == lat and last[0]["lng"] == lng:
            return
        last_at[rid] = now
        self._exec("INSERT INTO positions (rider_id, at, lat, lng, order_id) VALUES (?,?,?,?,?)",
                   (rid, iso(now), lat, lng, order_id))

    def positions_for_order(self, o: dict) -> list:
        if not o.get("rider_id") or not o.get("dispatched_at"):
            return []
        end = o.get("delivered_at") or o.get("cancelled_at") or datetime.now(UTC)
        return self._rows("SELECT at, lat, lng FROM positions WHERE rider_id=? AND at >= ? AND at <= ? ORDER BY at",
                          (o["rider_id"], iso(o["dispatched_at"]), iso(end)))

    def trail_km(self, o: dict) -> float | None:
        pts = self.positions_for_order(o)
        if len(pts) < 2:
            return None
        d = sum(haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) for a, b in zip(pts, pts[1:]))
        return round(d / 1000, 1)

    def cleanup(self, now: datetime):
        self._exec("DELETE FROM positions WHERE at < ?", (iso(now - timedelta(days=self.POSITION_KEEP_DAYS)),))
        self._exec("DELETE FROM rider_sessions WHERE online_at < ?", (iso(now - timedelta(days=60)),))
        self._exec("DELETE FROM alerts WHERE opened_at < ?", (iso(now - timedelta(days=90)),))

    def db_size_bytes(self) -> int:
        try:
            page, cnt = self.db.execute("PRAGMA page_size").fetchone()[0], self.db.execute("PRAGMA page_count").fetchone()[0]
            return page * cnt
        except Exception:
            return 0

    def vacuum(self) -> bool:
        """Give deleted space back to the disk (SQLite keeps it otherwise). Needs free disk ≈ the DB size."""
        try:
            with self.lock:
                self.db.execute("VACUUM")
                self.db.commit()
            return True
        except Exception:
            return False

    def online_minutes(self, rider_id: str, start: datetime, end: datetime) -> float:
        total = 0.0
        for s in self._rows("SELECT online_at, offline_at FROM rider_sessions WHERE rider_id=? AND online_at < ? "
                            "AND (offline_at IS NULL OR offline_at > ?)", (rider_id, iso(end), iso(start))):
            a = max(ts(s["online_at"]), start)
            b = min(ts(s["offline_at"]) or min(end, datetime.now(UTC)), end)
            if b > a:
                total += (b - a).total_seconds() / 60
        return round(total, 1)

    def rider_hours_by_hour(self, start: datetime, end: datetime) -> dict:
        """hour (Berlin) -> rider-hours online, summed over all riders."""
        out = {}
        for s in self._rows("SELECT online_at, offline_at FROM rider_sessions WHERE online_at < ? "
                            "AND (offline_at IS NULL OR offline_at > ?)", (iso(end), iso(start))):
            a = max(ts(s["online_at"]), start)
            b = min(ts(s["offline_at"]) or min(end, datetime.now(UTC)), end)
            t = a
            while t < b:
                nxt = min(b, (t + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
                h = t.astimezone(BERLIN).hour
                out[h] = out.get(h, 0) + (nxt - t).total_seconds() / 3600
                t = nxt
        return {h: round(v, 1) for h, v in out.items()}

    # ================================================================ analytics
    def insights(self, period: str, now: datetime, rules, sessions_ok: bool = False) -> dict:
        """sessions_ok=False (webhook mode): online/offline is only partly known, so every metric built on
        'hours online' (busy %, orders/hour, idle, rider-hours per hour) is left out; staffing uses the riders
        who actually handled orders in that hour instead — which comes straight from the orders."""
        start, end = period_range(period, now)
        orders = [o for o in self.orders_in(period, now) if o["dispatched_at"] and start <= o["dispatched_at"] < end]
        done = [o for o in orders if o["phase"] == "delivered" and o["delivered_at"]]
        cancelled = [o for o in orders if o["phase"] == "cancelled"]
        tgt = rules.ptod_target_min
        ptods = [o["phases"]["ptod"] for o in done if o["phases"]["ptod"] is not None]
        alerts = self.alerts_in(period, now, 2000)
        by_rider_alerts, by_kind = {}, {}
        for a in alerts:
            by_rider_alerts.setdefault(a["rider_id"] or "", {}).setdefault(a["kind"], 0)
            by_rider_alerts[a["rider_id"] or ""][a["kind"]] += 1
            by_kind[a["kind"]] = by_kind.get(a["kind"], 0) + 1

        def group(rows, key):
            g = {}
            for r in rows:
                g.setdefault(key(r), []).append(r)
            return g

        # --- restaurants ---
        restaurants = []
        for name, rs in group(done + cancelled, lambda o: o["restaurant"] or "?").items():
            d = [o for o in rs if o["phase"] == "delivered"]
            rs_sorted = sorted(rs, key=lambda o: -(o["phases"]["at_restaurant"] or 0))
            restaurants.append({"restaurant": name, "orders": len(rs), "delivered": len(d), "cancelled": len(rs) - len(d),
                                "place_id": next((o.get("place_id") for o in rs if o.get("place_id")), ""),
                                "refs": [o["ref"] for o in rs_sorted[:4]],
                                "avg_wait": _avg([o["phases"]["at_restaurant"] for o in d]),
                                "max_wait": max([o["phases"]["at_restaurant"] or 0 for o in d], default=None),
                                "avg_ptod": _avg([o["phases"]["ptod"] for o in d]),
                                "within_pct": _pct(sum(1 for o in d if (o["phases"]["ptod"] or 999) <= tgt), len(d))})
        restaurants.sort(key=lambda x: -(x["avg_wait"] or 0))

        # --- hours: demand vs supply ---
        rider_hours = self.rider_hours_by_hour(start, end)
        days = max(1, round((min(end, now) - start).total_seconds() / 86400))
        hours = []
        for h, rs in sorted(group(orders, lambda o: o["hour"]).items(), key=lambda x: ((x[0] is None), ((x[0] or 0) - DAY_STARTS_AT) % 24)):
            d = [o for o in rs if o["phase"] == "delivered"]
            rh = rider_hours.get(h, 0) if sessions_ok else 0
            active = len({o["rider_id"] for o in rs if o["rider_id"]})
            hours.append({"hour": h, "orders": len(rs), "delivered": len(d), "cancelled": sum(1 for o in rs if o["phase"] == "cancelled"),
                          "avg_ptod": _avg([o["phases"]["ptod"] for o in d]),
                          "avg_accept": _avg([o["phases"]["to_accept"] for o in rs if o["accepted_at"]]),
                          "within_pct": _pct(sum(1 for o in d if (o["phases"]["ptod"] or 999) <= tgt), len(d)),
                          "rider_hours": (round(rh / days, 1) if days > 1 else rh) if sessions_ok else None,
                          "orders_per_rider_hour": round(len(rs) / rh, 1) if rh else None,
                          "riders_active": round(active / days, 1) if days > 1 else active,
                          "orders_per_rider": round(len(rs) / active, 1) if active else None,
                          "no_rider_5": sum(1 for o in rs if (o["phases"]["to_accept"] or 0) >= 5 or (not o["accepted_at"] and o["phase"] == "cancelled"))})

        # --- riders ---
        handbacks = {}
        for o in orders:
            for h in o.get("history") or []:
                if h.get("what") == "released" and h.get("rider_id"):
                    handbacks[h["rider_id"]] = handbacks.get(h["rider_id"], 0) + 1
        riders = []
        for rid, rs in group(orders, lambda o: o["rider_id"] or "").items():
            if not rid:
                continue
            d = [o for o in rs if o["phase"] == "delivered"]
            ph = lambda k: _avg([o["phases"][k] for o in d])
            busy = sum((o["phases"]["ptod"] or 0) - (o["phases"]["to_accept"] or 0) for o in d)  # accept -> delivered
            online = self.online_minutes(rid, start, min(end, now)) if sessions_ok else 0
            kms = [self.trail_km(o) for o in d[-30:]]
            kms = [k for k in kms if k]
            riders.append({"rider_id": rid, "rider": rs[-1]["rider"] or "Unknown", "orders": len(rs), "delivered": len(d),
                           "cancelled": sum(1 for o in rs if o["phase"] == "cancelled"),
                           "live": sum(1 for o in rs if o["phase"] not in ("delivered", "cancelled", "closed")),
                           "within_pct": _pct(sum(1 for o in d if (o["phases"]["ptod"] or 999) <= tgt), len(d)),
                           "avg_ptod": ph("ptod"), "median_ptod": round(median([o["phases"]["ptod"] for o in d if o["phases"]["ptod"] is not None]), 1) if d else None,
                           "avg_accept": ph("to_accept"), "avg_to_restaurant": ph("to_restaurant"),
                           "avg_wait": ph("at_restaurant"), "avg_to_customer": ph("to_customer"), "avg_handover": ph("handover"),
                           "avg_delivery_min": round(busy / len(d), 1) if d else None,
                           "delivery_minutes": round(busy), "online_minutes": online,
                           "utilisation_pct": _pct(busy, online) if online else None,
                           "orders_per_hour": round(len(d) / (online / 60), 1) if online >= 30 else None,
                           "idle_minutes": round(max(0, online - busy)) if online else None,
                           "avg_km": _avg(kms), "double": sum(1 for o in rs if o["stacked"]), "handbacks": handbacks.get(rid, 0),
                           "alerts": sum(by_rider_alerts.get(rid, {}).values()), "alert_kinds": by_rider_alerts.get(rid, {})})
        riders.sort(key=lambda x: (-(x["within_pct"] if x["within_pct"] is not None else -1), x["avg_ptod"] or 0, -x["delivered"]))

        # --- districts (customer postcode) ---
        districts = []
        for z, rs in group(done, lambda o: o.get("customer_zip") or "?").items():
            districts.append({"zip": z, "orders": len(rs), "avg_to_customer": _avg([o["phases"]["to_customer"] for o in rs]),
                              "avg_ptod": _avg([o["phases"]["ptod"] for o in rs]),
                              "within_pct": _pct(sum(1 for o in rs if (o["phases"]["ptod"] or 999) <= tgt), len(rs))})
        districts.sort(key=lambda x: -(x["avg_ptod"] or 0))

        single = [o["phases"]["ptod"] for o in done if not o["stacked"] and o["phases"]["ptod"] is not None]
        double = [o["phases"]["ptod"] for o in done if o["stacked"] and o["phases"]["ptod"] is not None]
        phases = {k: _avg([o["phases"][k] for o in done]) for k in ("to_accept", "to_restaurant", "at_restaurant", "to_customer", "handover")}
        late = sorted([o for o in done if (o["phases"]["ptod"] or 0) > tgt], key=lambda o: -(o["phases"]["ptod"] or 0))
        # which phase was worst in the late orders (vs the period average) -> the real cause of lateness
        cause = {}
        for o in late:
            worst, gap = None, 0
            for k in ("to_accept", "to_restaurant", "at_restaurant", "to_customer", "handover"):
                v, avg = o["phases"][k], phases[k]
                if v is not None and avg is not None and v - avg > gap:
                    worst, gap = k, v - avg
            if worst:
                cause[worst] = cause.get(worst, 0) + 1

        focus = self._focus(rules, restaurants, hours, riders, phases, single, double, by_kind, late, cause, districts, sessions_ok)
        return {"period": period, "start": iso(start), "orders": len(orders), "delivered": len(done),
                "cancelled": len(cancelled), "cancel_pct": _pct(len(cancelled), len(orders)),
                "within_pct": _pct(sum(1 for p in ptods if p <= tgt), len(ptods)), "target_within_pct": rules.target_within_pct,
                "avg_ptod": _avg(ptods), "median_ptod": round(median(ptods), 1) if ptods else None,
                "p90_ptod": round(sorted(ptods)[int(len(ptods) * 0.9) - 1], 1) if len(ptods) >= 5 else None,
                "late": len(late), "late_cause": cause,
                "phases": phases, "single_avg": _avg(single), "double_avg": _avg(double), "double_orders": len(double),
                "restaurants": restaurants[:20], "hours": hours, "riders": riders, "districts": districts[:15],
                "alerts_by_kind": by_kind, "alerts_total": len(alerts),
                "handled": sum(1 for a in alerts if a["dismissed_at"]), "focus": focus,
                "late_orders": [{"id": o["id"], "ref": o["ref"], "rider": o["rider"], "restaurant": o["restaurant"], "ptod": o["phases"]["ptod"],
                                 "phases": o["phases"], "hour": o["hour"], "reason": o.get("reason", ""), "note": o.get("note", "")} for o in late[:15]],
                "sessions_ok": sessions_ok,
                "late_reasons": _count(o.get("reason") for o in late if o.get("reason")),
                "late_without_reason": sum(1 for o in late if not o.get("reason")),
                "recent_alerts": alerts[:60]}

    @staticmethod
    def _focus(rules, restaurants, hours, riders, phases, single, double, by_kind, late, cause, districts, sessions_ok=False):
        focus = []
        for x in restaurants:
            if x["delivered"] >= 3 and (x["avg_wait"] or 0) >= rules.wait_restaurant_min:
                focus.append({"icon": "🏪", "title": f"{x['restaurant']}: riders wait {x['avg_wait']:.0f} min on average ({x['delivered']} orders, max {x['max_wait']:.0f})",
                              "action": "Call the restaurant today: start cooking on dispatch and hand over at the counter. If it stays slow, dispatch riders 5 min later for this restaurant."})
        under = [h for h in hours if h["orders"] >= 3 and h["hour"] is not None and ((h["avg_accept"] or 0) >= 4 or (h["orders_per_rider_hour"] or 0) >= 2.5 or (h["orders_per_rider"] or 0) >= 3)]
        under.sort(key=lambda h: -(h["avg_accept"] or 0))
        for h in under[:2]:
            supply = f"{h['rider_hours']} rider-hours" if h.get("rider_hours") is not None else f"{h['riders_active']} riders handling them"
            focus.append({"icon": "⏱", "title": f"{h['hour']:02d}–{h['hour'] + 1:02d}h is under-staffed: {h['orders']} orders, {supply}, accept {h['avg_accept'] or 0:.0f} min",
                          "action": "Add 1–2 riders to this hour in the shift plan; ask riders to go online 15 min earlier."})
        if cause:
            top = max(cause, key=cause.get)
            names = {"to_accept": "waiting for a rider to accept", "to_restaurant": "the ride to the restaurant",
                     "at_restaurant": "waiting at the restaurant", "to_customer": "the ride to the customer", "handover": "the handover"}
            focus.append({"icon": "🎯", "title": f"{len(late)} late orders — the biggest cause was {names[top]} ({cause[top]} of them)",
                          "action": {"to_accept": "Capacity problem: more riders online at peak.", "to_restaurant": "Dispatch riders who are closer; check double orders.",
                                     "at_restaurant": "Restaurant readiness: call slow kitchens, dispatch later.", "to_customer": "Route/zone problem: check the districts table and rider speed.",
                                     "handover": "Riders must call the customer 2 min before arrival; check building access notes."}[top]})
        if (by_kind.get("unassigned", 0)) >= 3:
            focus.append({"icon": "🚫", "title": f"{by_kind['unassigned']} orders had no rider for over {rules.accept_limit_min} min",
                          "action": "Rider capacity is the bottleneck — plan +1–2 riders on the busiest hours."})
        if single and double and mean(double) - mean(single) >= 5:
            focus.append({"icon": "📦", "title": f"Double orders take {mean(double) - mean(single):.0f} min longer ({mean(double):.0f} vs {mean(single):.0f} min)",
                          "action": "Only stack a second order when both ETAs stay under 30 min."})
        if (phases["handover"] or 0) >= 4:
            focus.append({"icon": "🚪", "title": f"Handover at the customer takes {phases['handover']:.0f} min on average",
                          "action": "Riders should call the customer 2 min before arrival."})
        for x in riders:
            if x["delivered"] >= 3 and ((x["within_pct"] or 100) < 70 or (x["avg_ptod"] or 0) > rules.ptod_target_min):
                slow = max((k for k in ("avg_accept", "avg_to_restaurant", "avg_wait", "avg_to_customer", "avg_handover")), key=lambda k: (x[k] or 0) - (phases[{"avg_accept": "to_accept", "avg_to_restaurant": "to_restaurant", "avg_wait": "at_restaurant", "avg_to_customer": "to_customer", "avg_handover": "handover"}[k]] or 0))
                focus.append({"icon": "🧑", "title": f"{x['rider']}: {x['within_pct']}% within target, avg PTOD {x['avg_ptod']:.0f} min — loses most time in {slow.replace('avg_', '').replace('_', ' ')}",
                              "action": "Coach with the numbers from the rider table (tap the name)."})
        for x in riders:
            if sessions_ok and x["online_minutes"] >= 120 and (x["utilisation_pct"] or 0) < 40 and x["delivered"] < 3:
                focus.append({"icon": "💤", "title": f"{x['rider']}: online {x['online_minutes'] / 60:.1f} h but only {x['delivered']} deliveries ({x['utilisation_pct']}% busy)",
                              "action": "Check whether this rider accepts orders; otherwise shift them to a busier hour."})
        for d in districts[:1]:
            if d["orders"] >= 4 and (d["avg_ptod"] or 0) > rules.ptod_target_min:
                focus.append({"icon": "🗺", "title": f"Postcode {d['zip']}: avg PTOD {d['avg_ptod']:.0f} min over {d['orders']} orders",
                              "action": "Far district — position an idle rider nearby at peak, or dispatch earlier for these addresses."})
        if not focus and (single or double):
            focus.append({"icon": "✅", "title": "No structural problem found in this period", "action": "Keep the current setup; watch peak-hour acceptance times."})
        return focus[:7]

    # ================================================================ daily snapshot
    def save_daily(self, day: str, data: dict):
        self._exec("INSERT INTO daily_stats (day, computed_at, data) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET "
                   "computed_at=excluded.computed_at, data=excluded.data", (day, iso(datetime.now(UTC)), json.dumps(data)))

    def daily(self, day: str):
        rows = self._rows("SELECT data FROM daily_stats WHERE day=?", (day,))
        return json.loads(rows[0]["data"]) if rows else None

    def daily_trend(self, days=14) -> list:
        rows = self._rows("SELECT day, data FROM daily_stats ORDER BY day DESC LIMIT ?", (days,))
        out = []
        for r in rows:
            d = json.loads(r["data"])
            out.append({"day": r["day"], "delivered": d.get("delivered"), "within_pct": d.get("within_pct"),
                        "avg_ptod": d.get("avg_ptod"), "cancelled": d.get("cancelled")})
        return list(reversed(out))

    # ================================================================ export
    def export_csv(self, period: str, now: datetime) -> str:
        rows = self.orders_in(period, now)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["order", "status", "rider", "restaurant", "customer", "postcode", "dispatched", "accepted", "at_restaurant",
                    "picked_up", "at_customer", "delivered", "min_to_accept", "min_to_restaurant", "min_at_restaurant",
                    "min_to_customer", "min_handover", "ptod_min", "within_30", "double_order", "cancel_reason"])
        f = lambda v: v.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M") if v else ""
        for o in rows:
            p = o["phases"]
            w.writerow([o["ref"], o["phase"], o["rider"], o["restaurant"], o["customer_addr"], o.get("customer_zip", ""),
                        f(o["dispatched_at"]), f(o["accepted_at"]), f(o["at_restaurant_at"]), f(o["picked_up_at"]),
                        f(o["at_customer_at"]), f(o["delivered_at"]), p["to_accept"], p["to_restaurant"], p["at_restaurant"],
                        p["to_customer"], p["handover"], p["ptod"], "yes" if (p["ptod"] or 999) <= 30 else "no",
                        "yes" if o["stacked"] else "no", o.get("cancel_reason", "")])
        return buf.getvalue()
