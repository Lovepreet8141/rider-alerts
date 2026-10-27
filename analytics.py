"""Data for the premium dashboard (/v2): Performance, Restaurants, Riders & fleets.

Every number comes from SQL aggregates on the stored orders (indexed by day and city), so a day, a week or a month
answers in milliseconds-to-a-second without loading orders one by one. Live parts (riders right now) are added by
app.py from memory. No customer data leaves here.
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
import threading
import time
from datetime import date, datetime, timedelta

from orders import BERLIN
from store import day_key

# ------------------------------------------------------------------ periods
_NAMES = {"today": "Today", "yesterday": "Yesterday", "week": "This week", "lastweek": "Last week", "month": "This month"}


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _fmt(d: date, year: bool = False) -> str:
    return d.strftime("%a %d %b" + (" %Y" if year else "")).replace(" 0", " ")


def resolve(period: str, now: datetime, frm: str = "", to: str = "") -> dict:
    """The operating days of the period and of the period it is compared with (same length, just before /
    same weekday last week). Days are Berlin operating days (YYYY-MM-DD), as stored in orders.day."""
    today = _d(day_key(now))
    if frm and to:
        a, b = sorted((_d(frm), _d(to)))
        b = min(b, today)
        n = (b - a).days + 1
        pa, pb = a - timedelta(days=n), a - timedelta(days=1)
        label = f"{_fmt(a)} – {_fmt(b)}"
    elif len(period) == 10 and period[4] == "-":
        a = b = min(_d(period), today)
        pa = pb = a - timedelta(days=7)
        label = _fmt(a, True)
    elif period == "7d":
        a, b = today - timedelta(days=7), today - timedelta(days=1)
        pa, pb = a - timedelta(days=7), b - timedelta(days=7)
        label = f"Last 7 days · {_fmt(a)} – {_fmt(b)}"
    elif period == "yesterday":
        a = b = today - timedelta(days=1)
        pa = pb = a - timedelta(days=7)
        label = "Yesterday · " + _fmt(a)
    elif period == "week":
        a, b = today - timedelta(days=today.weekday()), today
        pa, pb = a - timedelta(days=7), b - timedelta(days=7)
        label = f"This week · {_fmt(a)} – {_fmt(b)}"
    elif period == "lastweek":
        a = today - timedelta(days=today.weekday() + 7)
        b = a + timedelta(days=6)
        pa, pb = a - timedelta(days=7), b - timedelta(days=7)
        label = f"Last week · {_fmt(a)} – {_fmt(b)}"
    elif period == "month":
        a, b = today.replace(day=1), today
        pm_end = a - timedelta(days=1)
        pa = pm_end.replace(day=1)
        pb = min(pa + timedelta(days=(b - a).days), pm_end)
        label = "This month · " + a.strftime("%B %Y")
    else:                                                       # today
        a = b = today
        pa = pb = a - timedelta(days=7)
        label = "Today · " + _fmt(a)
    same_len = (b - a).days == (pb - pa).days
    cmp_label = (("same day last week · " + _fmt(pa)) if a == b else
                 (f"{_fmt(pa)} – {_fmt(pb)}" + ("" if same_len else " (same days)")))
    return {"a": a.isoformat(), "b": b.isoformat(), "pa": pa.isoformat(), "pb": pb.isoformat(), "days": (b - a).days + 1,
            "label": label, "compare_label": cmp_label, "single": a == b, "has_today": b == today, "today": today.isoformat()}


def _where(a: str, b: str, city: str = "", fleet: str = "") -> tuple:
    w, args = "day BETWEEN ? AND ? AND phase != 'closed'", [a, b]
    if city:
        w += " AND city=?"; args.append(city)
    if fleet:
        w += " AND fleet=?"; args.append(fleet)
    return w, args


_MIN = "(julianday({b}) - julianday({a})) * 1440"
_SANE = "CASE WHEN {a} IS NOT NULL AND {b} IS NOT NULL AND " + _MIN + " BETWEEN 0 AND 180 THEN " + _MIN + " END"


def _span(a: str, b: str) -> str:
    return _SANE.format(a=a, b=b)


# ------------------------------------------------------------------ small cache (per process)
_CACHE: dict = {}


def cached(key, ttl: float, fn):
    hit = _CACHE.get(key)
    t = time.time()
    if hit and t - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (t, val)
    if len(_CACHE) > 300:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:100]:
            _CACHE.pop(k, None)
    return val


# ------------------------------------------------------------------ Performance
def _kpis(rows, rules, ndays: int) -> dict:
    r = rows[0] if rows else {}
    g = lambda k: r.get(k) or 0
    delivered, ptod_n, otn = g("delivered"), g("ptod_n"), g("otn")
    return {
        "orders": g("orders"), "delivered": delivered, "cancelled": g("cancelled"),
        "cancel_pct": round(100 * g("cancelled") / g("orders"), 1) if g("orders") else None,
        "avg_ptod": round(r["avg_ptod"], 1) if r.get("avg_ptod") is not None else None,
        "within_pct": round(100 * g("within") / ptod_n) if ptod_n else None,
        "on_time_pct": round(100 * g("ot") / otn) if otn else None,
        "avg_kitchen": round(r["avg_kitchen"], 1) if r.get("avg_kitchen") is not None else None,
        "long_waits": g("long_waits"), "long_pct": round(100 * g("long_waits") / delivered) if delivered else None,
        "riders": g("riders"), "per_rider": round(delivered / g("rider_days"), 1) if g("rider_days") else None,
        "per_day": round(delivered / max(1, ndays)),
    }


_KPI_SQL = ("SELECT COUNT(*) AS orders, SUM(phase='delivered') AS delivered, SUM(phase='cancelled') AS cancelled, "
            "AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod, "
            "SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL AND ptod_min <= ? THEN 1 ELSE 0 END) AS within, "
            "SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL THEN 1 ELSE 0 END) AS ptod_n, "
            "SUM(CASE WHEN on_time=1 THEN 1 ELSE 0 END) AS ot, SUM(CASE WHEN on_time IS NOT NULL THEN 1 ELSE 0 END) AS otn, "
            "AVG(CASE WHEN phase='delivered' THEN kitchen_wait END) AS avg_kitchen, "
            "SUM(CASE WHEN phase='delivered' AND kitchen_wait >= 10 THEN 1 ELSE 0 END) AS long_waits, "
            "COUNT(DISTINCT CASE WHEN phase='delivered' THEN rider_id END) AS riders, "
            "COUNT(DISTINCT CASE WHEN phase='delivered' THEN day || '|' || rider_id END) AS rider_days "
            "FROM orders WHERE {w}")


def performance_sql(store, rules, now: datetime, period: str = "today", city: str = "", frm: str = "", to: str = "") -> dict:
    """The first version: every number straight from SQL over the whole range (kept to check the day cube against)."""
    P = resolve(period, now, frm, to)
    tgt = rules.ptod_target_min
    w, args = _where(P["a"], P["b"], city)
    pw, pargs = _where(P["pa"], P["pb"], city)
    if P["has_today"]:
        # today is not over: compare with the earlier period only up to the same time of day on its last day
        from store import period_range
        from orders import iso
        t0 = period_range(P["today"], now)[0]
        p0 = period_range(P["pb"], now)[0]
        pw += " AND (day < ? OR COALESCE(dispatched_at, first_seen) < ?)"
        pargs += [P["pb"], iso(p0 + (now - t0))]
        P["compare_label"] += " · until " + now.astimezone(BERLIN).strftime("%H:%M")
    q = store._rows
    cur = _kpis(q(_KPI_SQL.format(w=w), [tgt] + args), rules, P["days"])
    prev = _kpis(q(_KPI_SQL.format(w=pw), [tgt] + pargs), rules, P["days"])

    # daily series: sparklines (last 14 days) and the 30-day trend, ending with the period
    end = _d(P["b"])
    s30 = (end - timedelta(days=29)).isoformat()
    sw, sargs = _where(s30, P["b"], city)
    daily = q("SELECT day, SUM(phase='delivered') AS delivered, SUM(phase='cancelled') AS cancelled, COUNT(*) AS orders, "
              "AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod, "
              "SUM(CASE WHEN phase='delivered' AND ptod_min <= ? THEN 1 ELSE 0 END) AS within, "
              "SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL THEN 1 ELSE 0 END) AS ptod_n, "
              "SUM(CASE WHEN on_time=1 THEN 1 ELSE 0 END) AS ot, SUM(CASE WHEN on_time IS NOT NULL THEN 1 ELSE 0 END) AS otn, "
              "AVG(CASE WHEN phase='delivered' THEN kitchen_wait END) AS avg_kitchen, "
              "COUNT(DISTINCT CASE WHEN phase='delivered' THEN rider_id END) AS riders "
              f"FROM orders WHERE {sw} GROUP BY day ORDER BY day", [tgt] + sargs)
    by_day = {r["day"]: r for r in daily}
    trend = []
    for i in range(30):
        d = (end - timedelta(days=29 - i)).isoformat()
        r = by_day.get(d) or {}
        trend.append({
            "day": d, "label": _fmt(_d(d)),
            "delivered": r.get("delivered") or 0,
            "avg_ptod": round(r["avg_ptod"], 1) if r.get("avg_ptod") is not None else None,
            "within_pct": round(100 * r["within"] / r["ptod_n"]) if r.get("ptod_n") else None,
            "on_time_pct": round(100 * r["ot"] / r["otn"]) if r.get("otn") else None,
            "cancel_pct": round(100 * r["cancelled"] / r["orders"], 1) if r.get("orders") else None,
            "avg_kitchen": round(r["avg_kitchen"], 1) if r.get("avg_kitchen") is not None else None,
            "riders": r.get("riders") or 0,
        })

    # hour by hour (average day of the period): orders, riders handling them, late
    hours = []
    hrs = {r["hour"]: r for r in q(
        "SELECT hour, COUNT(*) AS n, COUNT(DISTINCT day || '|' || rider_id) AS rd, "
        "SUM(CASE WHEN phase='delivered' AND ptod_min > ? THEN 1 ELSE 0 END) AS late, "
        "SUM(phase='delivered') AS delivered, AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod "
        f"FROM orders WHERE {w} AND hour IS NOT NULL GROUP BY hour", [tgt] + args)}
    cap = getattr(rules, "riders_capacity_per_hour", 1.5) or 1.5
    for h in range(9, 24):
        r = hrs.get(h)
        if not r and not (10 <= h <= 22):
            continue
        n = (r or {}).get("n") or 0
        riders = ((r or {}).get("rd") or 0)
        hours.append({"hour": h, "orders": round(n / P["days"], 1), "riders": round(riders / P["days"], 1),
                      "capacity": round(riders * cap / P["days"], 1), "late": (r or {}).get("late") or 0,
                      "late_pct": round(100 * r["late"] / r["delivered"]) if r and r.get("delivered") else None,
                      "avg_ptod": round(r["avg_ptod"], 1) if r and r.get("avg_ptod") is not None else None})

    # where the minutes go
    ph = (q("SELECT AVG(CASE WHEN accept_min BETWEEN 0 AND 180 THEN accept_min END) AS accept, "
            f"AVG({_span('accepted_at', 'at_restaurant_at')}) AS to_rest, "
            "AVG(CASE WHEN kitchen_wait BETWEEN 0 AND 180 THEN kitchen_wait END) AS wait, "
            f"AVG({_span('picked_up_at', 'at_customer_at')}) AS to_cust, "
            f"AVG({_span('at_customer_at', 'delivered_at')}) AS handover, AVG(ptod_min) AS ptod "
            f"FROM orders WHERE {w} AND phase='delivered'", args) or [{}])[0]
    phases = [{"key": k, "label": lbl, "min": round(ph[k], 1) if ph.get(k) is not None else None} for k, lbl in (
        ("accept", "Rider accepts"), ("to_rest", "Ride to the restaurant"), ("wait", "Waiting at the restaurant"),
        ("to_cust", "Ride to the customer"), ("handover", "Hand-over"))]

    # cities — always every city (a selected city is highlighted on the page, so it can be compared with the rest)
    aw, aargs = _where(P["a"], P["b"])
    cities = [{"city": r["city"] or "?", "delivered": r["delivered"] or 0, "cancelled": r["cancelled"] or 0,
               "avg_ptod": round(r["avg_ptod"], 1) if r["avg_ptod"] is not None else None,
               "within_pct": round(100 * r["within"] / r["ptod_n"]) if r["ptod_n"] else None,
               "on_time_pct": round(100 * r["ot"] / r["otn"]) if r["otn"] else None,
               "avg_kitchen": round(r["avg_kitchen"], 1) if r["avg_kitchen"] is not None else None}
              for r in q("SELECT city, SUM(phase='delivered') AS delivered, SUM(phase='cancelled') AS cancelled, "
                         "AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod, "
                         "SUM(CASE WHEN phase='delivered' AND ptod_min <= ? THEN 1 ELSE 0 END) AS within, "
                         "SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL THEN 1 ELSE 0 END) AS ptod_n, "
                         "SUM(CASE WHEN on_time=1 THEN 1 ELSE 0 END) AS ot, SUM(CASE WHEN on_time IS NOT NULL THEN 1 ELSE 0 END) AS otn, "
                         "AVG(CASE WHEN phase='delivered' THEN kitchen_wait END) AS avg_kitchen "
                         f"FROM orders WHERE {aw} GROUP BY city", [tgt] + aargs)
              if (r["delivered"] or 0) > 0]
    cities.sort(key=lambda c: -(c["avg_ptod"] or 0))

    # heatmap: weekday × hour, % late — the 4 weeks ending with the period
    h28 = (end - timedelta(days=27)).isoformat()
    hw, hargs = _where(h28, P["b"], city)
    heat = {}
    for r in q("SELECT CAST(strftime('%w', day) AS INTEGER) AS wd, hour, SUM(phase='delivered') AS n, "
               "SUM(CASE WHEN phase='delivered' AND ptod_min > ? THEN 1 ELSE 0 END) AS late "
               f"FROM orders WHERE {hw} AND hour IS NOT NULL GROUP BY wd, hour", [tgt] + hargs):
        heat[(r["wd"], r["hour"])] = (r["n"] or 0, r["late"] or 0)
    heat_rows = []
    for wd, name in ((1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (0, "Sun")):
        cells = []
        for h in range(11, 23):
            n, late = heat.get((wd, h), (0, 0))
            cells.append({"hour": h, "n": n, "pct": round(100 * late / n) if n >= 5 else None})
        heat_rows.append({"day": name, "cells": cells})

    # why late: one main cause per late order
    causes = {r["cause"]: r["n"] for r in q(
        "SELECT CASE "
        "WHEN kitchen_wait >= 10 THEN 'kitchen' "
        "WHEN accept_min >= 5 THEN 'accept' "
        "WHEN id IN (SELECT order_id FROM handbacks WHERE day BETWEEN ? AND ?) THEN 'handback' "
        f"WHEN {_span('picked_up_at', 'at_customer_at')} >= 20 THEN 'ride' "
        f"WHEN {_span('at_customer_at', 'delivered_at')} >= 5 THEN 'customer' "
        f"WHEN {_span('accepted_at', 'at_restaurant_at')} >= 15 THEN 'to_rest' "
        "ELSE 'other' END AS cause, COUNT(*) AS n "
        f"FROM orders WHERE {w} AND phase='delivered' AND ptod_min > ? GROUP BY cause",
        [P["a"], P["b"]] + args + [tgt])}
    total_late = sum(causes.values())
    reasons = [{"key": k, "label": lbl, "n": causes.get(k, 0), "pct": round(100 * causes.get(k, 0) / total_late) if total_late else 0}
               for k, lbl in (("kitchen", "Kitchen not ready (rider waited 10+ min)"), ("accept", "No rider accepted for 5+ min"),
                              ("handback", "A rider handed the order back"), ("ride", "Long ride to the customer (20+ min)"),
                              ("to_rest", "Long ride to the restaurant (15+ min)"), ("customer", "Waited at the customer (5+ min)"),
                              ("other", "Several small delays"))]
    reasons.sort(key=lambda x: -x["n"])

    return {"period": P, "target": tgt, "target_within_pct": rules.target_within_pct, "plan_grace": rules.plan_grace_min,
            "kpis": cur, "prev": prev, "trend": trend, "hours": hours, "capacity_per_hour": cap, "phases": phases,
            "ptod_avg": round(ph["ptod"], 1) if ph.get("ptod") is not None else None, "cities": cities,
            "heat": heat_rows, "reasons": reasons, "late_total": total_late}


# ====================================================================== the day cube
# A finished day never changes, so it is aggregated ONCE (per city × hour: counts, sums of minutes, late causes,
# riders) and kept in memory and in the day_cube table. A month, a city, a comparison, the 30-day trend and the
# 4-week heatmap are then sums of ~30 small tables in Python — milliseconds, no matter how many orders are stored.
# Only today is read from the orders table (one day, indexed), and refreshed every 20 s.
CUBE_V = 1
_CUBE: dict = {}                 # (kind, day, param) -> (monotonic time, data)
_CLOCKS: dict = {}
_CGUARD = threading.Lock()

_DAY_EXPR = [
    ("orders", "COUNT(*)"), ("delivered", "SUM(phase='delivered')"), ("cancelled", "SUM(phase='cancelled')"),
    ("ptod_s", "SUM(CASE WHEN phase='delivered' THEN ptod_min END)"), ("ptod_n", "SUM(phase='delivered' AND ptod_min IS NOT NULL)"),
    ("within", "SUM(phase='delivered' AND ptod_min <= :t)"), ("late", "SUM(phase='delivered' AND ptod_min > :t)"),
    ("ot", "SUM(on_time=1)"), ("otn", "SUM(on_time IS NOT NULL)"),
    ("kit_s", "SUM(CASE WHEN phase='delivered' THEN kitchen_wait END)"), ("kit_n", "SUM(phase='delivered' AND kitchen_wait IS NOT NULL)"),
    ("long", "SUM(phase='delivered' AND kitchen_wait >= 10)"), ("rider_h", "COUNT(DISTINCT rider_id)"),
    ("acc_s", "SUM(CASE WHEN phase='delivered' AND accept_min BETWEEN 0 AND 180 THEN accept_min END)"),
    ("acc_n", "SUM(phase='delivered' AND accept_min BETWEEN 0 AND 180)"),
    ("tr_s", "SUM(tr)"), ("tr_n", "COUNT(tr)"),
    ("wt_s", "SUM(CASE WHEN phase='delivered' AND kitchen_wait BETWEEN 0 AND 180 THEN kitchen_wait END)"),
    ("wt_n", "SUM(phase='delivered' AND kitchen_wait BETWEEN 0 AND 180)"),
    ("tc_s", "SUM(tc)"), ("tc_n", "COUNT(tc)"), ("ho_s", "SUM(ho)"), ("ho_n", "COUNT(ho)"),
    ("c_kitchen", "SUM(cause='kitchen')"), ("c_accept", "SUM(cause='accept')"), ("c_handback", "SUM(cause='handback')"),
    ("c_ride", "SUM(cause='ride')"), ("c_to_rest", "SUM(cause='to_rest')"), ("c_customer", "SUM(cause='customer')"), ("c_other", "SUM(cause='other')"),
]
F = [n for n, _ in _DAY_EXPR]
I = {n: i for i, n in enumerate(F)}
_NF = len(F)


def _day_sql(cut: bool) -> str:
    sel = ", ".join(f"{e} AS {n}" for n, e in _DAY_EXPR)
    return (f"SELECT city, hour, {sel} FROM ("
            " SELECT *, CASE WHEN phase='delivered' AND ptod_min > :t THEN"
            "  CASE WHEN kitchen_wait >= 10 THEN 'kitchen' WHEN accept_min >= 5 THEN 'accept' WHEN hb THEN 'handback'"
            "   WHEN tc >= 20 THEN 'ride' WHEN ho >= 5 THEN 'customer' WHEN tr >= 15 THEN 'to_rest' ELSE 'other' END END AS cause"
            " FROM ("
            "  SELECT city, hour, phase, ptod_min, on_time, kitchen_wait, accept_min, rider_id,"
            f"   CASE WHEN phase='delivered' THEN {_span('accepted_at', 'at_restaurant_at')} END AS tr,"
            f"   CASE WHEN phase='delivered' THEN {_span('picked_up_at', 'at_customer_at')} END AS tc,"
            f"   CASE WHEN phase='delivered' THEN {_span('at_customer_at', 'delivered_at')} END AS ho,"
            "   id IN (SELECT order_id FROM handbacks WHERE day = :d) AS hb"
            "  FROM orders WHERE day = :d AND phase != 'closed'"
            + (" AND COALESCE(dispatched_at, first_seen) < :cut" if cut else "") +
            " )) GROUP BY city, hour")


def _cube_table(store):
    if getattr(store, "_cube_ok", False):
        return
    try:
        store._exec("CREATE TABLE IF NOT EXISTS day_cube (kind TEXT, day TEXT, param TEXT, v INTEGER, data TEXT, "
                    "computed_at REAL, PRIMARY KEY (kind, day, param))")
        store._cube_ok = True
    except Exception:
        pass


def _cube_load(store, kind, day, param):
    _cube_table(store)
    try:
        r = store._rows("SELECT v, data, computed_at FROM day_cube WHERE kind=? AND day=? AND param=?", (kind, day, param))
    except Exception:
        return None
    if r and r[0]["v"] == CUBE_V and time.time() - (r[0]["computed_at"] or 0) < 86400:
        try:
            return json.loads(r[0]["data"])
        except Exception:
            return None
    return None


def _cube_save(store, kind, day, param, data):
    _cube_table(store)
    try:
        store._exec("INSERT OR REPLACE INTO day_cube (kind, day, param, v, data, computed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, day, param, CUBE_V, json.dumps(data, separators=(",", ":")), time.time()))
    except Exception:
        pass


def _yesterday(today: str) -> str:
    return (_d(today) - timedelta(days=1)).isoformat()


def cube(store, kind: str, day: str, param: str, today: str, build, ttl_today: float = 20):
    """One day's aggregate: today every ttl_today s, yesterday every 10 min, older days once (kept 24 h on disk)."""
    key = (kind, day, param)
    cutting = "cut:" in param
    stable = day < _yesterday(today) and not cutting
    ttl = ttl_today if day >= today else (120 if cutting else 600 if not stable else 6 * 3600)
    hit = _CUBE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    with _CGUARD:
        lk = _CLOCKS.setdefault(key, threading.Lock())
    with lk:
        hit = _CUBE.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        data = _cube_load(store, kind, day, param) if stable else None
        if data is None:
            data = build()
            if stable:
                _cube_save(store, kind, day, param, data)
        _CUBE[key] = (time.monotonic(), data)
        if len(_CUBE) > 400:
            old = (_d(today) - timedelta(days=75)).isoformat()
            for k in [k for k, v in _CUBE.items() if k[1] < old or ("cut:" in k[2] and time.monotonic() - v[0] > 600)]:
                _CUBE.pop(k, None); _CLOCKS.pop(k, None)
        return data


