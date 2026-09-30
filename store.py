"""SQLite storage for incidents and deliveries (the dashboard reads from here)."""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta

from detector import BERLIN, UTC

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id TEXT, rider TEXT, phone TEXT, kind TEXT, ref TEXT,
  headline TEXT, order_ref TEXT, address TEXT, due TEXT, map_url TEXT,
  opened_at TEXT, updated_at TEXT, resolved_at TEXT, resolution TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc_open ON incidents(resolved_at);
CREATE INDEX IF NOT EXISTS ix_inc_driver ON incidents(driver_id, opened_at);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  driver_id TEXT, rider TEXT, completed_at TEXT, on_time INTEGER, ptod_min REAL
);
CREATE INDEX IF NOT EXISTS ix_del_time ON deliveries(completed_at);
"""


KINDS = ("ptod", "late", "off_route", "wait", "stationary", "gps")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class Store:
    """Implements the detector's sink interface and the dashboard queries."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(deliveries)")}
            if "ptod_min" not in cols:
                self.db.execute("ALTER TABLE deliveries ADD COLUMN ptod_min REAL")
            # after a restart the detector has forgotten open problems; close them cleanly
            self.db.execute("UPDATE incidents SET resolved_at=?, resolution='service restarted' "
                            "WHERE resolved_at IS NULL", (iso(datetime.now(UTC)),))
            self.db.commit()
        self.ids: dict[tuple, int] = {}

    # ---------- sink interface ----------
    def open(self, key, info, now):
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO incidents (driver_id, rider, phone, kind, ref, headline, order_ref, address, "
                "due, map_url, opened_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (info["driver_id"], info["rider"], info["phone"], info["kind"], key[2], info["headline"],
                 info["order_ref"], info["address"], info["due"], info["map_url"], iso(now), iso(now)))
            self.db.commit()
            self.ids[key] = cur.lastrowid

    def update(self, key, info, now):
        iid = self.ids.get(key)
        if iid is None:
            return
        with self.lock:
            self.db.execute("UPDATE incidents SET headline=?, map_url=?, rider=?, phone=?, updated_at=? "
                            "WHERE id=?", (info["headline"], info["map_url"], info["rider"], info["phone"],
                                           iso(now), iid))
            self.db.commit()

    def resolve(self, key, why, now):
        iid = self.ids.pop(key, None)
        if iid is None:
            return
        with self.lock:
            self.db.execute("UPDATE incidents SET resolved_at=?, resolution=? WHERE id=?",
                            (iso(now), why, iid))
            self.db.commit()

    def delivery(self, driver_id, name, on_time, ptod_min, now):
        with self.lock:
            self.db.execute("INSERT INTO deliveries (driver_id, rider, completed_at, on_time, ptod_min) "
                            "VALUES (?,?,?,?,?)", (driver_id, name, iso(now), 1 if on_time else 0,
                                                   round(ptod_min, 1) if ptod_min is not None else None))
            self.db.commit()

    # ---------- dashboard queries ----------
    def _rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def live(self, now: datetime):
        """Open problems + anything resolved in the last 30 minutes."""
        since = iso(now - timedelta(minutes=30))
        return self._rows("SELECT * FROM incidents WHERE resolved_at IS NULL OR resolved_at >= ? "
                          "ORDER BY (resolved_at IS NULL) DESC, opened_at DESC LIMIT 60", (since,))

    @staticmethod
    def period_start(period: str, now: datetime) -> datetime:
        local = now.astimezone(BERLIN)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "week":
            day -= timedelta(days=local.weekday())
        elif period == "month":
            day = day.replace(day=1)
        return day.astimezone(UTC)

    def performance(self, period: str, now: datetime):
        start = iso(self.period_start(period, now))
        riders: dict[str, dict] = {}

        def row(did, name):
            r = riders.setdefault(did, {"driver_id": did, "rider": name, "deliveries": 0, "on_time": 0,
                                        "avg_ptod": None, **{k: 0 for k in KINDS}})
            if name and name != "Unknown rider":
                r["rider"] = name
            return r

        for d in self._rows("SELECT driver_id, MAX(rider) AS rider, COUNT(*) AS n, SUM(on_time) AS ok, "
                            "AVG(ptod_min) AS avg_ptod FROM deliveries WHERE completed_at >= ? "
                            "GROUP BY driver_id", (start,)):
            r = row(d["driver_id"], d["rider"])
            r["deliveries"], r["on_time"] = d["n"], d["ok"] or 0
            r["avg_ptod"] = round(d["avg_ptod"]) if d["avg_ptod"] is not None else None
        for i in self._rows("SELECT driver_id, MAX(rider) AS rider, kind, COUNT(*) AS n FROM incidents "
                            "WHERE opened_at >= ? AND COALESCE(resolution,'') != 'service restarted' "
                            "GROUP BY driver_id, kind", (start,)):
            if i["kind"] in KINDS:
                row(i["driver_id"], i["rider"])[i["kind"]] = i["n"]

        out = list(riders.values())
        for r in out:
            r["on_time_pct"] = round(100 * r["on_time"] / r["deliveries"]) if r["deliveries"] else None
            r["issues"] = sum(r[k] for k in KINDS)
        # best first: highest on-time %, then fewest issues, then most deliveries
        out.sort(key=lambda r: (-(r["on_time_pct"] if r["on_time_pct"] is not None else -1),
                                r["issues"], -r["deliveries"]))
        return out

    def rider_history(self, driver_id: str, now: datetime, days: int = 30):
        since = iso(now - timedelta(days=days))
        return self._rows("SELECT * FROM incidents WHERE driver_id=? AND opened_at >= ? "
                          "ORDER BY opened_at DESC LIMIT 200", (driver_id, since))
