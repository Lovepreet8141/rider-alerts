"""SQLite storage + analytics for the Quickzi ops platform.

Tables
  orders          every order ever seen (live + delivered + cancelled) with all phase timestamps
  alerts          every alert raised, with resolution / handled / snooze
  riders          last known state per rider
  rider_sessions  online/offline sessions (for hours online, utilisation, staffing per hour)
  positions       GPS trail of riders while they hold an order (kept 7 days)
  daily_stats     frozen report per operating day (frozen 5 min after the day rolls over, or on demand)
  settings        editable thresholds
  syslog          what the system did / errors (for the System panel)

Operating day = midnight -> midnight Berlin by default (Settings: "operating day starts at"; 4 would make
orders after midnight count for the evening before).
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from statistics import mean, median

from orders import BERLIN, UTC, haversine_m, iso, mins, new_order, on_time, phase_minutes, restaurant_waits, ts

DAY_STARTS_AT = 0          # hour (Berlin) at which the operating day starts — set from Settings at startup


def set_day_start(hour) -> int:
    global DAY_STARTS_AT
    try:
        DAY_STARTS_AT = max(0, min(23, int(hour)))
    except (TypeError, ValueError):
        pass
    return DAY_STARTS_AT

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
CREATE TABLE IF NOT EXISTS shifts (
  rider_id TEXT, rider TEXT, day TEXT, start TEXT, end TEXT, city TEXT, fleet TEXT, imported_at TEXT,
  PRIMARY KEY (rider_id, day)
);
CREATE INDEX IF NOT EXISTS ix_shifts_day ON shifts(day);
CREATE TABLE IF NOT EXISTS handbacks (
  order_id TEXT, rider_id TEXT, day TEXT, at TEXT, waited REAL, excused INTEGER DEFAULT 0, city TEXT, fleet TEXT,
  PRIMARY KEY (order_id, rider_id)
);
CREATE INDEX IF NOT EXISTS ix_handbacks_day ON handbacks(day);
CREATE TABLE IF NOT EXISTS auto_msgs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, day TEXT, rule TEXT, rider_id TEXT, rider TEXT, order_id TEXT, order_ref TEXT,
  text TEXT, mode TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS ix_auto_day ON auto_msgs(day, rider_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS syslog (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, msg TEXT);
"""

DT_FIELDS = ("created_at", "dispatched_at", "accepted_at", "started_at", "at_restaurant_at", "picked_up_at",
             "at_customer_at", "delivered_at", "eta_restaurant", "eta_customer", "scheduled_at", "last_event_at", "eta_at",
             "promised_at")


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
    if period == "lastweek":
        local = start.astimezone(BERLIN)
        this_week = (local - timedelta(days=local.weekday())).astimezone(UTC)
        return this_week - timedelta(days=7), this_week
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


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 1) if vals else None


def _within(rows, tgt):
    """% of orders delivered within the target — only over orders whose PTOD is known."""
    known = [o for o in rows if o["phases"]["ptod"] is not None]
    return _pct(sum(1 for o in known if o["phases"]["ptod"] <= tgt), len(known))