def perf_day(store, rules, day: str, today: str, cut: str = "") -> dict:
    """{"rows": [[city, hour, *F]], "tot": {city: [*F]}, "riders": {city: [rider ids that delivered]}} for one day."""
    t = rules.ptod_target_min

    def build():
        args = {"t": t, "d": day, "cut": cut}
        rows, tot = [], {}
        for r in store._rows(_day_sql(bool(cut)), args):
            vals = [r[n] or 0 for n in F]
            c = r["city"] or "?"
            rows.append([c, r["hour"]] + vals)
            acc = tot.setdefault(c, [0] * _NF)
            for i, v in enumerate(vals):
                acc[i] += v
        riders = {}
        for r in store._rows("SELECT city, rider_id FROM orders WHERE day = :d AND phase='delivered' AND rider_id IS NOT NULL AND rider_id != ''"
                             + (" AND COALESCE(dispatched_at, first_seen) < :cut" if cut else "") + " GROUP BY city, rider_id", args):
            riders.setdefault(r["city"] or "?", []).append(r["rider_id"])
        return {"rows": rows, "tot": tot, "riders": riders}
    return cube(store, "perf", day, f"t{t}" + (f"|cut:{cut}" if cut else ""), today, build)


def _days(a: str, b: str) -> list:
    out, d, e = [], _d(a), _d(b)
    while d <= e:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def _vec_kpis(a: list, riders: int, rider_days: int, ndays: int) -> dict:
    g = lambda k: a[I[k]]
    delivered, orders, ptod_n, otn = g("delivered"), g("orders"), g("ptod_n"), g("otn")
    return {"orders": orders, "delivered": delivered, "cancelled": g("cancelled"),
            "cancel_pct": round(100 * g("cancelled") / orders, 1) if orders else None,
            "avg_ptod": round(g("ptod_s") / ptod_n, 1) if ptod_n else None,
            "within_pct": round(100 * g("within") / ptod_n) if ptod_n else None,
            "on_time_pct": round(100 * g("ot") / otn) if otn else None,
            "avg_kitchen": round(g("kit_s") / g("kit_n"), 1) if g("kit_n") else None,
            "long_waits": g("long"), "long_pct": round(100 * g("long") / delivered) if delivered else None,
            "riders": riders, "per_rider": round(delivered / rider_days, 1) if rider_days else None,
            "per_day": round(delivered / max(1, ndays))}


