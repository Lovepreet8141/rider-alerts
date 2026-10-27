"""PDF report: orders where riders waited long at the restaurant (first rider arrival -> pickup).

Dashboard: Orders page and Daily report -> "⬇ PDF long waits" (minutes adjustable, default 10).
The wait is the kitchen's wait: from the FIRST rider's arrival until the food was picked up, so a rider who gave up
and handed the order back still counts. No customer data in the report.
"""
from __future__ import annotations

from datetime import datetime

from fpdf import FPDF

from orders import BERLIN, restaurant_waits, ts

PURPLE = (124, 58, 237)
LILAC = (243, 238, 255)
GREY = (110, 110, 120)
DETAIL_MAX = 2000                       # longest waits first if a period has more (the CSV has every order)
_SUBST = {"™": "TM", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "„": '"', "…": "...", "•": "-", "′": "'", "≥": ">="}


def _t(s) -> str:
    """The PDF's built-in font knows Western European letters (ä ö ü ß é ®); anything else becomes '?'."""
    s = "".join(_SUBST.get(c, c) for c in str(s if s is not None else ""))
    return s.encode("latin-1", "replace").decode("latin-1")


def _hm(dt) -> str:
    return dt.astimezone(BERLIN).strftime("%H:%M") if dt else "-"


def _period_label(period: str, start: datetime, end: datetime, now: datetime) -> str:
    d = lambda x: x.astimezone(BERLIN).strftime("%d %b %Y")
    if len(period) == 10 and period[4] == "-":
        return d(start)
    names = {"today": "Today", "yesterday": "Yesterday", "week": "This week", "lastweek": "Last week", "month": "This month"}
    last = min(end, now)
    return f"{names.get(period, period)} ({d(start)}" + (f" - {d(last)})" if d(last) != d(start) else ")")


def wait_rows(orders: list, min_wait: int, start: datetime, end: datetime, now: datetime):
    """-> (rows of orders that waited >= min_wait, orders that reached the restaurant per restaurant key)."""
    rows, reached = [], {}
    for o in orders:
        hist = o.get("history") or []
        firsts = [t for t in (ts(h.get("at")) for h in hist if h.get("what") == "arrived_restaurant") if t]
        if o.get("at_restaurant_at"):
            firsts.append(o["at_restaurant_at"])
        arrival = min(firsts) if firsts else None
        if not arrival or not (start <= arrival < end):
            continue
        key = (o.get("restaurant") or "?", o.get("city") or "")
        reached[key] = reached.get(key, 0) + 1
        kitchen, waits = restaurant_waits(o)
        status = "delivered" if o.get("phase") == "delivered" else "cancelled" if o.get("phase") == "cancelled" else "live"
        wait, note = kitchen, ""
        if wait is None and status == "live" and not o.get("picked_up_at") and o.get("phase") == "at_restaurant":
            wait, note = (now - arrival).total_seconds() / 60, "still waiting"
        per_rider = [w for w, _ in waits.values() if w is not None]
        if wait is None and per_rider:
            wait = max(per_rider)                      # nobody picked it up: the longest a rider waited
        if wait is None or wait < min_wait:
            continue
        names = {h.get("rider_id"): h.get("rider") for h in hist if h.get("rider_id") and h.get("rider")}
        gave_up = [f"{names.get(r) or 'rider'} {round(w)}' then handed back" for r, (w, g) in waits.items() if g and w is not None]
        if status == "cancelled":
            why = o.get("cancel_reason") or ""
            note = "cancelled" + (f" ({why})" if why and why != "cancelled in MotionTools" else "")
        note = "; ".join(x for x in [note] + gave_up if x)
        rows.append({"key": key, "day": arrival.astimezone(BERLIN).strftime("%d.%m."), "arrived": arrival, "ref": o.get("ref") or "",
                     "restaurant": key[0], "city": key[1], "rider": o.get("rider") or "-", "picked": o.get("picked_up_at"),
                     "wait": round(wait), "note": note})
    return rows, reached


def _fit(pdf, text: str, width: float) -> str:
    """Cut a cell's text to its column width (one line per row keeps 2 000 rows fast)."""
    text = _t(text)
    if pdf.get_string_width(text) <= width:
        return text
    while text and pdf.get_string_width(text + "...") > width:
        text = text[:-1]
    return text + "..."


def _table(pdf, headers, widths, aligns, rows, size=8.5, h=5.4):
    """Fast plain table: purple header repeated on every page, light lilac zebra rows."""
    def head():
        pdf.set_font("Helvetica", "B", size); pdf.set_fill_color(*PURPLE); pdf.set_text_color(255, 255, 255)
        for t, w, a in zip(headers, widths, aligns):
            pdf.cell(w, h + 0.6, _t(t), align=a, fill=True)
        pdf.ln(h + 0.6)
        pdf.set_font("Helvetica", "", size); pdf.set_text_color(30, 30, 40); pdf.set_fill_color(*LILAC)
    head()
    for i, r in enumerate(rows):
        if pdf.get_y() + h > pdf.page_break_trigger:
            pdf.add_page(); head()
        for v, w, a in zip(r, widths, aligns):
            pdf.cell(w, h, _fit(pdf, v, w - 1.6), align=a, fill=(i % 2 == 1))
        pdf.ln(h)