def _on_time(rows, grace):
    """% of orders delivered by their planned time (+ grace) — only over orders whose plan is known."""
    flags = [on_time(o, grace) for o in rows]
    known = [f for f in flags if f is not None]
    return _pct(sum(1 for f in known if f), len(known))


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
        self._alert_written: dict = {}          # alert id -> last payload written (skip identical UPDATEs)
        with self.lock:
            # WAL + synchronous=NORMAL: a commit no longer fsyncs the main file (the Railway volume is slow at that);
            # the data is still safe against process crashes.  busy_timeout: never fail on a short lock.
            for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=5000", "temp_store=MEMORY", "cache_size=-20000"):
                try:
                    self.db.execute(f"PRAGMA {pragma}")
                except Exception:
                    pass
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)").fetchall()}
            for col, typ in (("reason", "TEXT"), ("note", "TEXT"), ("reason_at", "TEXT"), ("city", "TEXT"), ("day", "TEXT"), ("hour", "INTEGER"),
                             ("on_time", "INTEGER"), ("vs_plan", "REAL"), ("kitchen_wait", "REAL"), ("accept_min", "REAL"), ("fleet", "TEXT")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")
            rcols = {r[1] for r in self.db.execute("PRAGMA table_info(riders)").fetchall()}
            for col in ("city", "fleet"):
                if col not in rcols:
                    self.db.execute(f"ALTER TABLE riders ADD COLUMN {col} TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_orders_city_day ON orders(city, day)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_orders_day ON orders(day)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_orders_ref ON orders(ref)")
            self.db.commit()
        self.city_map: dict = {}        # MotionTools service-area id -> city name (Settings "city:<id>")
        self.fleet_map: dict = {}       # rider id -> fleet name (Settings "fleet:<rider id>")
        self.plan_grace = 5
        self.wait_restaurant_min = 8
        self._rider_city: dict = {}
        self._load_maps()

    def _load_maps(self):
        for k, v in self.get_settings().items():
            if k.startswith("city:"):
                self.city_map[k[5:]] = v
            elif k.startswith("fleet:"):
                self.fleet_map[k[6:]] = v

    def city_of(self, o: dict) -> str:
        """City name of an order: the name given in Settings for its MotionTools service area, else the area's own
        name from the API, else the first 8 characters of the area id (so every area shows up even before it is named)."""
        area = o.get("area") or ""
        return self.city_map.get(area) or (o.get("area_name") or "") or (area[:8] if area else "")

    def set_city_name(self, area_id: str, name: str):
        self.city_map[area_id] = name
        self.set_settings({f"city:{area_id}": name})
        self._exec("UPDATE orders SET city=? WHERE area=?", (name, area_id))
        self._exec("UPDATE riders SET city=? WHERE city=? OR city=?", (name, area_id[:8], area_id))

    def set_fleet(self, rider_id: str, fleet: str):
        self.fleet_map[rider_id] = fleet
        self.set_settings({f"fleet:{rider_id}": fleet})
        self._exec("UPDATE riders SET fleet=? WHERE id=?", (fleet, rider_id))
        self._exec("UPDATE orders SET fleet=? WHERE rider_id=?", (fleet, rider_id))

    def areas_seen(self) -> list:
        """Every service area that ever appeared on an order, with its name (if given) and order count (30 days)."""
        rows = self._rows("SELECT area, city, COUNT(*) AS n, MAX(updated_at) AS last FROM orders WHERE area IS NOT NULL AND area != '' "
                          "AND updated_at >= ? GROUP BY area ORDER BY n DESC", (iso(datetime.now(UTC) - timedelta(days=30)),))
        return [{"area": r["area"], "name": self.city_map.get(r["area"], ""), "shown_as": r["city"] or r["area"][:8], "orders": r["n"], "last": r["last"]} for r in rows]

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
        raw = json.dumps({k: (iso(v) if isinstance(v, datetime) else v) for k, v in o.items() if k not in ("phases", "hour", "day", "city", "fleet")})
        city = self.city_of(o)
        o["city"] = city
        when = o.get("dispatched_at") or o.get("delivered_at") or o.get("created_at")
        day = day_key(when) if when else None
        hour = (o.get("dispatched_at") or o.get("cancelled_at") or when).astimezone(BERLIN).hour if when else None
        ot = on_time(o, self.plan_grace) if o["phase"] == "delivered" else None
        kitchen = restaurant_waits(o)[0] if o["phase"] == "delivered" else None
        fleet = self.fleet_map.get(o.get("rider_id") or "", "")
        if o.get("rider_id") and self._rider_city.get(o["rider_id"]) != city and city:
            self._rider_city[o["rider_id"]] = city
            self._exec("UPDATE riders SET city=? WHERE id=?", (city, o["rider_id"]))
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
            "delivered_at, cancelled_at, cancel_reason, ptod_min, closed, first_seen, updated_at, raw, "
            "city, day, hour, on_time, vs_plan, kitchen_wait, accept_min, fleet) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET ref=excluded.ref, area=excluded.area, " + merge +
            "restaurant=excluded.restaurant, place_id=excluded.place_id, customer_addr=excluded.customer_addr, "
            "customer_zip=excluded.customer_zip, status=excluded.status, phase=excluded.phase, stacked=excluded.stacked, "
            "dispatched_at=excluded.dispatched_at, cancelled_at=excluded.cancelled_at, "
            "cancel_reason=excluded.cancel_reason, "
            "closed=excluded.closed, updated_at=excluded.updated_at, raw=excluded.raw, "
            "city=excluded.city, day=excluded.day, hour=excluded.hour, on_time=excluded.on_time, vs_plan=excluded.vs_plan, "
            "kitchen_wait=excluded.kitchen_wait, accept_min=excluded.accept_min, fleet=excluded.fleet",
            (o["id"], o["ref"], o["area"], o["rider_id"], o["rider"] or "", o["restaurant"], o.get("place_id", ""),
             o["customer_addr"], o.get("customer_zip", ""), o["status"], o["phase"], stacked_flag,
             iso(o["dispatched_at"]), iso(o["accepted_at"]), iso(o["started_at"]), iso(o["at_restaurant_at"]),
             iso(o["picked_up_at"]), iso(o["at_customer_at"]), iso(o["delivered_at"]), cancelled_at,
             o.get("cancel_reason", ""), pm["ptod"], closed, first_seen, iso(now), raw,
             city, day, hour, (None if ot is None else int(ot)), pm["vs_plan"], kitchen, pm["to_accept"], fleet))
        if any(h.get("what") == "released" for h in (o.get("history") or [])):
            self._write_handbacks(o, day, city)

    def _write_handbacks(self, o: dict, day: str, city: str):
        """One row per rider who handed this order back (from the order's history); excused = waited at the restaurant
        at least the restaurant threshold first (the kitchen's fault, not the rider's)."""
        waits = restaurant_waits(o)[1]
        rows = []
        for h in o.get("history") or []:
            if h.get("what") == "released" and h.get("rider_id"):
                w = waits.get(h["rider_id"])
                waited = w[0] if w else None
                rows.append((o["id"], h["rider_id"], day, h.get("at"), waited, 1 if (waited is not None and waited >= self.wait_restaurant_min) else 0,
                             city, self.fleet_map.get(h["rider_id"], "")))
        with self.lock:
            self.db.execute("DELETE FROM handbacks WHERE order_id=?", (o["id"],))
            self.db.executemany("INSERT OR REPLACE INTO handbacks (order_id, rider_id, day, at, waited, excused, city, fleet) VALUES (?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def backfill_handbacks(self, now: datetime, days: int = 90) -> int:
        """Once after the 6.3 update: hand-backs of the orders already stored (their history lives in the raw JSON)."""
        if self.get_settings().get("handbacks_backfilled"):
            return 0
        n = 0
        for r in self._rows("SELECT * FROM orders WHERE raw LIKE '%\"released\"%' AND day >= ?", (day_key(now - timedelta(days=days)),)):
            o = self._hydrate(r)
            self._write_handbacks(o, r.get("day"), r.get("city") or "")
            n += 1
        self.set_settings({"handbacks_backfilled": iso(now)})
        return n

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
        o = json.loads(r["raw"]) if r.get("raw") else new_order(r["id"], r.get("ref") or "", r.get("area"))
        if not r.get("raw"):                       # archived row: rebuild what the columns know
            for k in ("rider_id", "rider", "restaurant", "place_id", "customer_addr", "customer_zip", "status", "phase", "cancel_reason",
                      "dispatched_at", "accepted_at", "started_at", "at_restaurant_at", "picked_up_at", "at_customer_at", "delivered_at"):
                if r.get(k) is not None:
                    o[k] = r[k]
            o["created_at"] = r.get("first_seen"); o["archived"] = True
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
        o["city"], o["fleet"], o["day"] = r.get("city") or self.city_of(o), r.get("fleet") or "", r.get("day")
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

    def orders_in(self, period: str, now: datetime, q: str = "", city: str = "") -> list:
        start, end = period_range(period, now)
        where = "((dispatched_at >= ? AND dispatched_at < ?) OR closed=0 OR (dispatched_at IS NULL AND first_seen >= ? AND first_seen < ?))"
        args = [iso(start), iso(end), iso(start), iso(end)]
        if city:
            where += " AND city=?"; args.append(city)
        rows = self._rows(f"SELECT * FROM orders WHERE {where} ORDER BY COALESCE(dispatched_at, first_seen) DESC", args)
        out = [self._hydrate(r) for r in rows]
        if q:
            ql = q.lower()
            out = [o for o in out if ql in (o["ref"] or "").lower() or ql in (o["rider"] or "").lower()
                   or ql in (o["restaurant"] or "").lower() or ql in (o["customer_addr"] or "").lower()]
        return out

    def order(self, oid: str):
        rows = self._rows("SELECT * FROM orders WHERE id=?", (oid,))
        return self._hydrate(rows[0]) if rows else None

    def order_by_ref(self, ref: str):
        rows = self._rows("SELECT * FROM orders WHERE upper(ref)=? ORDER BY first_seen DESC LIMIT 1", (ref.upper(),))
        return self._hydrate(rows[0]) if rows else None

    def delete_day(self, day: str, now: datetime, dry_run: bool = False, city: str = "") -> dict:
        """Wipe one operating day that was recorded wrongly: its finished orders, their alerts and GPS points, and the
        frozen report. Live orders are never touched. dry_run only counts."""
        start, end = period_range(day, now)
        a, b = iso(start), iso(end)
        where = "closed=1 AND COALESCE(dispatched_at, first_seen) >= ? AND COALESCE(dispatched_at, first_seen) < ?" + (" AND city=?" if city else "")
        wargs = (a, b, city) if city else (a, b)
        ids = [r["id"] for r in self._rows(f"SELECT id FROM orders WHERE {where}", wargs)]
        alerts = self._rows("SELECT COUNT(*) AS n FROM alerts WHERE opened_at >= ? AND opened_at < ?", (a, b))[0]["n"]
        reports = self._rows("SELECT COUNT(*) AS n FROM daily_stats WHERE day=?", (day,))[0]["n"]
        out = {"day": day, "orders": len(ids), "alerts": alerts, "reports": reports, "deleted": False}
        if dry_run or not (ids or alerts or reports):
            return out
        with self.lock:
            self.db.execute(f"DELETE FROM orders WHERE {where}", wargs)
            self.db.execute("DELETE FROM alerts WHERE opened_at >= ? AND opened_at < ?", (a, b))
            self.db.execute("DELETE FROM positions WHERE at >= ? AND at < ?", (a, b))
            self.db.execute("DELETE FROM daily_stats WHERE day=?", (day,))
            self.db.commit()
        out["deleted"] = True
        self.log("info", f"data of {day} deleted on request: {len(ids)} orders, {alerts} alerts, {reports} frozen report(s)")
        return out

    def delivered(self, period: str, now: datetime, city: str = "") -> list:
        return [o for o in self.orders_in(period, now, city=city) if o["phase"] == "delivered" and o["delivered_at"]]

    def backfill_columns(self, limit: int = 2000) -> int:
        """Rows written before 6.1 have no city/day/hour columns yet — fill them batch by batch (runs after startup)."""
        rows = self._rows("SELECT * FROM orders WHERE day IS NULL AND raw IS NOT NULL LIMIT ?", (limit,))
        for r in rows:
            o = self._hydrate(r)
            self.upsert_order(o, ts(r.get("updated_at")) or datetime.now(UTC), stacked=bool(r.get("stacked")), force=True)
        return len(rows)

    def cities_on(self, day: str) -> list:
        return [r["city"] for r in self._rows("SELECT DISTINCT city FROM orders WHERE day=? AND city IS NOT NULL AND city != ''", (day,))]

    def archive_old(self, now: datetime, keep_days: int = 90) -> int:
        """Rows older than keep_days lose their raw JSON (history, addresses, phones) but keep every number the reports
        use. At 170 000 orders a month this keeps the database at a fraction of the size."""
        cutoff = day_key(now - timedelta(days=keep_days))
        return self._exec("UPDATE orders SET raw=NULL WHERE day < ? AND raw IS NOT NULL AND closed=1", (cutoff,)).rowcount

    def network_today(self, now: datetime, grace: int) -> dict:
        """Per city, today, straight from SQL: delivered, on-time %, avg PTOD, cancelled, orders per hour."""
        day = day_key(now)
        out = {}
        for r in self._rows("SELECT city, SUM(phase='delivered') AS delivered, SUM(phase='cancelled') AS cancelled, COUNT(*) AS orders, "
                            "AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod, "
                            "SUM(CASE WHEN on_time=1 THEN 1 ELSE 0 END) AS ot, SUM(CASE WHEN on_time IS NOT NULL THEN 1 ELSE 0 END) AS otn, "
                            "SUM(CASE WHEN ptod_min IS NOT NULL AND ptod_min > 30 THEN 1 ELSE 0 END) AS late "
                            "FROM orders WHERE day=? GROUP BY city", (day,)):
            out[r["city"] or ""] = {"delivered": r["delivered"] or 0, "cancelled": r["cancelled"] or 0, "orders": r["orders"] or 0,
                                    "avg_ptod": round(r["avg_ptod"], 1) if r["avg_ptod"] is not None else None,
                                    "on_time_pct": _pct(r["ot"] or 0, r["otn"] or 0), "late": r["late"] or 0, "hours": {}}
        for r in self._rows("SELECT city, hour, COUNT(*) AS n FROM orders WHERE day=? AND hour IS NOT NULL GROUP BY city, hour", (day,)):
            out.setdefault(r["city"] or "", {"delivered": 0, "cancelled": 0, "orders": 0, "avg_ptod": None, "on_time_pct": None, "late": 0, "hours": {}})["hours"][r["hour"]] = r["n"]
        return out

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
        """Only writes when something visible changed — at peak this runs for ~100 alerts after every event."""
        key = (a["severity"], a["headline"], a["action"], a["phone"], a["map_url"], a["rider"], a["rider_id"], a["restaurant"], a["restaurant_phone"])
        if self._alert_written.get(aid) == key:
            return
        self._alert_written[aid] = key
        self._exec("UPDATE alerts SET severity=?, headline=?, action=?, phone=?, map_url=?, rider=?, rider_id=?, "
                   "restaurant=?, restaurant_phone=?, updated_at=? WHERE id=?", key + (iso(now), aid))

    def resolve_alert(self, aid: int, why: str, now: datetime):
        self._alert_written.pop(aid, None)
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
        rows = self._rows("SELECT * FROM riders ORDER BY name")
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

    def backup_to(self, path: str):
        import sqlite3 as _sq
        with self.lock:
            dst = _sq.connect(path)
            self.db.backup(dst)
            dst.close()

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
    def insights(self, period: str, now: datetime, rules, sessions_ok: bool = False, city: str = "") -> dict:
        """sessions_ok=False (webhook mode): online/offline is only partly known, so every metric built on
        'hours online' (busy %, orders/hour, idle, rider-hours per hour) is left out; staffing uses the riders
        who actually handled orders in that hour instead — which comes straight from the orders."""
        start, end = period_range(period, now)
        def when(o):                       # an order belongs to the period of its dispatch; if that is unknown, of its delivery / creation
            return o["dispatched_at"] or o["delivered_at"] or o.get("created_at")
        orders = [o for o in self.orders_in(period, now, city=city) if when(o) and start <= when(o) < end]
        done = [o for o in orders if o["phase"] == "delivered" and o["delivered_at"]]
        cancelled = [o for o in orders if o["phase"] == "cancelled"]
        tgt = rules.ptod_target_min
        grace = rules.plan_grace_min
        ptods = [o["phases"]["ptod"] for o in done if o["phases"]["ptod"] is not None]
        vs_plan = [o["phases"]["vs_plan"] for o in done if o["phases"]["vs_plan"] is not None]
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
        kitchen, own_waits = {}, {}                         # per order: kitchen wait (any rider) · per rider: own waits
        for o in orders:
            k, w = restaurant_waits(o)
            kitchen[o["id"]] = k
            for rid, (m, gave_up) in w.items():
                if rid and m is not None:
                    own_waits.setdefault(rid, []).append((m, gave_up))
        for name, rs in group(done + cancelled, lambda o: o["restaurant"] or "?").items():
            d = [o for o in rs if o["phase"] == "delivered"]
            rs_sorted = sorted(rs, key=lambda o: -(kitchen.get(o["id"]) or 0))
            restaurants.append({"restaurant": name, "orders": len(rs), "delivered": len(d), "cancelled": len(rs) - len(d),
                                "place_id": next((o.get("place_id") for o in rs if o.get("place_id")), ""),
                                "refs": [o["ref"] for o in rs_sorted[:4]],
                                "avg_wait": _avg([kitchen.get(o["id"]) for o in d]),          # first rider's arrival -> pickup
                                "max_wait": max([kitchen.get(o["id"]) or 0 for o in d], default=None),
                                "gave_up": sum(1 for o in rs for _, g in restaurant_waits(o)[1].values() if g),
                                "avg_ptod": _avg([o["phases"]["ptod"] for o in d]),
                                "within_pct": _within(d, tgt)})
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
                          "within_pct": _within(d, tgt), "on_time_pct": _on_time(d, grace),
                          "rider_hours": (round(rh / days, 1) if days > 1 else rh) if sessions_ok else None,
                          "orders_per_rider_hour": round(len(rs) / rh, 1) if rh else None,
                          "riders_active": round(active / days, 1) if days > 1 else active,
                          "orders_per_rider": round(len(rs) / active, 1) if active else None,
                          "no_rider_5": sum(1 for o in rs if (o["phases"]["to_accept"] or 0) >= 5 or (not o["accepted_at"] and o["phase"] == "cancelled"))})

        # --- riders ---
        handbacks, excused = {}, {}
        for o in orders:
            for h in o.get("history") or []:
                if h.get("what") == "released" and h.get("rider_id"):
                    handbacks[h["rider_id"]] = handbacks.get(h["rider_id"], 0) + 1
            for rid, (m, gave_up) in restaurant_waits(o)[1].items():
                if gave_up and rid and m is not None and m >= rules.wait_restaurant_min:
                    excused[rid] = excused.get(rid, 0) + 1       # handed back after waiting long enough — the kitchen's fault
        riders = []
        for rid, rs in group(orders, lambda o: o["rider_id"] or "").items():
            if not rid:
                continue
            d = [o for o in rs if o["phase"] == "delivered"]
            ph = lambda k: _avg([o["phases"][k] for o in d])
            busy = sum((o["phases"]["ptod"] or 0) - (o["phases"]["to_accept"] or 0) for o in d)  # accept -> delivered
            online = self.online_minutes(rid, start, min(end, now)) if sessions_ok else 0
            kms = [self.trail_km(o) for o in d[-8:]]            # one positions query per order — keep it small
            kms = [k for k in kms if k]
            riders.append({"rider_id": rid, "rider": rs[-1]["rider"] or "Unknown", "city": rs[-1].get("city") or "", "fleet": rs[-1].get("fleet") or "",
                           "orders": len(rs), "delivered": len(d),
                           "cancelled": sum(1 for o in rs if o["phase"] == "cancelled"),
                           "live": sum(1 for o in rs if o["phase"] not in ("delivered", "cancelled", "closed")),
                           "within_pct": _within(d, tgt), "on_time_pct": _on_time(d, grace),
                           "avg_vs_plan": _avg([o["phases"]["vs_plan"] for o in d]),
                           "avg_ptod": ph("ptod"), "median_ptod": _median([o["phases"]["ptod"] for o in d]),
                           "avg_accept": ph("to_accept"), "avg_to_restaurant": ph("to_restaurant"),
                           "avg_wait": _avg([m for m, _ in own_waits.get(rid, [])]) if own_waits.get(rid) else ph("at_restaurant"),
                           "waits_given_up": sum(1 for _, g in own_waits.get(rid, []) if g),
                           "avg_to_customer": ph("to_customer"), "avg_handover": ph("handover"),
                           "avg_delivery_min": round(busy / len(d), 1) if d else None,
                           "delivery_minutes": round(busy), "online_minutes": online,
                           "utilisation_pct": _pct(busy, online) if online else None,
                           "orders_per_hour": round(len(d) / (online / 60), 1) if online >= 30 else None,
                           "idle_minutes": round(max(0, online - busy)) if online else None,
                           "avg_km": _avg(kms), "double": sum(1 for o in rs if o["stacked"]), "handbacks": handbacks.get(rid, 0),
                           "handbacks_excused": excused.get(rid, 0),
                           "alerts": sum(by_rider_alerts.get(rid, {}).values()), "alert_kinds": by_rider_alerts.get(rid, {})})
        riders.sort(key=lambda x: (-(x["within_pct"] if x["within_pct"] is not None else -1), x["avg_ptod"] or 0, -x["delivered"]))

        # --- districts (customer postcode) ---
        districts = []
        for z, rs in group(done, lambda o: o.get("customer_zip") or "?").items():
            districts.append({"zip": z, "orders": len(rs), "avg_to_customer": _avg([o["phases"]["to_customer"] for o in rs]),
                              "avg_ptod": _avg([o["phases"]["ptod"] for o in rs]),
                              "within_pct": _within(rs, tgt)})
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
        fleets = []
        for fl, rs in group([o for o in orders if o.get("fleet")], lambda o: o["fleet"]).items():
            d = [o for o in rs if o["phase"] == "delivered"]
            fleets.append({"fleet": fl, "orders": len(rs), "delivered": len(d), "riders": len({o["rider_id"] for o in rs if o["rider_id"]}),
                           "within_pct": _within(d, tgt), "on_time_pct": _on_time(d, grace), "avg_ptod": _avg([o["phases"]["ptod"] for o in d]),
                           "avg_accept": _avg([o["phases"]["to_accept"] for o in rs if o["accepted_at"]]),
                           "handbacks": sum(1 for o in rs for h in (o.get("history") or []) if h.get("what") == "released"),
                           "per_rider": round(len(d) / len({o["rider_id"] for o in d if o["rider_id"]}), 1) if d else None})
        fleets.sort(key=lambda x: -(x["on_time_pct"] if x["on_time_pct"] is not None else x["within_pct"] or 0))
        return {"period": period, "city": city, "fleets": fleets, "start": iso(start), "orders": len(orders), "delivered": len(done),
                "cancelled": len(cancelled), "cancel_pct": _pct(len(cancelled), len(orders)),
                "within_pct": _pct(sum(1 for p in ptods if p <= tgt), len(ptods)), "target_within_pct": rules.target_within_pct,
                "on_time_pct": _pct(sum(1 for v in vs_plan if v <= grace), len(vs_plan)), "plan_known": len(vs_plan),
                "avg_vs_plan": _avg(vs_plan), "late_vs_plan": sum(1 for v in vs_plan if v > grace), "plan_grace": grace,
                "avg_ptod": _avg(ptods), "median_ptod": round(median(ptods), 1) if ptods else None,
                "p90_ptod": round(sorted(ptods)[int(len(ptods) * 0.9) - 1], 1) if len(ptods) >= 5 else None,
                "late": len(late), "late_cause": cause,
                "phases": phases, "single_avg": _avg(single), "double_avg": _avg(double), "double_orders": len(double),
                "restaurants": restaurants[:20], "hours": hours, "riders": riders, "districts": districts[:15],
                "alerts_by_kind": by_kind, "alerts_total": len(alerts),
                "handled": sum(1 for a in alerts if a["dismissed_at"]), "focus": focus,
                "late_orders": [{"id": o["id"], "ref": o["ref"], "rider": o["rider"], "restaurant": o["restaurant"], "ptod": o["phases"]["ptod"],
                                 "phases": o["phases"], "hour": o["hour"], "reason": o.get("reason", ""), "note": o.get("note", ""),
                                 "vs_plan": o["phases"]["vs_plan"]} for o in late[:15]],
                "sessions_ok": sessions_ok,
                "late_reasons": _count(o.get("reason") for o in late if o.get("reason")),
                "late_without_reason": sum(1 for o in late if not o.get("reason")),
                "recent_alerts": alerts[:60]}

    @staticmethod
    def _focus(rules, restaurants, hours, riders, phases, single, double, by_kind, late, cause, districts, sessions_ok=False):
        focus = []
        for x in restaurants:
            if x["delivered"] >= 3 and x["avg_wait"] is not None and x["avg_wait"] >= rules.wait_restaurant_min:
                focus.append({"icon": "🏪", "title": f"{x['restaurant']}: riders wait {x['avg_wait']:.0f} min on average ({x['delivered']} orders, max {x['max_wait'] or 0:.0f})",
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
            focus.append({"icon": "🚪", "title": f"Handover at the customer takes {phases['handover'] or 0:.0f} min on average",
                          "action": "Riders should call the customer 2 min before arrival."})
        for x in riders:
            if x["delivered"] >= 3 and x["avg_ptod"] is not None and ((x["within_pct"] if x["within_pct"] is not None else 100) < 70 or x["avg_ptod"] > rules.ptod_target_min):
                slow = max((k for k in ("avg_accept", "avg_to_restaurant", "avg_wait", "avg_to_customer", "avg_handover")), key=lambda k: (x[k] or 0) - (phases[{"avg_accept": "to_accept", "avg_to_restaurant": "to_restaurant", "avg_wait": "at_restaurant", "avg_to_customer": "to_customer", "avg_handover": "handover"}[k]] or 0))
                focus.append({"icon": "🧑", "title": f"{x['rider']}: {x['within_pct']}% within target, avg PTOD {x['avg_ptod']:.0f} min — loses most time in {slow.replace('avg_', '').replace('_', ' ')}",
                              "action": "Coach with the numbers from the rider table (tap the name)."})
        for x in riders:
            if sessions_ok and x["online_minutes"] >= 120 and (x["utilisation_pct"] or 0) < 40 and x["delivered"] < 3:
                focus.append({"icon": "💤", "title": f"{x['rider']}: online {x['online_minutes'] / 60:.1f} h but only {x['delivered']} deliveries ({x['utilisation_pct']}% busy)",
                              "action": "Check whether this rider accepts orders; otherwise shift them to a busier hour."})
        for d in districts[:1]:
            if d["orders"] >= 4 and d["avg_ptod"] is not None and d["avg_ptod"] > rules.ptod_target_min:
                focus.append({"icon": "🗺", "title": f"Postcode {d['zip']}: avg PTOD {d['avg_ptod']:.0f} min over {d['orders']} orders",
                              "action": "Far district — position an idle rider nearby at peak, or dispatch earlier for these addresses."})
        if not focus and (single or double):
            focus.append({"icon": "✅", "title": "No structural problem found in this period", "action": "Keep the current setup; watch peak-hour acceptance times."})
        return focus[:7]

    # ================================================================ staffing plan
    def staffing_plan(self, now: datetime, rules, day: str = "", weeks: int = 4, city: str = "") -> dict:
        """Riders needed per hour on a given day (default: tomorrow), weekday-aware:
        a Saturday is planned from the previous Saturdays (up to `weeks` back); while fewer than 2 of that weekday
        are recorded, the last 7 days are used instead.  Per hour: orders (average / busiest day) ÷ capacity
        (orders one rider really delivers per hour, Settings); where acceptance was slow with N riders, at least N+1.
        Also returns the same summary for each of the next 7 days."""
        today = day_start(now)
        start = today - timedelta(days=weeks * 7 - 1)
        # aggregated in SQL: at 170 000 orders a month this must not touch rows one by one
        where = "day >= ? AND day <= ? AND dispatched_at IS NOT NULL AND phase != 'closed'"
        args = [day_key(start), day_key(now + timedelta(days=1))]
        if city:
            where += " AND city=?"; args.append(city)
        per_day = {}                                              # day_key -> hour -> aggregate dict
        for r in self._rows(f"SELECT day, hour, COUNT(*) AS n, COUNT(DISTINCT rider_id) AS riders, AVG(accept_min) AS accept, "
                            f"SUM(CASE WHEN accept_min >= ? OR (accepted_at IS NULL AND phase='cancelled') THEN 1 ELSE 0 END) AS no_rider, "
                            f"SUM(CASE WHEN phase='delivered' AND ptod_min <= ? THEN 1 ELSE 0 END) AS within, SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL THEN 1 ELSE 0 END) AS withn, "
                            f"SUM(CASE WHEN on_time=1 THEN 1 ELSE 0 END) AS ot, SUM(CASE WHEN on_time IS NOT NULL THEN 1 ELSE 0 END) AS otn "
                            f"FROM orders WHERE {where} GROUP BY day, hour", [rules.accept_limit_min, rules.ptod_target_min] + args):
            if r["day"] and r["hour"] is not None:
                per_day.setdefault(r["day"], {})[r["hour"]] = dict(r)
        today_key = day_key(now)
        cap = max(0.5, float(rules.riders_capacity_per_hour or 1.5))
        local_today = today.astimezone(BERLIN).date()

        def weekday_of(dk):
            return datetime.strptime(dk, "%Y-%m-%d").weekday()

        def plan_for(target):                                     # target: date (Berlin)
            same = sorted(dk for dk in per_day if weekday_of(dk) == target.weekday() and dk != today_key)
            if len(same) >= 2:
                basis, basis_label = same[-weeks:], f"the last {min(len(same), weeks)} {target.strftime('%A')}s"
            else:
                last7 = [dk for dk in per_day if dk >= day_key(today - timedelta(days=6))]
                basis, basis_label = sorted(last7), f"the last {len(last7)} days (no {target.strftime('%A')} recorded yet)" if last7 else "no data yet"
            n_days = max(1, len(basis))
            by_hour = {}
            for dk in basis:
                for h, agg in per_day.get(dk, {}).items():
                    by_hour.setdefault(h, {})[dk] = agg
            hours = []
            for h in sorted(by_hour, key=lambda x: (x - DAY_STARTS_AT) % 24):
                pd = by_hour[h]
                counts = [a["n"] for a in pd.values()]
                total = sum(counts)
                active = [a["riders"] for a in pd.values()]
                avg = total / n_days                                  # basis days without orders in this hour count as 0
                had = round(sum(active) / len(active), 1) if active else None
                acc_vals = [(a["accept"], a["n"]) for a in pd.values() if a["accept"] is not None]
                accept = round(sum(v * n for v, n in acc_vals) / sum(n for _, n in acc_vals), 1) if acc_vals else None
                no_rider = sum(a["no_rider"] or 0 for a in pd.values())
                slow = (accept or 0) >= 4 or no_rider >= max(2, total // 5)
                need = math.ceil(avg / cap)                            # throughput: orders ÷ what one rider delivers per hour
                plan = max(need, math.ceil(had or 0) + 1) if slow else need   # it was slow with N riders -> at least N+1
                within_n = sum(a["withn"] or 0 for a in pd.values()); ot_n = sum(a["otn"] or 0 for a in pd.values())
                hours.append({"hour": h, "avg_orders": round(avg, 1), "max_orders": max(counts), "riders_needed": need,
                              "riders_peak": math.ceil(max(counts) / cap), "riders_had": had, "avg_accept": accept,
                              "within_pct": _pct(sum(a["within"] or 0 for a in pd.values()), within_n), "on_time_pct": _pct(sum(a["ot"] or 0 for a in pd.values()), ot_n),
                              "no_rider": no_rider, "slow": slow, "plan": plan})
            peak = sorted(x["hour"] for x in sorted(hours, key=lambda x: -x["avg_orders"])[:3])
            return {"day": target.isoformat(), "weekday": target.strftime("%A"), "basis": basis_label, "basis_days": len(basis),
                    "same_weekday": len(same) >= 2, "hours": hours, "peak_hours": peak,
                    "rider_hours": sum(x["plan"] for x in hours), "riders_peak": max([x["plan"] for x in hours], default=0),
                    "orders_per_day": round(sum(a["n"] for dk in basis for a in per_day.get(dk, {}).values()) / n_days, 1)}

        try:
            target = datetime.strptime(day, "%Y-%m-%d").date() if day else local_today + timedelta(days=1)
        except ValueError:
            target = local_today + timedelta(days=1)
        out = plan_for(target)
        out["capacity"] = cap
        out["city"] = city
        out["tomorrow"] = (local_today + timedelta(days=1)).strftime("%A")
        out["week"] = []
        for i in range(1, 8):
            t = local_today + timedelta(days=i)
            w = plan_for(t)
            out["week"].append({k: w[k] for k in ("day", "weekday", "basis", "basis_days", "same_weekday", "peak_hours", "rider_hours", "riders_peak", "orders_per_day")})
        return out

    def staffing_csv(self, now: datetime, rules, city: str = "") -> str:
        """Next 7 days × hours: the riders to plan — paste into the shift sheet."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["city", "date", "weekday", "hour", "orders_avg", "orders_busiest_day", "riders_plan", "riders_at_peak", "based_on"])
        local_today = day_start(now).astimezone(BERLIN).date()
        for i in range(1, 8):
            d = (local_today + timedelta(days=i)).isoformat()
            pl = self.staffing_plan(now, rules, day=d, city=city)
            for h in pl["hours"]:
                w.writerow([city or "all", d, pl["weekday"], f"{h['hour']:02d}:00", h["avg_orders"], h["max_orders"], h["plan"], h["riders_peak"], pl["basis"]])
        return buf.getvalue()

    # ================================================================ 6.3 — shift sheet
    SHIFT_COLS = {"rider": ("rider", "name", "driver", "fahrer", "courier", "rider_name", "driver_name", "fahrername", "mitarbeiter"),
                  "id": ("rider_id", "driver_id", "id", "user_id", "motiontools_id"),
                  "day": ("date", "day", "datum", "tag", "shift_date"),
                  "start": ("start", "from", "von", "shift_start", "beginn", "start_time"),
                  "end": ("end", "to", "bis", "shift_end", "ende", "end_time"),
                  "city": ("city", "stadt", "area", "service_area"),
                  "fleet": ("fleet", "organization", "organisation", "org", "flotte", "partner")}

    @staticmethod
    def _norm_name(s: str) -> str:
        return " ".join(str(s or "").replace(",", " ").lower().split())

    def _name_index(self, extra: dict = None) -> dict:
        """normalised full name -> rider id (riders table + the live riders the app knows)."""
        idx = {}
        for r in self._rows("SELECT id, name FROM riders WHERE name IS NOT NULL AND name != ''"):
            idx[self._norm_name(r["name"])] = r["id"]
        for rid, name in (extra or {}).items():
            if name:
                idx[self._norm_name(name)] = rid
        swapped = {" ".join(reversed(k.split())): v for k, v in idx.items() if len(k.split()) == 2}   # "Last First"
        for k, v in swapped.items():
            idx.setdefault(k, v)
        return idx

    def shifts_import(self, text: str, now: datetime, known_names: dict = None) -> dict:
        """Shift sheet as CSV (comma or semicolon; Excel 'Save as CSV' works): one row per rider and day with
        rider (name or MotionTools id), date, start, end — optional city and fleet. Names are matched to the riders
        seen on orders; a name not seen yet is kept as the name and linked as soon as that rider delivers."""
        text = text.lstrip("﻿")
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
        if not rows:
            return {"ok": False, "error": "empty file"}
        head = [self._norm_name(h).replace(" ", "_") for h in rows[0]]
        col = {}
        for key, names in self.SHIFT_COLS.items():
            for i, h in enumerate(head):
                if h in names and key not in col:
                    col[key] = i
        if "day" not in col or ("rider" not in col and "id" not in col):
            return {"ok": False, "error": f"need columns rider (or rider_id), date, start, end — found: {', '.join(rows[0])}"}
        names = self._name_index(known_names)
        out, unmatched, bad = [], set(), 0
        for r in rows[1:]:
            if not any(x.strip() for x in r):
                continue
            def cell(k):
                i = col.get(k)
                return r[i].strip() if i is not None and i < len(r) else ""
            day = cell("day")
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
                try:
                    day = datetime.strptime(day, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            else:
                bad += 1
                continue
            def hhmm(v):
                v = v.strip()
                for fmt in ("%H:%M", "%H:%M:%S", "%H.%M", "%H"):
                    try:
                        return datetime.strptime(v, fmt).strftime("%H:%M")
                    except ValueError:
                        continue
                return v[:5]
            rid = cell("id")
            name = cell("rider")
            if not rid:
                rid = names.get(self._norm_name(name), "")
                if not rid:
                    rid = "?" + self._norm_name(name)
                    unmatched.add(name)
            out.append((rid, name or rid, day, hhmm(cell("start")), hhmm(cell("end")), cell("city"), cell("fleet") or self.fleet_map.get(rid, ""), iso(now)))
        with self.lock:
            self.db.executemany("INSERT INTO shifts (rider_id, rider, day, start, end, city, fleet, imported_at) VALUES (?,?,?,?,?,?,?,?) "
                                "ON CONFLICT(rider_id, day) DO UPDATE SET rider=excluded.rider, start=MIN(shifts.start, excluded.start), "
                                "end=MAX(shifts.end, excluded.end), city=CASE WHEN excluded.city!='' THEN excluded.city ELSE shifts.city END, "
                                "fleet=CASE WHEN excluded.fleet!='' THEN excluded.fleet ELSE shifts.fleet END, imported_at=excluded.imported_at", out)
            self.db.commit()
        days = sorted({x[2] for x in out})
        self.log("info", f"shift sheet imported: {len(out)} rider-days, {len(days)} days, {len(unmatched)} names not seen on orders yet")
        return {"ok": True, "rows": len(out), "days": days[:1] + days[-1:] if days else [], "unmatched": sorted(unmatched)[:50], "unmatched_n": len(unmatched), "skipped": bad}

    def shifts_relink(self, known_names: dict = None) -> int:
        """Shift rows that only had a name get the rider id once that rider shows up on an order."""
        names = self._name_index(known_names)
        n = 0
        for r in self._rows("SELECT DISTINCT rider_id, rider FROM shifts WHERE rider_id LIKE '?%'"):
            rid = names.get(self._norm_name(r["rider"]))
            if rid:
                with self.lock:
                    self.db.execute("DELETE FROM shifts WHERE rider_id=? AND day IN (SELECT day FROM shifts WHERE rider_id=?)", (r["rider_id"], rid))
                    self.db.execute("UPDATE shifts SET rider_id=? WHERE rider_id=?", (rid, r["rider_id"]))
                    self.db.commit()
                n += 1
        return n

    def shifts_status(self) -> dict:
        r = self._rows("SELECT COUNT(*) AS n, COUNT(DISTINCT day) AS days, MIN(day) AS first, MAX(day) AS last, MAX(imported_at) AS imported, "
                       "SUM(rider_id LIKE '?%') AS unmatched FROM shifts")[0]
        return {"rows": r["n"] or 0, "days": r["days"] or 0, "first": r["first"], "last": r["last"], "imported_at": r["imported"], "unmatched": r["unmatched"] or 0,
                "has_data": bool(r["n"])}

    def shifts_clear(self) -> int:
        return self._exec("DELETE FROM shifts").rowcount

    def shifts_between(self, d1: str, d2: str, city: str = "", fleet: str = "") -> list:
        where, args = "day >= ? AND day <= ?", [d1, d2]
        if city:
            where += " AND (city=? OR city='' OR city IS NULL)"; args.append(city)
        if fleet:
            where += " AND fleet=?"; args.append(fleet)
        return self._rows(f"SELECT * FROM shifts WHERE {where}", args)

    # ================================================================ 6.3 — rider performance (from orders + the sheet)
    @staticmethod
    def rider_score(r: dict, avg_per_h: float, rules) -> dict | None:
        """0–100, explainable. Customer 40: on time 25 · PTOD 10 · handover 5. Productivity 30: deliveries per working hour
        vs the average rider of the same period/city. Reliability 30: acceptance 12 · hand-backs 10 · shift sheet 8
        (planned days without an order, first order late). Needs 3 deliveries; a rider planned with no order at all = 0."""
        d = r.get("delivered") or 0
        if d < 3:
            if r.get("planned_days") and not d and not r.get("live"):
                return {"score": 0, "customer": 0, "productivity": 0, "reliability": 0}
            return None
        tgt = rules.ptod_target_min
        ot = r.get("on_time_pct")
        c_ot = 25 * (ot / 100) if ot is not None else 20
        p = r.get("avg_ptod")
        c_ptod = 10 if p is None or p <= tgt else max(0.0, 10 - (p - tgt) * (10 / 15))
        h = r.get("avg_handover")
        c_hand = 5 if h is None or h <= 3 else max(0.0, 5 - (h - 3))
        per_h = r.get("per_hour") or 0
        base = max(0.5, avg_per_h or rules.riders_capacity_per_hour or 1.5)
        prod = 30 * min(1.0, per_h / base)
        a = r.get("avg_accept")
        r_acc = 12 if a is None or a <= 2 else max(0.0, 12 - (a - 2) * 2)
        r_hb = max(0.0, 10 - 4 * (r.get("handbacks_bad") or 0))
        planned, worked = r.get("planned_days") or 0, r.get("planned_days_worked") or 0
        r_sheet = 8.0
        if planned:
            r_sheet = 8 * (worked / planned)
            if r.get("first_late_days"):
                r_sheet = max(0.0, r_sheet - 2 * r["first_late_days"])
        cust, prodv, rel = round(c_ot + c_ptod + c_hand), round(prod), round(r_acc + r_hb + r_sheet)
        return {"score": max(0, min(100, cust + prodv + rel)), "customer": cust, "productivity": prodv, "reliability": rel}

    def rider_stats(self, period: str, now: datetime, rules, city: str = "", fleet: str = "") -> dict:
        """Every rider with orders in the period (SQL-aggregated: never touches rows one by one):
        deliveries, usual (avg per worked day in the 4 weeks before), working hours (first accept → last delivery, per day),
        per working hour, on time, PTOD, accept, hand-backs (excused ones excluded), doubles, late for the customer,
        shift-sheet fulfilment, and the 0–100 score."""
        start, end = period_range(period, now)
        d1, d2 = day_key(start), day_key(min(end, now) - timedelta(seconds=1)) if end <= now else day_key(now)
        where, args = "day >= ? AND day <= ? AND rider_id IS NOT NULL AND rider_id != '' AND phase != 'closed'", [d1, d2]
        if city:
            where += " AND city=?"; args.append(city)
        if fleet:
            where += " AND fleet=?"; args.append(fleet)
        per_day = self._rows(
            "SELECT rider_id, MAX(rider) AS rider, MAX(city) AS city, MAX(fleet) AS fleet, day, "
            "SUM(phase='delivered') AS delivered, SUM(phase='cancelled') AS cancelled, SUM(closed=0 AND phase!='on_hold') AS live, "
            "MIN(COALESCE(accepted_at, dispatched_at)) AS first_at, MAX(COALESCE(delivered_at, cancelled_at)) AS last_at, "
            "MAX(updated_at) AS updated, "
            "SUM(CASE WHEN phase='delivered' THEN ptod_min END) AS ptod_sum, SUM(phase='delivered' AND ptod_min IS NOT NULL) AS ptod_n, "
            "SUM(phase='delivered' AND ptod_min <= ?) AS within, "
            "SUM(on_time=1) AS ot, SUM(on_time IS NOT NULL) AS otn, SUM(on_time=0) AS late, "
            "SUM(accept_min) AS acc_sum, SUM(accept_min IS NOT NULL) AS acc_n, SUM(stacked) AS dbl, "
            "SUM(CASE WHEN phase='delivered' AND at_customer_at IS NOT NULL THEN (julianday(delivered_at)-julianday(at_customer_at))*1440 END) AS hand_sum, "
            "SUM(phase='delivered' AND at_customer_at IS NOT NULL) AS hand_n, "
            "SUM(CASE WHEN phase='delivered' AND at_restaurant_at IS NOT NULL AND picked_up_at IS NOT NULL THEN (julianday(picked_up_at)-julianday(at_restaurant_at))*1440 END) AS wait_sum, "
            "SUM(phase='delivered' AND at_restaurant_at IS NOT NULL AND picked_up_at IS NOT NULL) AS wait_n "
            f"FROM orders WHERE {where} GROUP BY rider_id, day", [rules.ptod_target_min] + args)
        hb = {}
        hwhere, hargs = "day >= ? AND day <= ?", [d1, d2]
        if city:
            hwhere += " AND city=?"; hargs.append(city)
        for r in self._rows(f"SELECT rider_id, SUM(excused) AS exc, COUNT(*) AS n FROM handbacks WHERE {hwhere} GROUP BY rider_id", hargs):
            hb[r["rider_id"]] = (r["n"] or 0, r["exc"] or 0)
        # usual = deliveries per worked day in the 28 days before the period
        u1, u2 = day_key(start - timedelta(days=28)), day_key(start - timedelta(days=1))
        usual = {r["rider_id"]: round(r["n"] / r["days"], 1) for r in self._rows(
            "SELECT rider_id, COUNT(*) AS n, COUNT(DISTINCT day) AS days FROM orders WHERE phase='delivered' AND day >= ? AND day <= ? "
            "AND rider_id IS NOT NULL AND rider_id != '' GROUP BY rider_id", (u1, u2)) if r["days"]}
        shifts = {}
        for s in self.shifts_between(d1, d2, city=city, fleet=fleet):
            shifts.setdefault(s["rider_id"], {})[s["day"]] = s
        riders = {}
        today = day_key(now)
        for r in per_day:
            x = riders.setdefault(r["rider_id"], {"rider_id": r["rider_id"], "rider": r["rider"] or "Rider", "city": r["city"] or "", "fleet": r["fleet"] or "",
                                                   "delivered": 0, "cancelled": 0, "live": 0, "days": 0, "hours": 0.0, "ptod_sum": 0.0, "ptod_n": 0, "within": 0,
                                                   "ot": 0, "otn": 0, "late": 0, "acc_sum": 0.0, "acc_n": 0, "double": 0, "hand_sum": 0.0, "hand_n": 0,
                                                   "wait_sum": 0.0, "wait_n": 0, "first_at": None, "last_at": None, "first_late_days": 0, "planned_days_worked": 0, "day_rows": {}})
            x["delivered"] += r["delivered"] or 0; x["cancelled"] += r["cancelled"] or 0; x["live"] += r["live"] or 0
            cc = x.setdefault("cities", {}); cc[r["city"] or ""] = cc.get(r["city"] or "", 0) + (r["delivered"] or 0) + (r["live"] or 0) + (r["cancelled"] or 0)
            x["city"] = max(cc.items(), key=lambda kv: kv[1])[0]
            if r["fleet"]:
                x["fleet"] = r["fleet"]
            if r["delivered"] or r["live"]:
                x["days"] += 1
            f, l = ts(r["first_at"]), ts(r["last_at"])
            if r["live"] and r["day"] == today:
                l = max([t for t in (l, now) if t]) if l else now
            if f and l:
                x["hours"] += max(1.0, (l - f).total_seconds() / 3600 + 0.5)
            elif r["delivered"] or r["live"]:
                x["hours"] += 1.0
            for k in ("ptod_sum", "ptod_n", "within", "ot", "otn", "late", "acc_sum", "acc_n", "hand_sum", "hand_n", "wait_sum", "wait_n"):
                x[k] += r[k] or 0
            x["double"] += r["dbl"] or 0
            x["day_rows"][r["day"]] = {"delivered": r["delivered"] or 0, "first": f, "last": l}
            if f and (x["first_at"] is None or f < x["first_at"]):
                x["first_at"] = f
            if l and (x["last_at"] is None or l > x["last_at"]):
                x["last_at"] = l
            sh = shifts.get(r["rider_id"], {}).get(r["day"])
            if sh and (r["delivered"] or r["live"]):
                x["planned_days_worked"] += 1
                if f and sh.get("start"):
                    try:
                        st = datetime.strptime(f"{r['day']} {sh['start']}", "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
                        if (f - st).total_seconds() > 45 * 60:
                            x["first_late_days"] += 1
                    except ValueError:
                        pass
        # riders in the sheet who had no order at all in the period
        for rid, days in shifts.items():
            if rid not in riders:
                s0 = next(iter(days.values()))
                riders[rid] = {"rider_id": rid, "rider": s0["rider"] or "Rider", "city": s0.get("city") or city or "", "fleet": s0.get("fleet") or "",
                               "delivered": 0, "cancelled": 0, "live": 0, "days": 0, "hours": 0.0, "ptod_sum": 0.0, "ptod_n": 0, "within": 0,
                               "ot": 0, "otn": 0, "late": 0, "acc_sum": 0.0, "acc_n": 0, "double": 0, "hand_sum": 0.0, "hand_n": 0,
                               "wait_sum": 0.0, "wait_n": 0, "first_at": None, "last_at": None, "first_late_days": 0, "planned_days_worked": 0, "day_rows": {}}
        out = []
        for rid, x in riders.items():
            n, exc = hb.get(rid, (0, 0))
            sh = shifts.get(rid, {})
            today_shift = sh.get(today) or (sh.get(d2) if d1 == d2 else None)
            o = {"rider_id": rid, "rider": x["rider"], "city": x["city"], "fleet": x["fleet"] or self.fleet_map.get(rid, ""),
                 "delivered": x["delivered"], "cancelled": x["cancelled"], "live": x["live"], "days": x["days"],
                 "hours": round(x["hours"], 1), "per_hour": round(x["delivered"] / x["hours"], 1) if x["hours"] else None,
                 "usual": usual.get(rid), "within_pct": _pct(x["within"], x["ptod_n"]), "on_time_pct": _pct(x["ot"], x["otn"]), "late": x["late"],
                 "avg_ptod": round(x["ptod_sum"] / x["ptod_n"], 1) if x["ptod_n"] else None,
                 "avg_accept": round(x["acc_sum"] / x["acc_n"], 1) if x["acc_n"] else None,
                 "avg_handover": round(x["hand_sum"] / x["hand_n"], 1) if x["hand_n"] else None,
                 "avg_wait": round(x["wait_sum"] / x["wait_n"], 1) if x["wait_n"] else None,
                 "double": x["double"], "handbacks": n, "handbacks_excused": exc, "handbacks_bad": max(0, n - exc),
                 "first_at": iso(x["first_at"]), "last_at": iso(x["last_at"]),
                 "planned_days": len(sh), "planned_days_worked": x["planned_days_worked"], "first_late_days": x["first_late_days"],
                 "shift": f"{today_shift['start']}–{today_shift['end']}" if today_shift and today_shift.get("start") else ("" if not sh else "planned"),
                 "shift_start": today_shift.get("start") if today_shift else None, "shift_end": today_shift.get("end") if today_shift else None,
                 "in_sheet": bool(sh), "no_order_planned_days": max(0, len(sh) - x["planned_days_worked"])}
            o["usual_pct"] = round(100 * x["delivered"] / (usual[rid] * max(1, x["days"] or 1))) if usual.get(rid) and (x["delivered"] or x["days"]) else None
            out.append(o)
        scored = [r for r in out if (r["delivered"] or 0) >= 3 and r["per_hour"]]
        avg_per_h = (sum(r["per_hour"] for r in scored) / len(scored)) if scored else None
        for r in out:
            s = self.rider_score(r, avg_per_h, rules)
            r["score"] = s["score"] if s else None
            r["score_parts"] = s
        out.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -1), -r["delivered"], r["rider"]))
        return {"period": period, "city": city, "fleet": fleet, "from": d1, "to": d2, "riders": out, "avg_per_hour": round(avg_per_h, 2) if avg_per_h else None,
                "sheet": bool(shifts)}

    def rider_week_grid(self, now: datetime, city: str = "", fleet: str = "", days: int = 7, limit: int = 40) -> dict:
        """Deliveries per rider per day for the last `days` days — who works less at a glance."""
        d1 = day_key(now - timedelta(days=days - 1))
        where, args = "phase='delivered' AND day >= ? AND rider_id IS NOT NULL AND rider_id != ''", [d1]
        if city:
            where += " AND city=?"; args.append(city)
        if fleet:
            where += " AND fleet=?"; args.append(fleet)
        rows = self._rows(f"SELECT rider_id, MAX(rider) AS rider, MAX(city) AS city, day, COUNT(*) AS n FROM orders WHERE {where} GROUP BY rider_id, day", args)
        grid = {}
        for r in rows:
            g = grid.setdefault(r["rider_id"], {"rider_id": r["rider_id"], "rider": r["rider"], "city": r["city"], "days": {}, "total": 0})
            g["days"][r["day"]] = r["n"]; g["total"] += r["n"]
        keys = [day_key(now - timedelta(days=i)) for i in range(days - 1, -1, -1)]
        out = sorted(grid.values(), key=lambda g: -g["total"])[:limit]
        for g in out:
            g["cells"] = [g["days"].get(k, 0) for k in keys]
            g["worked"] = sum(1 for v in g["cells"] if v)
            del g["days"]
        return {"days": keys, "riders": out}

    def reliability_by_city(self, period: str, now: datetime) -> list:
        """Shift sheet vs orders per city: rider-days planned, with orders, fulfilment, first order late."""
        start, end = period_range(period, now)
        d1, d2 = day_key(start), day_key(min(end, now) - timedelta(seconds=1)) if end <= now else day_key(now)
        planned = self._rows("SELECT city, COUNT(*) AS n FROM shifts WHERE day >= ? AND day <= ? GROUP BY city", (d1, d2))
        if not planned:
            return []
        worked_any = {(r["rider_id"], r["day"]) for r in self._rows(
            "SELECT DISTINCT rider_id, day FROM orders WHERE day >= ? AND day <= ? AND rider_id IS NOT NULL AND rider_id != '' AND phase != 'closed'", (d1, d2))}
        deliv = {r["city"]: (r["n"], r["riders"]) for r in self._rows(
            "SELECT city, COUNT(*) AS n, COUNT(DISTINCT rider_id || day) AS riders FROM orders WHERE phase='delivered' AND day >= ? AND day <= ? GROUP BY city", (d1, d2))}
        out = []
        for p in planned:
            cityname = p["city"] or ""
            rows = self._rows("SELECT rider_id, day, start FROM shifts WHERE day >= ? AND day <= ? AND city=?", (d1, d2, cityname))
            with_orders = sum(1 for s in rows if (s["rider_id"], s["day"]) in worked_any)
            out.append({"city": cityname or "(no city in sheet)", "planned": p["n"], "with_orders": with_orders, "fulfilment_pct": _pct(with_orders, p["n"]),
                        "no_order": p["n"] - with_orders, "deliveries_per_rider_day": round(deliv.get(cityname, (0, 0))[0] / deliv[cityname][1], 1) if deliv.get(cityname, (0, 0))[1] else None})
        out.sort(key=lambda x: -x["planned"])
        return out

    def hourly_forecast(self, now: datetime, city: str = "", weeks: int = 4) -> dict:
        """Orders per hour today vs the average of the last `weeks` same weekdays (the forecast)."""
        today = day_key(now)
        local = day_start(now).astimezone(BERLIN).date()
        same = [(local - timedelta(days=7 * i)).isoformat() for i in range(1, weeks + 1)]
        where, args = "day IN (%s) AND hour IS NOT NULL AND phase != 'closed'" % ",".join("?" * (len(same) + 1)), same + [today]
        if city:
            where += " AND city=?"; args.append(city)
        rows = self._rows(f"SELECT day, hour, COUNT(*) AS n FROM orders WHERE {where} GROUP BY day, hour", args)
        fc, act, basis = [0.0] * 24, [0] * 24, set()
        for r in rows:
            if r["day"] == today:
                act[r["hour"]] = r["n"]
            else:
                fc[r["hour"]] += r["n"]; basis.add(r["day"])
        nb = max(1, len(basis))
        h_now = now.astimezone(BERLIN).hour
        last = same[0]
        last_rows = {r["hour"]: r["n"] for r in rows if r["day"] == last}
        order = lambda h: (h - DAY_STARTS_AT) % 24
        return {"hours": list(range(24)), "forecast": [round(v / nb, 1) for v in fc], "actual": act, "basis_days": len(basis),
                "weekday": local.strftime("%A"), "today_total": sum(act), "forecast_total": round(sum(fc) / nb), "hour_now": h_now,
                "last_same_day": last, "last_same_day_total": sum(last_rows.values()),
                "last_same_day_until_now": sum(n for h, n in last_rows.items() if order(h) <= order(h_now))}

    def delivered_on(self, day: str, city: str = "") -> int:
        where, args = "day=? AND phase='delivered'", [day]
        if city:
            where += " AND city=?"; args.append(city)
        return self._rows(f"SELECT COUNT(*) AS n FROM orders WHERE {where}", args)[0]["n"] or 0

    def cancelled_today(self, now: datetime, city: str = "") -> int:
        where, args = "day=? AND phase='cancelled'", [day_key(now)]
        if city:
            where += " AND city=?"; args.append(city)
        return self._rows(f"SELECT COUNT(*) AS n FROM orders WHERE {where}", args)[0]["n"] or 0

    # ================================================================ 6.3 — fleets
    def fleet_report(self, period: str, now: datetime, rules, city: str = "") -> dict:
        """Every fleet (MotionTools organization) on the same order-based metrics, plus a 14-day on-time trend and
        the phase minutes of each fleet vs the whole network."""
        rs = self.rider_stats(period, now, rules, city=city)
        riders = rs["riders"]
        start, end = period_range(period, now)
        d1, d2 = rs["from"], rs["to"]
        where, args = "day >= ? AND day <= ? AND phase='delivered'", [d1, d2]
        if city:
            where += " AND city=?"; args.append(city)
        phases_sql = ("AVG(accept_min) AS to_accept, "
                      "AVG(CASE WHEN at_restaurant_at IS NOT NULL THEN (julianday(at_restaurant_at)-julianday(COALESCE(accepted_at, dispatched_at)))*1440 END) AS to_restaurant, "
                      "AVG(CASE WHEN at_restaurant_at IS NOT NULL AND picked_up_at IS NOT NULL THEN (julianday(picked_up_at)-julianday(at_restaurant_at))*1440 END) AS at_restaurant, "
                      "AVG(CASE WHEN picked_up_at IS NOT NULL AND at_customer_at IS NOT NULL THEN (julianday(at_customer_at)-julianday(picked_up_at))*1440 END) AS to_customer, "
                      "AVG(CASE WHEN at_customer_at IS NOT NULL THEN (julianday(delivered_at)-julianday(at_customer_at))*1440 END) AS handover")
        net = self._rows(f"SELECT COUNT(*) AS n, AVG(ptod_min) AS ptod, SUM(on_time=1) AS ot, SUM(on_time IS NOT NULL) AS otn, {phases_sql}, "
                         f"COUNT(DISTINCT rider_id || day) AS rider_days FROM orders WHERE {where}", args)[0]
        by_fleet = {r["fleet"] or "": r for r in self._rows(
            f"SELECT fleet, COUNT(*) AS n, AVG(ptod_min) AS ptod, SUM(on_time=1) AS ot, SUM(on_time IS NOT NULL) AS otn, {phases_sql}, "
            f"COUNT(DISTINCT rider_id || day) AS rider_days, COUNT(DISTINCT rider_id) AS riders_active, GROUP_CONCAT(DISTINCT city) AS cities "
            f"FROM orders WHERE {where} GROUP BY fleet", args)}
        trend_rows = self._rows("SELECT fleet, day, SUM(on_time=1) AS ot, SUM(on_time IS NOT NULL) AS otn, COUNT(*) AS n FROM orders WHERE phase='delivered' AND day >= ? AND day <= ?"
                                + (" AND city=?" if city else "") + " GROUP BY fleet, day", [day_key(now - timedelta(days=13)), day_key(now)] + ([city] if city else []))
        days14 = [day_key(now - timedelta(days=i)) for i in range(13, -1, -1)]
        trend = {}
        for r in trend_rows:
            trend.setdefault(r["fleet"] or "", {})[r["day"]] = _pct(r["ot"] or 0, r["otn"] or 0)
        net_trend = {}
        for dk in days14:
            ot = sum((r["ot"] or 0) for r in trend_rows if r["day"] == dk); otn = sum((r["otn"] or 0) for r in trend_rows if r["day"] == dk)
            net_trend[dk] = _pct(ot, otn)
        total_riders = {}
        for r in self._rows("SELECT fleet, COUNT(*) AS n FROM riders WHERE fleet IS NOT NULL AND fleet != '' GROUP BY fleet"):
            total_riders[r["fleet"]] = r["n"]
        hb = {r["fleet"] or "": (r["n"] or 0, r["exc"] or 0) for r in self._rows(
            "SELECT fleet, COUNT(*) AS n, SUM(excused) AS exc FROM handbacks WHERE day >= ? AND day <= ?" + (" AND city=?" if city else "") + " GROUP BY fleet", [d1, d2] + ([city] if city else []))}
        prev = None
        if period == "week":
            prev = {r["rider_id"]: r["score"] for r in self.rider_stats("lastweek", now, rules, city=city)["riders"]}
        fleets = []
        for name, agg in by_fleet.items():
            if not name:
                continue
            frs = [r for r in riders if r["fleet"] == name]
            scores = [r["score"] for r in frs if r["score"] is not None]
            planned = sum(r["planned_days"] for r in frs); worked = sum(r["planned_days_worked"] for r in frs)
            n, exc = hb.get(name, (0, 0))
            fleets.append({"fleet": name, "cities": sorted((agg["cities"] or "").split(",")) if agg["cities"] else [],
                           "riders_active": agg["riders_active"] or 0, "riders_total": max(total_riders.get(name, 0), agg["riders_active"] or 0, len(frs)),
                           "delivered": agg["n"] or 0, "per_rider_day": round(agg["n"] / agg["rider_days"], 1) if agg["rider_days"] else None,
                           "on_time_pct": _pct(agg["ot"] or 0, agg["otn"] or 0), "avg_ptod": round(agg["ptod"], 1) if agg["ptod"] is not None else None,
                           "avg_accept": round(agg["to_accept"], 1) if agg["to_accept"] is not None else None,
                           "handbacks": n, "handbacks_excused": exc, "handback_pct": round(100 * n / agg["n"], 1) if agg["n"] else None,
                           "no_order_days_pct": _pct(planned - worked, planned) if planned else None, "planned_days": planned,
                           "score": round(sum(scores) / len(scores)) if scores else None,
                           "phases": {k: (round(agg[k], 1) if agg[k] is not None else None) for k in ("to_accept", "to_restaurant", "at_restaurant", "to_customer", "handover")},
                           "trend": [trend.get(name, {}).get(dk) for dk in days14],
                           "bands": {"90": sum(1 for s in scores if s >= 90), "80": sum(1 for s in scores if 80 <= s < 90), "70": sum(1 for s in scores if 70 <= s < 80),
                                     "60": sum(1 for s in scores if 60 <= s < 70), "low": sum(1 for s in scores if s < 60), "none": sum(1 for r in frs if r["score"] is None)},
                           "riders": [{**r, "prev_score": prev.get(r["rider_id"]) if prev else None} for r in frs]})
        fleets.sort(key=lambda f: -(f["score"] if f["score"] is not None else -1))
        unassigned = [r for r in riders if not r["fleet"]]
        return {"period": period, "city": city, "from": d1, "to": d2, "days": days14, "fleets": fleets,
                "network": {"delivered": net["n"] or 0, "on_time_pct": _pct(net["ot"] or 0, net["otn"] or 0),
                            "avg_ptod": round(net["ptod"], 1) if net["ptod"] is not None else None,
                            "per_rider_day": round(net["n"] / net["rider_days"], 1) if net["rider_days"] else None,
                            "phases": {k: (round(net[k], 1) if net[k] is not None else None) for k in ("to_accept", "to_restaurant", "at_restaurant", "to_customer", "handover")},
                            "trend": [net_trend.get(dk) for dk in days14], "avg_score": round(sum(r["score"] for r in riders if r["score"] is not None) / max(1, sum(1 for r in riders if r["score"] is not None))) if any(r["score"] is not None for r in riders) else None},
                "unassigned_riders": len(unassigned), "unassigned_delivered": sum(r["delivered"] for r in unassigned), "sheet": rs["sheet"]}

    # ================================================================ 6.3 — automation log (Intercom rules)
    def auto_log(self, now: datetime, rule: str, rider_id: str, rider: str, order_id: str, order_ref: str, text: str, mode: str, error: str = "") -> int:
        cur = self._exec("INSERT INTO auto_msgs (at, day, rule, rider_id, rider, order_id, order_ref, text, mode, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (iso(now), day_key(now), rule, rider_id, rider, order_id or "", order_ref or "", text, mode, error))
        return cur.lastrowid

    def auto_update(self, aid: int, mode: str, error: str = ""):
        self._exec("UPDATE auto_msgs SET mode=?, error=? WHERE id=?", (mode, error, aid))

    def auto_recent(self, limit: int = 40, rider_id: str = "", order_id: str = "") -> list:
        where, args = "1=1", []
        if rider_id:
            where += " AND rider_id=?"; args.append(rider_id)
        if order_id:
            where += " AND order_id=?"; args.append(order_id)
        return self._rows(f"SELECT * FROM auto_msgs WHERE {where} ORDER BY id DESC LIMIT ?", args + [limit])

    def auto_today(self, now: datetime) -> dict:
        """rider id -> messages today (scorecard excluded) — the daily cap."""
        return {r["rider_id"]: r["n"] for r in self._rows("SELECT rider_id, COUNT(*) AS n FROM auto_msgs WHERE day=? AND rule != 'scorecard' GROUP BY rider_id", (day_key(now),))}

    def auto_keys_since(self, since: datetime) -> set:
        return {(r["rule"], r["rider_id"], r["order_id"] or "") for r in self._rows("SELECT rule, rider_id, order_id FROM auto_msgs WHERE at >= ?", (iso(since),))}

    def auto_counts(self, now: datetime) -> dict:
        return {r["rule"]: {"today": r["n"], "sent": r["sent"]} for r in self._rows(
            "SELECT rule, COUNT(*) AS n, SUM(mode='sent') AS sent FROM auto_msgs WHERE day=? GROUP BY rule", (day_key(now),))}

    def delivered_by_rider(self, day: str) -> dict:
        return {r["rider_id"]: {"n": r["n"], "last": r["last"], "name": r["rider"]} for r in self._rows(
            "SELECT rider_id, MAX(rider) AS rider, COUNT(*) AS n, MAX(delivered_at) AS last FROM orders WHERE day=? AND phase='delivered' AND rider_id IS NOT NULL AND rider_id != '' GROUP BY rider_id", (day,))}

    # ================================================================ daily snapshot
    def save_daily(self, day: str, data: dict, city: str = ""):
        key = f"{day}#{city}" if city else day
        self._exec("INSERT INTO daily_stats (day, computed_at, data) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET "
                   "computed_at=excluded.computed_at, data=excluded.data", (key, iso(datetime.now(UTC)), json.dumps(data)))

    def daily(self, day: str, city: str = ""):
        rows = self._rows("SELECT data FROM daily_stats WHERE day=?", (f"{day}#{city}" if city else day,))
        return json.loads(rows[0]["data"]) if rows else None

    def daily_trend(self, days=14, city: str = "") -> list:
        if city:
            rows = self._rows("SELECT day, data FROM daily_stats WHERE day LIKE ? ORDER BY day DESC LIMIT ?", (f"%#{city}", days))
            for r in rows:
                r["day"] = r["day"].split("#")[0]
        else:
            rows = self._rows("SELECT day, data FROM daily_stats WHERE day NOT LIKE '%#%' ORDER BY day DESC LIMIT ?", (days,))
        out = []
        for r in rows:
            d = json.loads(r["data"])
            out.append({"day": r["day"], "delivered": d.get("delivered"), "within_pct": d.get("within_pct"),
                        "avg_ptod": d.get("avg_ptod"), "cancelled": d.get("cancelled"), "on_time_pct": d.get("on_time_pct")})
        return list(reversed(out))

    # ================================================================ export
    def export_csv(self, period: str, now: datetime, rules=None, city: str = "") -> str:
        rows = self.orders_in(period, now, city=city)
        tgt = rules.ptod_target_min if rules else 30
        grace = rules.plan_grace_min if rules else 5
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["order", "city", "fleet", "status", "rider", "restaurant", "customer", "postcode", "created", "dispatched", "accepted", "at_restaurant",
                    "picked_up", "at_customer", "delivered", "min_to_accept", "min_to_restaurant", "min_at_restaurant",
                    "min_to_customer", "min_handover", "ptod_min", f"within_{tgt}", "planned_delivery", "min_vs_plan", "on_time_plan",
                    "double_order", "redispatched", "riders_history", "reason", "note", "cancel_reason"])
        f = lambda v: v.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M") if v else ""
        for o in rows:
            p = o["phases"]
            hist = " > ".join(f"{h.get('what')} {h.get('rider') or ''}".strip() for h in (o.get("history") or []))
            w.writerow([o["ref"], o.get("city", ""), o.get("fleet", ""), o["phase"], o["rider"], o["restaurant"], o["customer_addr"], o.get("customer_zip", ""),
                        f(o.get("created_at")), f(o["dispatched_at"]), f(o["accepted_at"]), f(o["at_restaurant_at"]), f(o["picked_up_at"]),
                        f(o["at_customer_at"]), f(o["delivered_at"]), p["to_accept"], p["to_restaurant"], p["at_restaurant"],
                        p["to_customer"], p["handover"], p["ptod"], "" if p["ptod"] is None else ("yes" if p["ptod"] <= tgt else "no"),
                        f(o.get("promised_at")), p["vs_plan"],
                        "" if p["vs_plan"] is None else ("yes" if p["vs_plan"] <= grace else "no"),
                        "yes" if o["stacked"] else "no", o.get("reassigned") or 0, hist,
                        o.get("reason", ""), o.get("note", ""), o.get("cancel_reason", "")])
        return buf.getvalue()