def _agg(datas: list, city: str = "") -> tuple:
    """Sum of day tables (optionally one city) → (vector, distinct riders, rider-days)."""
    vec, allr, rider_days = [0] * _NF, set(), 0
    for data in datas:
        for c, vals in data["tot"].items():
            if city and c != city:
                continue
            for i, v in enumerate(vals):
                vec[i] += v
        day_r = set()
        for c, ids in data["riders"].items():
            if not city or c == city:
                day_r.update(ids)
        rider_days += len(day_r); allr |= day_r
    return vec, len(allr), rider_days


def performance(store, rules, now: datetime, period: str = "today", city: str = "", frm: str = "", to: str = "") -> dict:
    """Same answer as performance_sql, built from the day cube."""
    from store import period_range
    from orders import iso
    P = resolve(period, now, frm, to)
    tgt, today = rules.ptod_target_min, P["today"]
    D = lambda d, cut="": perf_day(store, rules, d, today, cut)
    cur_days = [D(d) for d in _days(P["a"], P["b"])]
    if P["has_today"]:
        # today is not over: the earlier period's last day only up to the same time of day
        t0 = period_range(today, now)[0]
        p0 = period_range(P["pb"], now)[0]
        cut = iso((p0 + (now - t0)).replace(second=0, microsecond=0))
        prev_days = [D(d) for d in _days(P["pa"], P["pb"])[:-1]] + [D(P["pb"], cut)]
        P["compare_label"] += " · until " + now.astimezone(BERLIN).strftime("%H:%M")
    else:
        prev_days = [D(d) for d in _days(P["pa"], P["pb"])]
    vec, nr, nrd = _agg(cur_days, city)
    pvec, pnr, pnrd = _agg(prev_days, city)
    cur, prev = _vec_kpis(vec, nr, nrd, P["days"]), _vec_kpis(pvec, pnr, pnrd, P["days"])

    end = _d(P["b"])
    trend = []
    for i in range(30):
        d = (end - timedelta(days=29 - i)).isoformat()
        v, n_r, _ = _agg([D(d)], city)
        g = lambda k: v[I[k]]
        trend.append({"day": d, "label": _fmt(_d(d)), "delivered": g("delivered"),
                      "avg_ptod": round(g("ptod_s") / g("ptod_n"), 1) if g("ptod_n") else None,
                      "within_pct": round(100 * g("within") / g("ptod_n")) if g("ptod_n") else None,
                      "on_time_pct": round(100 * g("ot") / g("otn")) if g("otn") else None,
                      "cancel_pct": round(100 * g("cancelled") / g("orders"), 1) if g("orders") else None,
                      "avg_kitchen": round(g("kit_s") / g("kit_n"), 1) if g("kit_n") else None, "riders": n_r})

    hs = {}
    for data in cur_days:
        for row in data["rows"]:
            if (city and row[0] != city) or row[1] is None:
                continue
            a = hs.setdefault(row[1], [0] * _NF)
            for i, v in enumerate(row[2:]):
                a[i] += v
    cap = getattr(rules, "riders_capacity_per_hour", 1.5) or 1.5
    hours = []
    for h in range(9, 24):
        r = hs.get(h)
        if not r and not (10 <= h <= 22):
            continue
        r = r or [0] * _NF
        n, rd = r[I["orders"]], r[I["rider_h"]]
        hours.append({"hour": h, "orders": round(n / P["days"], 1), "riders": round(rd / P["days"], 1),
                      "capacity": round(rd * cap / P["days"], 1), "late": r[I["late"]],
                      "late_pct": round(100 * r[I["late"]] / r[I["delivered"]]) if r[I["delivered"]] else None,
                      "avg_ptod": round(r[I["ptod_s"]] / r[I["ptod_n"]], 1) if r[I["ptod_n"]] else None})

    ph = lambda s, n: round(vec[I[s]] / vec[I[n]], 1) if vec[I[n]] else None
    phases = [{"key": k, "label": lbl, "min": ph(s, n)} for k, lbl, s, n in (
        ("accept", "Rider accepts", "acc_s", "acc_n"), ("to_rest", "Ride to the restaurant", "tr_s", "tr_n"),
        ("wait", "Waiting at the restaurant", "wt_s", "wt_n"), ("to_cust", "Ride to the customer", "tc_s", "tc_n"),
        ("handover", "Hand-over", "ho_s", "ho_n"))]

    per_city = {}
    for data in cur_days:
        for c, vals in data["tot"].items():
            a = per_city.setdefault(c, [0] * _NF)
            for i, v in enumerate(vals):
                a[i] += v
    cities = []
    for c, a in per_city.items():
        g = lambda k: a[I[k]]
        if not g("delivered"):
            continue
        cities.append({"city": c, "delivered": g("delivered"), "cancelled": g("cancelled"),
                       "avg_ptod": round(g("ptod_s") / g("ptod_n"), 1) if g("ptod_n") else None,
                       "within_pct": round(100 * g("within") / g("ptod_n")) if g("ptod_n") else None,
                       "on_time_pct": round(100 * g("ot") / g("otn")) if g("otn") else None,
                       "avg_kitchen": round(g("kit_s") / g("kit_n"), 1) if g("kit_n") else None})
    cities.sort(key=lambda c: (-(c["avg_ptod"] or 0), c["city"]))

    heat = {}
    for i in range(28):
        d = end - timedelta(days=27 - i)
        wd = (d.weekday() + 1) % 7                                  # SQLite %w: Sunday = 0
        for row in D(d.isoformat())["rows"]:
            if (city and row[0] != city) or row[1] is None or not (11 <= row[1] <= 22):
                continue
            x = heat.setdefault((wd, row[1]), [0, 0])
            x[0] += row[2 + I["delivered"]]; x[1] += row[2 + I["late"]]
    heat_rows = []
    for wd, name in ((1, "Mon"), (2, "Tue"), (3, "Wed"), (4, "Thu"), (5, "Fri"), (6, "Sat"), (0, "Sun")):
        cells = []
        for h in range(11, 23):
            n, late = heat.get((wd, h), (0, 0))
            cells.append({"hour": h, "n": n, "pct": round(100 * late / n) if n >= 5 else None})
        heat_rows.append({"day": name, "cells": cells})

    total_late = sum(vec[I["c_" + k]] for k in ("kitchen", "accept", "handback", "ride", "to_rest", "customer", "other"))
    reasons = [{"key": k, "label": lbl, "n": vec[I["c_" + k]], "pct": round(100 * vec[I["c_" + k]] / total_late) if total_late else 0}
               for k, lbl in (("kitchen", "Kitchen not ready (rider waited 10+ min)"), ("accept", "No rider accepted for 5+ min"),
                              ("handback", "A rider handed the order back"), ("ride", "Long ride to the customer (20+ min)"),
                              ("to_rest", "Long ride to the restaurant (15+ min)"), ("customer", "Waited at the customer (5+ min)"),
                              ("other", "Several small delays"))]
    reasons.sort(key=lambda x: -x["n"])
    return {"period": P, "target": tgt, "target_within_pct": rules.target_within_pct, "plan_grace": rules.plan_grace_min,
            "kpis": cur, "prev": prev, "trend": trend, "hours": hours, "capacity_per_hour": cap, "phases": phases,
            "ptod_avg": round(vec[I["ptod_s"]] / vec[I["ptod_n"]], 1) if vec[I["ptod_n"]] else None, "cities": cities,
            "heat": heat_rows, "reasons": reasons, "late_total": total_late}


def rest_day(store, day: str, today: str, minutes: int, now: datetime) -> dict:
    """One day of restaurant waits: {"reached": [[restaurant, city, n]], "long": [[restaurant, city, hour, n, sum, max]]}.
    Delivered orders come from their stored kitchen_wait; for finished days the not-delivered ones (cancelled after
    waiting) are measured from their history like the PDF. Today's not-delivered ones are added live by app.py."""
    def build():
        a = {"d": day, "m": minutes}
        reached = [[r["restaurant"] or "?", r["city"] or "", r["n"]] for r in store._rows(
            "SELECT restaurant, city, COUNT(*) AS n FROM orders WHERE day = :d AND at_restaurant_at IS NOT NULL GROUP BY restaurant, city", a)]
        long = [[r["restaurant"] or "?", r["city"] or "", r["hour"], r["n"], r["s"] or 0.0, r["mx"] or 0.0] for r in store._rows(
            "SELECT restaurant, city, hour, COUNT(*) AS n, SUM(kitchen_wait) AS s, MAX(kitchen_wait) AS mx FROM orders "
            "WHERE day = :d AND phase='delivered' AND kitchen_wait >= :m GROUP BY restaurant, city, hour", a)]
        if day < today:
            from store import period_range
            from pdf_report import collect
            start, end = period_range(day, now)
            stored = [store._hydrate(r) for r in store._rows(
                "SELECT * FROM orders WHERE day = :d AND at_restaurant_at IS NOT NULL AND phase != 'delivered'", a)]
            for o in stored:
                o.setdefault("city", store.city_of(o))
            data = collect(stored, minutes, start - timedelta(hours=6), end + timedelta(hours=6), min(now, end), day, "", None)
            for g in data["groups"]:
                for row in g["rows"]:
                    w = int(str(row[7]).split()[0])
                    try:
                        h = int(row[3][:2])
                    except (ValueError, TypeError):
                        h = None
                    long.append([g["restaurant"], g["city"], h, 1, float(w), float(w)])
        return {"reached": reached, "long": long}
    return cube(store, "rest", day, f"m{minutes}", today, build, ttl_today=30)


def warm(store, rules, now: datetime, days: int = 63, rest_days: int = 31):
    """After a start: fill the cube for the days the pages use (in the background, gently)."""
    today = day_key(now)
    for i in range(1, days + 1):
        d = (_d(today) - timedelta(days=i)).isoformat()
        try:
            perf_day(store, rules, d, today)
            if i <= rest_days:
                rest_day(store, d, today, 10, now)
        except Exception:
            pass
        time.sleep(0.03)


