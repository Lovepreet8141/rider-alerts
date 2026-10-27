"""Data for the premium dashboard (/v2): Performance, Restaurants, Riders & fleets.

Every number comes from SQL aggregates on the stored orders (indexed by day and city), so a day, a week or a month
answers in milliseconds-to-a-second without loading orders one by one. Live parts (riders right now) are added by
app.py from memory. No customer data leaves here.
"""
from __future__ import annotations

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


def performance(store, rules, now: datetime, period: str = "today", city: str = "", frm: str = "", to: str = "") -> dict:
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