class _Doc(FPDF):
    title_line = ""

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*GREY)
        self.cell(0, 5, _t(f"{self.title_line}  -  page {self.page_no()}/{{nb}}"), align="R")


def build_pdf(orders: list, period: str, start: datetime, end: datetime, now: datetime, city: str, min_wait: int,
              reached: dict | None = None) -> bytes:
    rows, seen = wait_rows(orders, min_wait, start, end, now)
    if reached is None:
        reached = seen
    else:                                       # the database counted every order that reached each restaurant
        reached = {k: max(v, seen.get(k, 0)) for k, v in {**seen, **reached}.items()}
    plabel = _period_label(period, start, end, now)
    where = city or "All cities"

    # by restaurant: most long waits first
    by = {}
    for r in rows:
        by.setdefault(r["key"], []).append(r["wait"])
    rank = sorted(by.items(), key=lambda kv: (-len(kv[1]), -max(kv[1])))
    order_of = {k: i for i, (k, _) in enumerate(rank)}
    detail = sorted(rows, key=lambda r: (order_of[r["key"]], r["arrived"]))
    cut = len(detail) > DETAIL_MAX
    if cut:
        keep = set(id(r) for r in sorted(rows, key=lambda r: -r["wait"])[:DETAIL_MAX])
        detail = [r for r in detail if id(r) in keep]

    pdf = _Doc(orientation="L", format="A4")
    pdf.title_line = f"Quickzi - long restaurant waits - {where} - {plabel}"
    pdf.set_title(_t(pdf.title_line)); pdf.set_author("Quickzi")
    pdf.set_auto_page_break(True, margin=14)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()

    # title
    pdf.set_fill_color(*PURPLE); pdf.rect(0, 0, pdf.w, 3, "F")
    pdf.set_font("Helvetica", "B", 18); pdf.set_text_color(30, 20, 60)
    pdf.cell(0, 10, _t("Long restaurant waits"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10); pdf.set_text_color(*GREY)
    pdf.cell(0, 6, _t(f"Quickzi  -  {where}  -  {plabel}  -  rider waited {min_wait} min or more at the restaurant "
                      f"(first arrival to pickup)  -  created {now.astimezone(BERLIN).strftime('%d %b %Y %H:%M')}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # summary
    total = sum(reached.values())
    pdf.set_font("Helvetica", "B", 12); pdf.set_text_color(30, 20, 60)
    if rows:
        waits = [r["wait"] for r in rows]
        share = f"{round(100 * len(rows) / total)} %" if total else "-"
        pdf.cell(0, 7, _t(f"{len(rows)} orders waited {min_wait}+ min  -  {share} of {total} orders that reached a restaurant  -  "
                          f"average wait {round(sum(waits) / len(waits))} min  -  longest {max(waits)} min"), new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 7, _t(f"No order waited {min_wait} min or more at a restaurant ({total} orders reached a restaurant)."),
                 new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())
    pdf.ln(3)

    # 1) restaurants
    pdf.set_font("Helvetica", "B", 11); pdf.set_text_color(30, 20, 60)
    pdf.cell(0, 7, _t("By restaurant"), new_x="LMARGIN", new_y="NEXT")
    rrows = []
    for (rest, cty), ws in rank:
        n_all = reached.get((rest, cty), 0)
        rrows.append([rest, cty, str(len(ws)), f"{round(100 * len(ws) / n_all)} %" if n_all else "-",
                      f"{round(sum(ws) / len(ws))} min", f"{max(ws)} min"])
    _table(pdf, ("Restaurant", "City", "Long waits", "Of its orders", "Avg wait", "Longest"), (110, 50, 28, 32, 26, 26),
           ("L", "L", "R", "R", "R", "R"), rrows, size=9, h=5.8)
    pdf.ln(5)

    # 2) orders
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, _t("Orders" + (f" (the {DETAIL_MAX} longest waits - the CSV has every order)" if cut else "")),
             new_x="LMARGIN", new_y="NEXT")
    _table(pdf, ("Date", "Arrived", "Order", "Restaurant", "City", "Rider", "Pickup", "Waited", "Note"),
           (14, 15, 20, 70, 32, 36, 16, 16, 54), ("L", "L", "L", "L", "L", "L", "L", "R", "L"),
           [[r["day"], _hm(r["arrived"]), r["ref"], r["restaurant"], r["city"], r["rider"], _hm(r["picked"]), f"{r['wait']} min", r["note"]]
            for r in detail])
    return bytes(pdf.output())