# ------------------------------------------------------------------ Restaurants
def restaurant_detail(store, now: datetime, P: dict, name: str, city: str, minutes: int) -> dict:
    """Average kitchen wait by hour (all its delivered orders in the period) and long waits per week (8 weeks)."""
    q = store._rows
    w, args = _where(P["a"], P["b"], city)
    by_hour = [{"hour": r["hour"], "avg": round(r["avg"], 1), "n": r["n"]} for r in q(
        "SELECT hour, AVG(kitchen_wait) AS avg, COUNT(*) AS n FROM orders "
        f"WHERE {w} AND restaurant=? AND phase='delivered' AND kitchen_wait IS NOT NULL AND hour IS NOT NULL GROUP BY hour ORDER BY hour",
        args + [name]) if r["avg"] is not None]
    end = _d(P["b"])
    start = end - timedelta(days=end.weekday() + 7 * 7)                # Monday, 8 weeks back
    ww, wargs = _where(start.isoformat(), P["b"], city)
    per = {r["wk"]: (r["long"] or 0, r["n"] or 0) for r in q(
        "SELECT CAST((julianday(day) - julianday(?)) / 7 AS INTEGER) AS wk, "
        "SUM(CASE WHEN kitchen_wait >= ? THEN 1 ELSE 0 END) AS long, COUNT(*) AS n FROM orders "
        f"WHERE {ww} AND restaurant=? AND phase='delivered' GROUP BY wk", [start.isoformat(), minutes] + wargs + [name])}
    weeks = [{"label": (start + timedelta(days=7 * i)).strftime("%d %b").lstrip("0"), "long": per.get(i, (0, 0))[0],
              "orders": per.get(i, (0, 0))[1]} for i in range(8)]
    return {"by_hour": by_hour, "weeks": weeks}


def waits_from_columns(store, start: datetime, end: datetime, now: datetime, city: str, minutes: int) -> tuple:
    """Delivered orders whose rider waited `minutes`+ (stored kitchen_wait = first arrival of any rider → pickup, the
    same number the PDF shows), per restaurant — no order is loaded, so a month answers in well under a second.
    Returns (reached {(restaurant, city): n}, long {(restaurant, city): [n, sum, max]}, long per dispatch hour)."""
    from orders import iso
    q = store._rows
    w = "day >= ? AND day <= ? AND at_restaurant_at IS NOT NULL AND at_restaurant_at >= ? AND at_restaurant_at < ?"
    args = [day_key(start - timedelta(days=1)), day_key(min(end, now + timedelta(days=1))), iso(start), iso(end)]
    if city:
        w += " AND city=?"; args.append(city)
    reached = {(r["restaurant"] or "?", r["city"] or ""): r["n"] for r in q(
        f"SELECT restaurant, city, COUNT(*) AS n FROM orders WHERE {w} GROUP BY restaurant, city", args)}
    lw = f"{w} AND phase='delivered' AND kitchen_wait >= ?"
    long = {(r["restaurant"] or "?", r["city"] or ""): [r["n"], r["s"] or 0.0, r["mx"] or 0.0] for r in q(
        f"SELECT restaurant, city, COUNT(*) AS n, SUM(kitchen_wait) AS s, MAX(kitchen_wait) AS mx FROM orders WHERE {lw} GROUP BY restaurant, city",
        args + [minutes])}
    hours = {r["hour"]: r["n"] for r in q(f"SELECT hour, COUNT(*) AS n FROM orders WHERE {lw} AND hour IS NOT NULL GROUP BY hour", args + [minutes])}
    return reached, long, hours


def restaurant_summary(data: dict, top: int = 250) -> dict:
    """The ranking from pdf_report.collect()'s data, without the order rows."""
    groups = [{**{k: g[k] for k in ("restaurant", "city", "n", "of_all", "avg", "max")},
               "share": round(100 * g["n"] / g["of_all"]) if g.get("of_all") else None} for g in data["groups"][:top]]
    longest = max(data["groups"], key=lambda g: g["max"]) if data["groups"] else None
    return {"reached": data["total_reached"], "n": data["n"], "avg": data["avg"], "max": data["max"],
            "share": round(100 * data["n"] / data["total_reached"]) if data["total_reached"] else None,
            "max_where": f"{longest['restaurant']} · {longest['city']}" if longest else "",
            "restaurants": len(data["groups"]), "groups": groups}


# ------------------------------------------------------------------ Riders & fleets
def fleets(store, rules, P: dict, city: str = "") -> list:
    q = store._rows
    tgt = rules.ptod_target_min
    w, args = _where(P["a"], P["b"], city)
    hb = {r["fleet"] or "": r["n"] for r in q(
        "SELECT fleet, COUNT(*) AS n FROM handbacks WHERE day BETWEEN ? AND ? AND excused=0" + (" AND city=?" if city else "") + " GROUP BY fleet",
        [P["a"], P["b"]] + ([city] if city else []))}
    out = []
    for r in q("SELECT fleet, COUNT(DISTINCT CASE WHEN phase='delivered' THEN rider_id END) AS riders, SUM(phase='delivered') AS delivered, "
               "AVG(CASE WHEN phase='delivered' THEN ptod_min END) AS avg_ptod, "
               "SUM(CASE WHEN phase='delivered' AND ptod_min <= ? THEN 1 ELSE 0 END) AS within, "
               "SUM(CASE WHEN phase='delivered' AND ptod_min IS NOT NULL THEN 1 ELSE 0 END) AS ptod_n, "
               "AVG(CASE WHEN accept_min BETWEEN 0 AND 180 THEN accept_min END) AS accept "
               f"FROM orders WHERE {w} GROUP BY fleet", [tgt] + args):
        if not r["delivered"]:
            continue
        name = r["fleet"] or "No fleet set"
        out.append({"fleet": name, "riders": r["riders"] or 0, "delivered": r["delivered"] or 0,
                    "avg_ptod": round(r["avg_ptod"], 1) if r["avg_ptod"] is not None else None,
                    "within_pct": round(100 * r["within"] / r["ptod_n"]) if r["ptod_n"] else None,
                    "accept": round(r["accept"], 1) if r["accept"] is not None else None,
                    "handbacks": hb.get(r["fleet"] or "", 0),
                    "per_rider": round((r["delivered"] or 0) / r["riders"], 1) if r["riders"] else None})
    out.sort(key=lambda f: -f["delivered"])
    return out


# ====================================================================== 9.5 — staffing forecast (with the weather)
# Tomorrow's orders per city and hour = the last 4 same weekdays (recent ones count more) × the city's trend of the
# last 7 days × the weather in that hour (Deutscher Wetterdienst forecast via Bright Sky). Riders needed = orders ÷
# what one rider delivers per hour (Settings). Only shown on the dashboard — nobody is messaged.
_KNOWN_LL = {
    "aachen": (50.776, 6.084), "arnsberg": (51.396, 8.064), "augsburg": (48.371, 10.898), "berlin": (52.520, 13.405),
    "bielefeld": (52.022, 8.532), "bischofsheim": (49.993, 8.364), "bochum": (51.482, 7.216), "bonn": (50.737, 7.098),
    "braunschweig": (52.269, 10.521), "bremen": (53.079, 8.802), "celle": (52.625, 10.081), "chemnitz": (50.828, 12.921),
    "darmstadt": (49.873, 8.651), "dessau": (51.835, 12.246), "dormagen": (51.096, 6.840), "dortmund": (51.514, 7.468),
    "dresden": (51.050, 13.738), "duisburg": (51.435, 6.763), "düsseldorf": (51.227, 6.774), "erfurt": (50.979, 11.033),
    "essen": (51.456, 7.012), "frankfurt": (50.111, 8.682), "freiburg": (47.999, 7.842), "georgsmarienhütte": (52.203, 8.045),
    "göttingen": (51.541, 9.916), "halle": (51.482, 11.970), "hamburg": (53.551, 9.994), "harburg": (53.460, 9.983),
    "hannover": (52.375, 9.732), "heidelberg": (49.399, 8.672), "karlsruhe": (49.007, 8.404), "kassel": (51.312, 9.480),
    "kiel": (54.323, 10.123), "koblenz": (50.356, 7.594), "köln": (50.938, 6.960), "leipzig": (51.340, 12.375),
    "lippstadt": (51.674, 8.344), "lübeck": (53.866, 10.687), "magdeburg": (52.121, 11.628), "mainz": (49.993, 8.247),
    "mannheim": (49.488, 8.466), "minden": (52.289, 8.914), "mönchengladbach": (51.180, 6.442), "münchen": (48.137, 11.576),
    "munich": (48.137, 11.576), "münster": (51.961, 7.626), "neumünster": (54.073, 9.984), "neunkirchen": (49.344, 7.180),
    "nürnberg": (49.452, 11.077), "oldenburg": (53.144, 8.214), "osnabrück": (52.279, 8.047), "paderborn": (51.719, 8.754),
    "plauen": (50.495, 12.136), "potsdam": (52.391, 13.064), "regensburg": (49.013, 12.102), "remscheid": (51.179, 7.189),
    "rheine": (52.281, 7.440), "rostock": (54.092, 12.099), "rüsselsheim": (49.992, 8.413), "saarbrücken": (49.240, 6.997),
    "solingen": (51.171, 7.084), "stuttgart": (48.776, 9.183), "ulm": (48.401, 9.987), "waiblingen": (48.831, 9.317),
    "wiesbaden": (50.078, 8.240), "wolfsburg": (52.423, 10.787), "wuppertal": (51.256, 7.151), "würzburg": (49.792, 9.953),
    "zwickau": (50.719, 12.496),
}


def _wmedian(vals: list) -> float:
    vals = sorted(vals)
    half, acc = sum(w for _, w in vals) / 2, 0
    for v, w in vals:
        acc += w
        if acc >= half:
            return v
    return vals[-1][0]


def city_coords(store, place_ll: dict, now: datetime) -> dict:
    """city -> (lat, lon): the middle of its restaurants (from the place positions MotionTools gave us); a city with no
    known restaurant position falls back to the town's centre."""
    def build():
        pts = {}
        for r in store._rows("SELECT city, place_id, COUNT(*) AS n FROM orders WHERE day >= ? AND place_id IS NOT NULL AND place_id != '' "
                             "GROUP BY city, place_id", (day_key(now - timedelta(days=14)),)):
            ll = place_ll.get(r["place_id"])
            if ll:
                pts.setdefault(r["city"] or "?", []).append((ll[0], ll[1], r["n"]))
        out = {c: (_wmedian([(a, n) for a, _, n in p]), _wmedian([(b, n) for _, b, n in p])) for c, p in pts.items() if p}
        for r in store._rows("SELECT DISTINCT city FROM orders WHERE day >= ? AND city IS NOT NULL AND city != ''", (day_key(now - timedelta(days=14)),)):
            c = r["city"]
            if c in out:
                continue
            words = [w for w in c.lower().replace("/", " ").replace("-", " ").split() if w]
            for w in [" ".join(words)] + words:
                if w in _KNOWN_LL:
                    out[c] = _KNOWN_LL[w]; break
        return out
    return cached(("coords", day_key(now)), 6 * 3600, build)


_WX: dict = {}                      # city -> {"at": epoch, "hours": {"YYYY-MM-DDTHH": {"mm", "temp", "cond", "icon"}}}
WX_SOURCE = "Deutscher Wetterdienst (DWD) via Bright Sky"


WX_URL = os.environ.get("WX_URL", "").strip() or "https://api.brightsky.dev/weather"   # override only for tests


def refresh_weather(coords: dict, today: str, days: int = 8) -> int:
    """Hourly forecast (rain, temperature, condition) for every city for the next `days` days. Free, no key; DWD open
    data. Returns how many cities were updated. Failures keep the previous forecast."""
    import httpx
    ok = 0
    d2 = (_d(today) + timedelta(days=days)).isoformat()
    with httpx.Client(timeout=10, headers={"User-Agent": "quickzi-ops/9.5"}) as c:
        for city, (lat, lon) in coords.items():
            try:
                r = c.get(WX_URL, params={"lat": round(lat, 3), "lon": round(lon, 3), "date": today,
                                                                     "last_date": d2, "tz": "Europe/Berlin"})
                if r.status_code != 200:
                    continue
                hours = {}
                for w in r.json().get("weather") or []:
                    try:
                        t = datetime.fromisoformat(w["timestamp"]).astimezone(BERLIN)
                    except (KeyError, ValueError, TypeError):
                        continue
                    hours[t.strftime("%Y-%m-%dT%H")] = {"mm": w.get("precipitation"), "temp": w.get("temperature"),
                                                         "cond": w.get("condition") or "", "icon": w.get("icon") or ""}
                if hours:
                    _WX[city] = {"at": time.time(), "hours": hours}; ok += 1
            except Exception:
                continue
            time.sleep(0.15)
    return ok


def wx_factor(w: dict | None) -> float:
    """How the weather moves orders in that hour: rain +8 %, heavy rain / snow / sleet / hail +15 %, frost +5 %,
    heat (28 °C+) −5 %."""
    if not w:
        return 1.0
    mm, cond, t = w.get("mm") or 0, (w.get("cond") or "").lower(), w.get("temp")
    f = 1.0
    if cond in ("snow", "sleet", "hail") or mm >= 2.5:
        f += 0.15
    elif cond in ("rain", "thunderstorm") or mm >= 0.5:
        f += 0.08
    if t is not None and t <= 0:
        f += 0.05
    if t is not None and t >= 28:
        f -= 0.05
    return round(f, 2)


_STAGES = ("tr_s", "tr_n", "wt_s", "wt_n", "tc_s", "tc_n", "ho_s", "ho_n")


def rider_busy_day(store, day: str, today: str) -> dict:
    """{city: [rider minutes busy (overlaps counted once), minutes summed per order, deliveries]} for one day.
    A rider carrying two orders at once is busy once, not twice — this is how much merged orders save."""
    from orders import ts

    def build():
        out, cur = {}, None
        for r in store._rows("SELECT city, rider_id, accepted_at, delivered_at FROM orders WHERE day = ? AND phase='delivered' "
                             "AND rider_id IS NOT NULL AND rider_id != '' AND accepted_at IS NOT NULL AND delivered_at IS NOT NULL "
                             "ORDER BY city, rider_id, accepted_at", [day]):
            a, b = ts(r["accepted_at"]), ts(r["delivered_at"])
            if not a or not b or b <= a or (b - a).total_seconds() > 3 * 3600:
                continue
            c = r["city"] or "?"
            g = out.setdefault(c, [0.0, 0.0, 0])
            g[1] += (b - a).total_seconds() / 60; g[2] += 1
            k = (c, r["rider_id"])
            if cur and cur[0] == k and a < cur[2]:
                cur[2] = max(cur[2], b)
            else:
                if cur:
                    out[cur[0][0]][0] += (cur[2] - cur[1]).total_seconds() / 60
                cur = [k, a, b]
        if cur:
            out[cur[0][0]][0] += (cur[2] - cur[1]).total_seconds() / 60
        return {c: [round(u, 1), round(sm, 1), n] for c, (u, sm, n) in out.items()}
    return cube(store, "busy", day, "", today, build, 60)
BUSY_SHARE = 0.8                 # a rider is busy with deliveries ~80 % of an hour; the rest is waiting for the next one


def forecast(store, rules, now: datetime, day: str = "", city: str = "", shifts: list = None) -> dict:
    """Orders and riders needed per hour for one day (default tomorrow), per city and in total."""
    today = day_key(now)
    T = day or (_d(today) + timedelta(days=1)).isoformat()
    T = max(today, min(T, (_d(today) + timedelta(days=6)).isoformat()))
    D = lambda d: perf_day(store, rules, d, today)
    weights_all = [0.4, 0.3, 0.2, 0.1]
    base = [((_d(T) - timedelta(days=7 * k)).isoformat(), weights_all[k - 1]) for k in range(1, 5)]
    base = [(d, w) for d, w in base if d < today and D(d)["tot"]]            # days before the first recorded one don't count
    wsum = sum(w for _, w in base) or 1.0
    cap = max(0.5, float(getattr(rules, "riders_capacity_per_hour", 1.5) or 1.5))
    # trend: the last 7 finished days vs the 4 weeks before them, per city (bounded ±15 %)
    last7 = [D((_d(today) - timedelta(days=i)).isoformat()) for i in range(1, 8)]
    prev28 = [D((_d(today) - timedelta(days=i)).isoformat()) for i in range(8, 36)]
    tot = lambda days, c: sum(x["tot"].get(c, [0] * _NF)[I["orders"]] for x in days)
    per = {}                                                                     # (city, hour) -> [weighted orders, weighted riders, lo, hi]
    cities = set()
    st_h, st_c = {}, {}                          # delivery-time stages (sum, n) per (city, hour) and per city, same weekdays
    for d, w in base:
        rows = {}
        for row in D(d)["rows"]:
            if row[1] is None or (city and row[0] != city):
                continue
            rows[(row[0], row[1])] = (row[2 + I["orders"]], row[2 + I["rider_h"]])
            cities.add(row[0])
            for acc in (st_h.setdefault((row[0], row[1]), [0.0] * 9), st_c.setdefault(row[0], [0.0] * 9)):
                for j, k in enumerate(_STAGES):
                    acc[j] += row[2 + I[k]] or 0
                acc[8] += row[2 + I["delivered"]] or 0
        for k, (o, r) in rows.items():
            x = per.setdefault(k, [0.0, 0.0, None, None, 0])
            x[0] += w * o; x[1] += w * r; x[4] += 1
            x[2] = o if x[2] is None else min(x[2], o); x[3] = o if x[3] is None else max(x[3], o)
    # weekly average over the days that were recorded (a city opened 3 weeks ago is not "+33 %")
    n7 = sum(1 for x in last7 if x["tot"]); n28 = sum(1 for x in prev28 if x["tot"])
    trend = {}
    for c in cities:
        a = tot(last7, c) / n7 * 7 if n7 >= 5 else 0
        b = tot(prev28, c) / n28 * 7 if n28 >= 14 else 0
        trend[c] = round(max(0.85, min(1.15, a / b)), 2) if a and b else 1.0
    # today: the hours already over steer the rest of the day
    actual, today_f = {}, {}
    if T == today:
        for row in D(today)["rows"]:
            if row[1] is not None and (not city or row[0] == city):
                actual[(row[0], row[1])] = row[2 + I["orders"]]
        cur_h = now.astimezone(BERLIN).hour
        for c in cities:
            done = [h for h in range(0, cur_h) if per.get((c, h))]
            fc = sum(per[(c, h)][0] / wsum * trend[c] for h in done)
            ac = sum(actual.get((c, h), 0) for h in done)
            today_f[c] = round(max(0.7, min(1.3, ac / fc)), 2) if len(done) >= 3 and fc >= 5 else 1.0
    planned = {}
    for s in shifts or []:
        try:
            sh, sm = map(int, (s.get("start") or "")[:5].split(":")); eh, em = map(int, (s.get("end") or "")[:5].split(":"))
        except (ValueError, AttributeError):
            continue
        a, b = sh * 60 + sm, eh * 60 + em
        if b <= a:
            b += 24 * 60
        c = s.get("city") or ""
        if city and c and c != city:
            continue
        for h in range(0, 24):
            if min(b, (h + 1) * 60) - max(a, h * 60) >= 30:                      # at least half of the hour on shift
                planned[(c, h)] = planned.get((c, h), 0) + 1
    has_sheet = bool(planned)
    cur_h = now.astimezone(BERLIN).hour if T == today else -1

    merged = {}                                  # per city: share of rider time left once overlapping orders count once
    for c in cities:
        u = sm = n = 0
        for d, _ in base:
            x = rider_busy_day(store, d, today).get(c)
            if x:
                u += x[0]; sm += x[1]; n += x[2]
        merged[c] = round(max(0.4, min(1.0, u / sm)), 2) if n >= 20 and sm else 1.0

    def busy(c, h):
        """Minutes one delivery keeps a rider busy (accept → at restaurant → wait → ride → hand-over) in that city and
        hour on the same weekdays; the city's whole day when the hour has too few deliveries; None without timings."""
        for acc, need in ((st_h.get((c, h)), 8), (st_c.get(c), 5)):
            if acc and acc[8] >= need and all(acc[j + 1] > 0 for j in range(0, 8, 2)):
                return sum(acc[j] / acc[j + 1] for j in range(0, 8, 2)) * merged.get(c, 1.0), ("hour" if acc is st_h.get((c, h)) else "city")
        return None, "settings"
    out_c, hours_sum = [], {}
    for c in sorted(cities):
        wx = (_WX.get(c) or {}).get("hours", {})
        rows_c = []
        for h in range(0, 24):
            x = per.get((c, h))
            if not x:
                continue
            w = wx.get(f"{T}T{h:02d}")
            f = wx_factor(w)
            o = x[0] / wsum * trend.get(c, 1.0) * f * (today_f.get(c, 1.0) if h >= cur_h >= 0 else 1.0)
            if h < cur_h:                                                          # an hour already over: what really happened
                o = actual.get((c, h), 0)
            mins, src = busy(c, h)
            per_rider = max(0.6, min(4.0, BUSY_SHARE * 60 / mins)) if mins else cap   # deliveries one rider manages in the hour
            need = math.ceil(o / per_rider - 0.15) if o >= 0.3 else 0
            p = planned.get((c, h), 0)
            rows_c.append({"hour": h, "orders": round(o, 1), "lo": round(x[2] * trend.get(c, 1.0) * f, 1), "hi": round(x[3] * trend.get(c, 1.0) * f, 1),
                           "had": round(x[1] / wsum, 1), "needed": need, "planned": p, "actual": actual.get((c, h)), "past": h < cur_h,
                           "min": round(mins, 1) if mins else None, "per_rider": round(per_rider, 2), "src": src,
                           "mm": (w or {}).get("mm"), "temp": (w or {}).get("temp"), "cond": (w or {}).get("cond"), "icon": (w or {}).get("icon"), "wx": f})
        if not rows_c:
            continue
        total = sum(r["orders"] for r in rows_c)
        peak = max(rows_c, key=lambda r: r["orders"])
        need_h = sum(r["needed"] for r in rows_c if not r["past"])
        plan_h = sum(planned.get((c, r["hour"]), 0) for r in rows_c if not r["past"])
        rain_h = [r for r in rows_c if (r["wx"] or 1) > 1.0 and 11 <= r["hour"] <= 22]
        tm = [(r["min"], r["orders"]) for r in rows_c if r["min"]]
        city_min = sum(m * o for m, o in tm) / sum(o for _, o in tm) if tm and sum(o for _, o in tm) else None
        temps = [r["temp"] for r in rows_c if r["temp"] is not None and 11 <= r["hour"] <= 22]
        out_c.append({"city": c, "orders": round(total), "peak_hour": peak["hour"], "peak_orders": round(peak["orders"], 1), "peak_needed": peak["needed"],
                      "rider_hours": need_h, "planned_hours": plan_h if has_sheet else None, "gap_hours": (plan_h - need_h) if has_sheet else None,
                      "trend": trend.get(c, 1.0), "today_factor": today_f.get(c), "rain_hours": len(rain_h),
                      "min": round(city_min, 1) if city_min else None, "peak_min": peak["min"], "peak_per_rider": peak["per_rider"],
                      "merged_saves": round(1 - merged.get(c, 1.0), 2),
                      "rain_mm": round(sum((r["mm"] or 0) for r in rows_c if 11 <= r["hour"] <= 22), 1),
                      "temp_min": min(temps) if temps else None, "temp_max": max(temps) if temps else None,
                      "weather": bool(wx), "hours": rows_c})
        for r in rows_c:
            s = hours_sum.setdefault(r["hour"], {"hour": r["hour"], "orders": 0.0, "lo": 0.0, "hi": 0.0, "had": 0.0, "needed": 0, "planned": 0,
                                                  "actual": None, "past": r["past"], "mm": None, "temp": None, "cond": None, "icon": None, "wx": None,
                                                  "_mo": 0.0, "_o": 0.0})
            if r["min"]:
                s["_mo"] += r["min"] * r["orders"]; s["_o"] += r["orders"]
            for k in ("orders", "lo", "hi", "had", "needed", "planned"):
                s[k] += r[k] or 0
            if r["actual"] is not None:
                s["actual"] = (s["actual"] or 0) + r["actual"]
            if city:
                s.update(mm=r["mm"], temp=r["temp"], cond=r["cond"], icon=r["icon"], wx=r["wx"])
    out_c.sort(key=lambda c: -c["orders"])
    hours = [dict({k: x for k, x in v.items() if not k.startswith("_")}, orders=round(v["orders"], 1), lo=round(v["lo"], 1), hi=round(v["hi"], 1),
                  had=round(v["had"], 1), min=round(v["_mo"] / v["_o"], 1) if v["_o"] else None)
             for _, v in sorted(hours_sum.items()) if v["orders"] >= 0.5 or v["actual"]]
    if not city:                                    # all cities: how many cities have rain in that hour
        for h in hours:
            n = sum(1 for c in out_c for r in c["hours"] if r["hour"] == h["hour"] and (r["wx"] or 1) > 1.0)
            h["rain_cities"] = n
    peak = max(hours, key=lambda r: r["orders"]) if hours else None
    need_h = sum(h["needed"] for h in hours if not h["past"])
    plan_h = sum(h["planned"] for h in hours if not h["past"])
    return {"day": T, "today": today, "weekday": _d(T).strftime("%A"), "label": _fmt(_d(T)), "city": city,
            "basis": [d for d, _ in base], "capacity_per_hour": cap, "busy_share": BUSY_SHARE,
            "min": (lambda tm: round(sum(c["min"] * c["orders"] for c in tm) / sum(c["orders"] for c in tm), 1) if tm and sum(c["orders"] for c in tm) else None)(
                [c for c in out_c if c["min"]]),
            "no_timing": [c["city"] for c in out_c if not c["min"]],
            "totals": {"orders": round(sum(h["orders"] for h in hours)), "peak_hour": peak["hour"] if peak else None,
                       "peak_orders": round(peak["orders"]) if peak else None, "peak_needed": peak["needed"] if peak else None,
                       "rider_hours": need_h, "planned_hours": plan_h if has_sheet else None, "gap_hours": (plan_h - need_h) if has_sheet else None,
                       "same_day_avg": round(sum(tot([D(d)], c) for d, _ in base for c in cities) / max(1, len(base))) if base else None},
            "hours": hours, "cities": out_c, "has_sheet": has_sheet,
            "weather": {"cities": sum(1 for c in out_c if c["weather"]), "source": WX_SOURCE,
                        "rule": "rain +8 %, heavy rain / snow +15 %, frost +5 %, heat (28 °C+) −5 %"}}


# ====================================================================== 9.5 — Lieferando evidence pack
def evidence(store, rules, now: datetime, a: str, b: str, city: str = "", min_wait: int = 10, normal: int = 5, arrive_max: int = 20) -> dict:
    """Late deliveries (PTOD over target) that were late BECAUSE of the restaurant: the rider was at the restaurant within
    `arrive_max` min of dispatch, waited `min_wait`+ min for the food, and without the waiting beyond a normal
    `normal`-minute hand-over the order would have been on target. Only order numbers, restaurants and times — no
    customer data, no rider names."""
    from orders import ts
    tgt = rules.ptod_target_min
    w, args = "day BETWEEN ? AND ? AND phase='delivered' AND ptod_min > ? AND kitchen_wait >= ?", [a, b, tgt, min_wait]
    if city:
        w += " AND city=?"; args.append(city)
    rows = []
    for r in store._rows("SELECT id, ref, city, restaurant, dispatched_at, accepted_at, picked_up_at, at_customer_at, delivered_at, ptod_min, kitchen_wait, "
                         "(SELECT COUNT(*) FROM handbacks h WHERE h.order_id = orders.id) AS hb "
                         f"FROM orders WHERE {w} ORDER BY dispatched_at", args):
        disp, pick = ts(r["dispatched_at"]), ts(r["picked_up_at"])
        if not disp or not pick:
            continue
        kw = r["kitchen_wait"] or 0
        arrived = pick - timedelta(minutes=kw)                              # the first rider's arrival
        to_rest = (arrived - disp).total_seconds() / 60
        delay = max(0.0, kw - normal)
        without = r["ptod_min"] - delay
        if to_rest > arrive_max or without > tgt:
            continue
        hm = lambda t: t.astimezone(BERLIN).strftime("%H:%M") if t else "–"
        rows.append({"day": disp.astimezone(BERLIN).strftime("%a %d.%m."), "date": disp.astimezone(BERLIN).date().isoformat(),
                     "ref": r["ref"] or "", "id": r["id"], "city": r["city"] or "", "restaurant": r["restaurant"] or "?",
                     "dispatched": hm(disp), "accepted": hm(ts(r["accepted_at"])), "arrived": hm(arrived), "to_rest": round(to_rest),
                     "picked": hm(pick), "delivered": hm(ts(r["delivered_at"])), "wait": round(kw), "delay": round(delay),
                     "ptod": round(r["ptod_min"]), "without": round(without), "over": round(r["ptod_min"] - tgt),
                     "note": f"handed back {r['hb']}× while waiting" if r["hb"] else ""})
    vec, _, _ = _agg([perf_day(store, rules, d, day_key(now)) for d in _days(a, b)], city)
    delivered, late, within = vec[I["delivered"]], vec[I["late"]], vec[I["within"]]
    n_ptod = vec[I["ptod_n"]]
    groups = {}
    for x in rows:
        g = groups.setdefault((x["restaurant"], x["city"]), {"restaurant": x["restaurant"], "city": x["city"], "n": 0, "delay": 0, "wait": 0})
        g["n"] += 1; g["delay"] += x["delay"]; g["wait"] += x["wait"]
    for g in groups.values():
        g["avg_wait"] = round(g["wait"] / g["n"])
    return {"from": a, "to": b, "city": city, "target": tgt, "min_wait": min_wait, "normal": normal, "arrive_max": arrive_max,
            "delivered": delivered, "late": late, "n": len(rows), "late_share": round(100 * len(rows) / late) if late else None,
            "delay_min": sum(x["delay"] for x in rows), "within_pct": round(100 * within / n_ptod) if n_ptod else None,
            "within_if_excused": round(100 * (within + len(rows)) / n_ptod) if n_ptod else None,
            "restaurants": sorted(groups.values(), key=lambda g: (-g["n"], -g["delay"])), "rows": rows}


# ====================================================================== 9.5 — Monday report: waiting time
def weekly_waits(store, rules, now: datetime, a: str, b: str, pa: str, pb: str, normal: int = 5) -> dict:
    """Last week vs the week before, about restaurant waiting: minutes per order, rider-hours lost beyond a normal
    hand-over, the restaurants and cities that cost the most, the hour they are slowest."""
    q = store._rows
    sel = ("SELECT restaurant, city, COUNT(*) AS n, SUM(kitchen_wait >= 10) AS long, AVG(kitchen_wait) AS avg, "
           f"SUM(MAX(kitchen_wait - {int(normal)}, 0)) AS lost FROM orders WHERE day BETWEEN ? AND ? AND phase='delivered' "
           "AND kitchen_wait IS NOT NULL AND kitchen_wait BETWEEN 0 AND 180 GROUP BY restaurant, city")
    cur = {(r["restaurant"] or "?", r["city"] or ""): r for r in q(sel, (a, b))}
    prev = {(r["restaurant"] or "?", r["city"] or ""): r for r in q(sel, (pa, pb))}
    today = day_key(now)
    def tot(days):
        v, _, _ = _agg([perf_day(store, rules, d, today) for d in days])
        g = lambda k: v[I[k]]
        return {"delivered": g("delivered"), "avg_ptod": round(g("ptod_s") / g("ptod_n"), 1) if g("ptod_n") else None,
                "within_pct": round(100 * g("within") / g("ptod_n")) if g("ptod_n") else None,
                "avg_wait": round(g("wt_s") / g("wt_n"), 1) if g("wt_n") else None, "long": g("long"),
                "long_pct": round(100 * g("long") / g("delivered")) if g("delivered") else None}
    T, PT = tot(_days(a, b)), tot(_days(pa, pb))
    T["lost_h"] = round(sum(r["lost"] or 0 for r in cur.values()) / 60)
    PT["lost_h"] = round(sum(r["lost"] or 0 for r in prev.values()) / 60)
    top = sorted(cur.values(), key=lambda r: -(r["lost"] or 0))[:5]
    top_rows = []
    for r in top:
        k = (r["restaurant"] or "?", r["city"] or "")
        hrs = q("SELECT hour, AVG(kitchen_wait) AS avg, COUNT(*) AS n FROM orders WHERE day BETWEEN ? AND ? AND restaurant=? AND city=? "
                "AND phase='delivered' AND kitchen_wait IS NOT NULL AND hour IS NOT NULL GROUP BY hour HAVING n >= 3 ORDER BY avg DESC LIMIT 1",
                (a, b, r["restaurant"], r["city"]))
        p = prev.get(k)
        top_rows.append({"restaurant": k[0], "city": k[1], "lost_h": round((r["lost"] or 0) / 60, 1), "avg": round(r["avg"] or 0),
                         "long": r["long"] or 0, "n": r["n"], "slow_hour": hrs[0]["hour"] if hrs else None,
                         "slow_avg": round(hrs[0]["avg"]) if hrs else None,
                         "prev_lost_h": round((p["lost"] or 0) / 60, 1) if p else None})
    change = []
    for k in set(cur) | set(prev):
        c, p = (cur.get(k) or {}).get("lost") or 0, (prev.get(k) or {}).get("lost") or 0
        change.append((round((c - p) / 60, 1), k))
    change.sort()
    cities = {}
    for (rest, cty), r in cur.items():
        x = cities.setdefault(cty or "?", [0, 0.0, 0.0])
        x[0] += r["n"]; x[1] += (r["avg"] or 0) * r["n"]; x[2] += r["lost"] or 0
    city_rows = sorted(({"city": c, "avg": round(v[1] / v[0], 1) if v[0] else None, "lost_h": round(v[2] / 60)} for c, v in cities.items() if v[0] >= 30),
                       key=lambda c: -(c["avg"] or 0))
    return {"from": a, "to": b, "prev_from": pa, "prev_to": pb, "normal": normal, "week": T, "prev": PT, "top": top_rows,
            "worse": [{"restaurant": k[0], "city": k[1], "delta_h": d} for d, k in change[::-1][:3] if d > 0],
            "better": [{"restaurant": k[0], "city": k[1], "delta_h": d} for d, k in change[:3] if d < 0],
            "cities": city_rows[:3]}



# ====================================================================== 9.6 — keep PTOD under 30
# Every delivered order is split into its five stages. A late order is "saved" by a stage when it would have been on
# target had only that stage taken its normal time (the 30-minute budget below). Counting saved orders per stage, city,
# hour, restaurant and rider shows which fix brings the most "within 30′" — and exactly where to make it.
U30_STAGES = [("acc", "Rider accepts the order", 2), ("tr", "Ride to the restaurant", 8), ("wt", "Waiting for the food", 5),
              ("tc", "Ride to the customer", 12), ("ho", "Hand-over at the door", 3)]
_UK = [k for k, _, _ in U30_STAGES]
_UN = {k: v for k, _, v in U30_STAGES}
_UCH = 29          # per city|hour: n, within, late, ptod_s · (sum, n) × 5 all orders · (sum, n) × 5 late orders · saved × 5
_URE = 10          # per restaurant|city: n, late, sv_wt, wt_excess, wt_s, wt_n, sv_tc, tc_s, tc_n, sv_tr
_URI = 11          # per rider|city: n, late, sv_acc, acc_s, acc_n, sv_ho, ho_s, ho_n, sv_tr, tr_s, tr_n


def u30_day(store, rules, day: str, today: str) -> dict:
    t = rules.ptod_target_min

    def build():
        ch, rest, rid, rh, names = {}, {}, {}, {}, {}
        q = ("SELECT city, hour, restaurant, rider_id, rider, ptod_min, accept_min, kitchen_wait, "
             f"{_span('accepted_at', 'at_restaurant_at')} AS tr, {_span('picked_up_at', 'at_customer_at')} AS tc, "
             f"{_span('at_customer_at', 'delivered_at')} AS ho FROM orders WHERE day = ? AND phase='delivered' AND ptod_min IS NOT NULL")
        for r in store._rows(q, [day]):
            if r["hour"] is None:
                continue
            c, h, p = r["city"] or "?", int(r["hour"]), float(r["ptod_min"])
            ok = lambda x: x if x is not None and 0 <= x <= 180 else None
            st = {"acc": ok(r["accept_min"]), "tr": r["tr"], "wt": ok(r["kitchen_wait"]), "tc": r["tc"], "ho": r["ho"]}
            late = p > t
            sv = {k: bool(late and st[k] is not None and p - max(0.0, st[k] - _UN[k]) <= t) for k in _UK}
            v = ch.setdefault(f"{c}|{h}", [0.0] * _UCH)
            v[0] += 1; v[1] += not late; v[2] += late; v[3] += p
            for j, k in enumerate(_UK):
                x = st[k]
                if x is None:
                    continue
                v[4 + 2 * j] += x; v[5 + 2 * j] += 1
                if late:
                    v[14 + 2 * j] += x; v[15 + 2 * j] += 1
                v[24 + j] += sv[k]
            rn = r["restaurant"] or "?"
            g = rest.setdefault(f"{rn}|{c}", [0.0] * _URE)
            g[0] += 1; g[1] += late; g[2] += sv["wt"]; g[6] += sv["tc"]; g[9] += sv["tr"]
            if st["wt"] is not None:
                g[3] += max(0.0, st["wt"] - _UN["wt"]); g[4] += st["wt"]; g[5] += 1
                rh.setdefault(f"{rn}|{c}|{h}", []).append(round(st["wt"], 1))
            if st["tc"] is not None:
                g[7] += st["tc"]; g[8] += 1
            if r["rider_id"]:
                names[r["rider_id"]] = r["rider"] or names.get(r["rider_id"]) or ""
                x = rid.setdefault(f"{r['rider_id']}|{c}", [0.0] * _URI)
                x[0] += 1; x[1] += late; x[2] += sv["acc"]; x[5] += sv["ho"]; x[8] += sv["tr"]
                for k, i in (("acc", 3), ("ho", 6), ("tr", 9)):
                    if st[k] is not None:
                        x[i] += st[k]; x[i + 1] += 1
        hb = {}
        for r in store._rows("SELECT rider_id, city, COUNT(*) AS n FROM handbacks WHERE day = ? AND COALESCE(excused, 0) = 0 GROUP BY rider_id, city", [day]):
            if r["rider_id"]:
                hb[f"{r['rider_id']}|{r['city'] or '?'}"] = r["n"]
        rnd = lambda d: {k: [round(x, 1) for x in v] for k, v in d.items()}
        return {"ch": rnd(ch), "rest": rnd(rest), "rid": rnd(rid), "rh": rh, "hb": hb, "names": names}
    return cube(store, "u30", day, str(t), today, build, 30)


def _pct(a, b):
    return round(a / b * 100, 1) if b else None


def _send_later(waits_by_hour: dict) -> list:
    """Per hour: how many minutes later a rider can be sent so that he still waits ~2 min in 3 of 4 cases (25th
    percentile of the waits − 2). Neighbouring hours with a suggestion are joined; at least 6 orders per hour."""
    sug = {}
    for h, ws in waits_by_hour.items():
        if len(ws) < 6:
            continue
        q = sorted(ws)[len(ws) // 4]
        if q - 2 >= 3:
            sug[h] = (int(q - 2), len(ws))
    out, run = [], []
    for h in sorted(sug):
        if run and h != run[-1] + 1:
            out.append(run); run = []
        run.append(h)
    if run:
        out.append(run)
    return sorted(({"from": r[0], "to": r[-1] + 1, "min": min(sug[h][0] for h in r), "n": sum(sug[h][1] for h in r)} for r in out),
                  key=lambda x: -x["n"])[:3]


def under30(store, rules, now: datetime, period: str = "7d", city: str = "", frm: str = "", to: str = "") -> dict:
    P = resolve(period, now, frm, to)
    today, t = day_key(now), rules.ptod_target_min
    ch, rest, rid, rh, hb, names = {}, {}, {}, {}, {}, {}

    def add(dst, k, v, n):
        acc = dst.setdefault(k, [0.0] * n)
        for i, x in enumerate(v):
            acc[i] += x
    for d in _days(P["a"], P["b"]):
        D = u30_day(store, rules, d, today)
        for k, v in D["ch"].items():
            if not city or k.rsplit("|", 1)[0] == city:
                add(ch, k, v, _UCH)
        for k, v in D["rest"].items():
            if not city or k.rsplit("|", 1)[1] == city:
                add(rest, k, v, _URE)
        for k, v in D["rid"].items():
            if not city or k.rsplit("|", 1)[1] == city:
                add(rid, k, v, _URI)
        for k, v in D["rh"].items():
            if not city or k.rsplit("|", 2)[1] == city:
                rh.setdefault(k, []).extend(v)
        for k, v in D["hb"].items():
            if not city or k.rsplit("|", 1)[1] == city:
                hb[k] = hb.get(k, 0) + v
        names.update({k: v for k, v in D["names"].items() if v})
    tot = [0.0] * _UCH
    for v in ch.values():
        for i, x in enumerate(v):
            tot[i] += x
    n, late = tot[0], tot[2]
    prev_vec = _agg([perf_day(store, rules, d, today) for d in _days(P["pa"], P["pb"])], city)[0]
    prev_within = _pct(prev_vec[I["within"]], prev_vec[I["ptod_n"]])
    avg = lambda s_, n_: round(s_ / n_, 1) if n_ else None
    stages = []
    for j, (k, label, norm) in enumerate(U30_STAGES):
        sa, na, sl, nl = tot[4 + 2 * j], tot[5 + 2 * j], tot[14 + 2 * j], tot[15 + 2 * j]
        stages.append({"key": k, "label": label, "norm": norm, "all": avg(sa, na), "late": avg(sl, nl), "ontime": avg(sa - sl, na - nl)})
    # where each fix has to be made
    rows_rest = [(k.rsplit("|", 1), v) for k, v in rest.items()]
    rows_rid = [(k.rsplit("|", 1), v) for k, v in rid.items()]
    rows_ch = [(k.rsplit("|", 1), v) for k, v in ch.items()]
    by_rest_hours = {}
    for k, ws in rh.items():
        rn, c, h = k.rsplit("|", 2)
        by_rest_hours.setdefault((rn, c), {})[int(h)] = ws
    rider_name = lambda r: names.get(r) or r[:8]

    def top_rest(i_sv, extra, lim=8):
        xs = sorted((x for x in rows_rest if x[1][i_sv] >= 1), key=lambda x: -x[1][i_sv])[:lim]
        return [dict({"restaurant": rn, "city": c, "saved": int(v[i_sv]), "n": int(v[0]), "late": int(v[1])}, **extra(rn, c, v)) for (rn, c), v in xs]

    def top_ch(j, lim=8):
        xs = sorted((x for x in rows_ch if x[1][24 + j] >= 1), key=lambda x: -x[1][24 + j])[:lim]
        return [{"city": c, "hour": int(h), "saved": int(v[24 + j]), "n": int(v[0]), "within_pct": _pct(v[1], v[0]),
                 "avg": avg(v[4 + 2 * j], v[5 + 2 * j]), "avg_late": avg(v[14 + 2 * j], v[15 + 2 * j])} for (c, h), v in xs]

    def top_riders(i_sv, i_s, lim=8):
        xs = sorted((x for x in rows_rid if x[1][i_sv] >= 1), key=lambda x: (-x[1][i_sv], -x[1][0]))[:lim]
        return [{"rider_id": r, "rider": rider_name(r), "city": c, "fleet": store.fleet_map.get(r, ""), "saved": int(v[i_sv]), "n": int(v[0]),
                 "late": int(v[1]), "avg": avg(v[i_s], v[i_s + 1]), "handbacks": hb.get(f"{r}|{c}", 0)} for (r, c), v in xs]
    fixes = []
    for j, (k, label, norm) in enumerate(U30_STAGES):
        sv = int(tot[24 + j])
        f = {"key": k, "label": label, "norm": norm, "saved": sv, "pts": _pct(sv, n) or 0.0, "avg_late": stages[j]["late"]}
        if k == "wt":
            f["restaurants"] = top_rest(2, lambda rn, c, v: {"avg_wait": avg(v[4], v[5]), "lost_h": round(v[3] / 60, 1),
                                                             "send_later": _send_later(by_rest_hours.get((rn, c), {}))})
        elif k == "tc":
            f["restaurants"] = top_rest(6, lambda rn, c, v: {"avg_ride": avg(v[7], v[8])})
        elif k == "tr":
            f["hours"] = top_ch(j)
        elif k == "acc":
            f["hours"] = top_ch(j); f["riders"] = top_riders(2, 3)
        elif k == "ho":
            f["riders"] = top_riders(5, 6)
        fixes.append(f)
    fixes.sort(key=lambda f: -f["saved"])
    # city × hour: within target, and the fix that matters most there
    grid = {}
    for (c, h), v in rows_ch:
        g = grid.setdefault(c, {"city": c, "n": 0, "within": 0, "late": 0, "ptod_s": 0.0, "sv": [0] * 5, "hours": []})
        g["n"] += v[0]; g["within"] += v[1]; g["late"] += v[2]; g["ptod_s"] += v[3]
        for j in range(5):
            g["sv"][j] += v[24 + j]
        main = max(range(5), key=lambda j: v[24 + j])
        g["hours"].append({"hour": int(h), "n": int(v[0]), "within_pct": _pct(v[1], v[0]), "late": int(v[2]),
                           "cause": _UK[main] if v[24 + main] >= 1 else None, "saved": {_UK[j]: int(v[24 + j]) for j in range(5) if v[24 + j] >= 1}})
    cities = []
    for g in grid.values():
        g["hours"].sort(key=lambda x: x["hour"])
        main = max(range(5), key=lambda j: g["sv"][j])
        cities.append({"city": g["city"], "n": int(g["n"]), "late": int(g["late"]), "within_pct": _pct(g["within"], g["n"]),
                       "avg_ptod": avg(g["ptod_s"], g["n"]), "main": _UK[main] if g["sv"][main] >= 1 else None,
                       "main_pts": _pct(g["sv"][main], g["n"]) or 0.0, "pts": {_UK[j]: _pct(g["sv"][j], g["n"]) or 0.0 for j in range(5)},
                       "hours": g["hours"]})
    cities.sort(key=lambda c: -c["n"])
    best = fixes[0] if fixes and fixes[0]["saved"] else None
    return {"period": P, "city": city, "target": t, "target_within_pct": rules.target_within_pct,
            "delivered": int(n), "late": int(late), "within_pct": _pct(tot[1], n), "prev_within_pct": prev_within,
            "avg_ptod": avg(tot[3], n), "stages": stages, "fixes": fixes, "cities": cities,
            "best": {"key": best["key"], "pts": best["pts"], "saved": best["saved"]} if best else None,
            "top3_pts": round(sum(f["pts"] for f in fixes[:3]), 1),
            "budget": {k: v for k, _, v in U30_STAGES}}


def stage_avgs(store, rules, now: datetime) -> dict:
    """Typical minutes per stage per city over the last 7 finished days (for the live 'will it be late?' check)."""
    def build():
        today = day_key(now)
        datas = [perf_day(store, rules, (_d(today) - timedelta(days=i)).isoformat(), today) for i in range(1, 8)]
        out = {}
        for c in {c for x in datas for c in x["tot"]} | {""}:
            v = _agg(datas, c)[0]
            f = lambda s_, n_, dflt: round(v[I[s_]] / v[I[n_]], 1) if v[I[n_]] >= 20 else dflt
            out[c] = {"tr": f("tr_s", "tr_n", 8.0), "wt": f("wt_s", "wt_n", 6.0), "tc": f("tc_s", "tc_n", 12.0), "ho": f("ho_s", "ho_n", 3.0)}
        return out
    return cached(("stage_avgs", day_key(now)), 600, build)


def late_risk(orders: list, now: datetime, target: int, avgs: dict, city_of) -> dict:
    """Live orders that will go over the target unless someone acts, with the stage they're stuck in and what to do."""
    def mins(a):
        return (now - a).total_seconds() / 60 if a else None
    risk, over = [], []
    for o in orders:
        ph, disp = o.get("phase"), o.get("dispatched_at")
        if ph in ("delivered", "cancelled", "closed", "on_hold") or not disp:
            continue
        c = city_of(o) or ""
        A = avgs.get(c) or avgs.get("") or {"tr": 8.0, "wt": 6.0, "tc": 12.0, "ho": 3.0}
        el = mins(disp)
        if el is None or el < 0 or el > 180:
            continue
        acc, at_r, pick, at_c = o.get("accepted_at"), o.get("at_restaurant_at"), o.get("picked_up_at"), o.get("at_customer_at")
        # stuck = this step already takes clearly longer than usual → there is something to do now
        if at_c:
            x = mins(at_c); stage, since = "ho", x; rem = max(0.5, A["ho"] - x); stuck = x >= 4
            act = f"At the door for {round(x)}′ — the rider should call the customer" if stuck else "At the customer — almost done"
        elif pick:
            x = mins(pick); stage, since = "tc", x; rem = max(1.0, A["tc"] - x) + A["ho"]; stuck = x > A["tc"] + 5
            act = f"Riding to the customer for {round(x)}′ — check where the rider is" if stuck else "Riding to the customer — nothing to do but watch it"
        elif at_r:
            x = mins(at_r); stage, since = "wt", x; rem = max(1.0, A["wt"] - x) + A["tc"] + A["ho"]; stuck = x >= 8
            act = f"Food not ready after {round(x)}′ — call the restaurant" if stuck else "Waiting for the food — ask the kitchen to hurry"
        elif acc or o.get("rider_id"):
            x = mins(acc) if acc else el; stage, since = "tr", x; rem = max(1.0, A["tr"] - x) + A["wt"] + A["tc"] + A["ho"]; stuck = x > A["tr"] + 3
            act = f"On the way to the restaurant for {round(x)}′ — call the rider or give it to a closer one" if stuck else "Rider on the way — ask the restaurant to have the food ready"
        else:
            stage, since = "acc", el; rem = A["tr"] + A["wt"] + A["tc"] + A["ho"]; stuck = el >= 3
            act = f"No rider after {round(el)}′ — assign one now"
        proj = el + rem
        row = {"id": o.get("id"), "ref": o.get("ref") or "", "city": c, "restaurant": o.get("restaurant") or "", "rider": o.get("rider") or "",
               "rider_id": o.get("rider_id") or "", "stage": stage, "since": round(since or 0), "elapsed": round(el), "projected": round(proj),
               "left": round(target - el), "action": act}
        if el > target:
            over.append(row)
        elif proj > target - 1 and (stuck or el >= 10):      # a fresh order that is going normally: nothing to do yet
            risk.append(row)
    risk.sort(key=lambda r: (r["left"], -r["projected"]))
    over.sort(key=lambda r: -r["elapsed"])
    return {"risk": risk[:60], "over": over[:60], "n_risk": len(risk), "n_over": len(over),
            "live": sum(1 for o in orders if o.get("phase") not in ("delivered", "cancelled", "closed", "on_hold") and o.get("dispatched_at"))}


# ====================================================================== 9.8 — accounts that move together
def _jd_hm(m):
    """minutes since the Julian epoch (SQLite julianday × 1440) → 'HH:MM' Berlin."""
    from datetime import timezone as _tz
    if m is None:
        return ""
    return datetime.fromtimestamp((m / 1440 - 2440587.5) * 86400, _tz.utc).astimezone(BERLIN).strftime("%H:%M")


def twins(store, now: datetime, days: int = 14, city: str = "", phones: dict = None, pick_win: float = 4, drop_win: float = 8) -> dict:
    """Pairs of rider accounts that keep doing the same orders at the same time: picked up at the same restaurant within
    `pick_win` min of each other and delivered within `drop_win` min of each other. Once or twice is normal (orders paired
    at peak); on most of their orders, on several days, two accounts move as one. GPS (kept 2 days) and working hours
    are added as extra evidence. Only shown in the dashboard — nobody is blocked or messaged automatically."""
    today = day_key(now)
    a = (_d(today) - timedelta(days=days)).isoformat()
    b = (_d(today) - timedelta(days=1)).isoformat()
    w, args = ("day BETWEEN ? AND ? AND phase='delivered' AND rider_id IS NOT NULL AND rider_id != '' "
               "AND picked_up_at IS NOT NULL AND delivered_at IS NOT NULL"), [a, b]
    if city:
        w += " AND city=?"; args.append(city)
    rows = store._rows("SELECT id, ref, rider_id, rider, fleet, city, restaurant, day, julianday(picked_up_at) * 1440 AS pm, "
                       f"julianday(delivered_at) * 1440 AS dm FROM orders WHERE {w} ORDER BY city, restaurant, pm", args)
    total, name, fleet, rcity = Counter(), {}, {}, {}
    span = {}                                      # (rider, day) -> [first pickup, last delivery]
    for r in rows:
        rid = r["rider_id"]
        total[rid] += 1
        name[rid] = r["rider"] or name.get(rid) or rid[:8]
        fleet[rid] = r["fleet"] or fleet.get(rid, "")
        rcity[rid] = r["city"] or rcity.get(rid, "")
        s = span.setdefault((rid, r["day"]), [r["pm"], r["dm"]])
        s[0] = min(s[0], r["pm"]); s[1] = max(s[1], r["dm"])
    pairs = {}
    n = len(rows)
    for i in range(n):
        x = rows[i]
        for j in range(i + 1, n):
            y = rows[j]
            if y["restaurant"] != x["restaurant"] or y["city"] != x["city"] or y["pm"] - x["pm"] > pick_win:
                break
            if y["rider_id"] == x["rider_id"] or abs(y["dm"] - x["dm"]) > drop_win:
                continue
            k = tuple(sorted((x["rider_id"], y["rider_id"])))
            o1, o2 = (x, y) if x["rider_id"] == k[0] else (y, x)
            p = pairs.setdefault(k, {"a": set(), "b": set(), "days": set(), "ex": [], "rest": Counter()})
            p["a"].add(o1["id"]); p["b"].add(o2["id"]); p["days"].add(x["day"]); p["rest"][x["restaurant"]] += 1
            if len(p["ex"]) < 400:
                p["ex"].append((x["day"], x["restaurant"], o1["ref"], o2["ref"], o1["pm"], o2["pm"], o1["dm"], o2["dm"]))
    out = []
    for (ra, rb), p in pairs.items():
        tw = min(len(p["a"]), len(p["b"]))
        base = min(total[ra], total[rb]) or 1
        share = tw / base
        if tw < 6 or len(p["days"]) < 2 or share < 0.3:
            continue
        both = [d for (r_, d) in span if r_ == ra and (rb, d) in span]
        same = sum(1 for d in both if abs(span[(ra, d)][0] - span[(rb, d)][0]) <= 15 and abs(span[(ra, d)][1] - span[(rb, d)][1]) <= 15)
        ex = sorted(p["ex"], key=lambda e: (e[0], e[4]), reverse=True)[:12]
        pa, pb = (phones or {}).get(ra, ""), (phones or {}).get(rb, "")
        digits = lambda s: "".join(ch for ch in str(s or "") if ch.isdigit())[-9:]
        out.append({"a": ra, "b": rb, "name_a": name[ra], "name_b": name[rb], "fleet_a": fleet[ra], "fleet_b": fleet[rb],
                    "city": rcity[ra] or rcity[rb], "together": tw, "share": round(share * 100), "days": len(p["days"]),
                    "deliveries_a": total[ra], "deliveries_b": total[rb], "shared_days": len(both), "same_hours_days": same,
                    "same_phone": bool(digits(pa)) and digits(pa) == digits(pb),
                    "restaurants": [r_ for r_, _ in p["rest"].most_common(3)],
                    "level": "strong" if share >= 0.6 and len(p["days"]) >= 3 else "likely" if share >= 0.45 else "check",
                    "examples": [{"day": e[0], "restaurant": e[1], "ref_a": e[2], "ref_b": e[3], "pick_a": _jd_hm(e[4]), "pick_b": _jd_hm(e[5]),
                                  "drop_a": _jd_hm(e[6]), "drop_b": _jd_hm(e[7])} for e in ex]})
    # GPS: how often both accounts were within 120 m of each other while moving (positions are kept 2 days)
    ids = sorted({x for p in out for x in (p["a"], p["b"])})
    gps = {}
    if ids:
        since = (now - timedelta(days=2)).isoformat(timespec="seconds")
        q = ",".join("?" * len(ids))
        for r in store._rows(f"SELECT rider_id, CAST(julianday(at) * 1440 AS INTEGER) AS m, lat, lng FROM positions WHERE at >= ? AND rider_id IN ({q})",
                             [since] + ids):
            gps.setdefault(r["rider_id"], {})[r["m"]] = (r["lat"], r["lng"])
    for p in out:
        ga, gb = gps.get(p["a"], {}), gps.get(p["b"], {})
        both = close = 0
        for m, (la, lo) in ga.items():
            q2 = gb.get(m) or gb.get(m - 1) or gb.get(m + 1)
            if not q2:
                continue
            prev = ga.get(m - 5) or ga.get(m - 4) or ga.get(m - 6)
            moving = prev is not None and _meters(prev, (la, lo)) > 100
            if not moving:
                continue
            both += 1
            close += _meters((la, lo), q2) <= 120
        p["gps_minutes"] = both
        p["gps_together"] = round(close / both * 100) if both >= 15 else None
        if p["gps_together"] is not None and p["gps_together"] >= 70 and p["level"] != "strong":
            p["level"] = "likely" if p["level"] == "check" else "strong"
    rank = {"strong": 0, "likely": 1, "check": 2}
    out.sort(key=lambda p: (rank[p["level"]], -p["share"], -p["together"]))
    return {"from": a, "to": b, "days": days, "city": city, "pairs": out[:60], "riders": len(total),
            "rule": f"picked up at the same restaurant within {pick_win:g} min and delivered within {drop_win:g} min of each other"}


def _meters(p, q):
    la1, lo1 = map(math.radians, p); la2, lo2 = map(math.radians, q)
    x = (lo2 - lo1) * math.cos((la1 + la2) / 2)
    return math.hypot(x, la2 - la1) * 6371000
