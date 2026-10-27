"""PDF report: EVERY order whose rider waited long at the restaurant (first rider arrival -> pickup).

Dashboard: Orders page and Daily report -> "⬇ PDF long waits" (minutes adjustable, default 10).
The wait is the kitchen's wait: from the FIRST rider's arrival until the food was picked up, so a rider who gave up
and handed the order back still counts. No customer data in the report.

collect() turns stored orders into plain rows (runs on the server); render() draws the PDF from those rows and is
run in a separate process by app.py, so a week with 10 000+ orders never slows the live dashboard.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from fpdf import FPDF

from orders import BERLIN, restaurant_waits, ts

PURPLE = (124, 58, 237)
LILAC = (244, 240, 255)
GROUP = (232, 223, 255)
GREY = (110, 110, 120)
INK = (30, 24, 52)
ROWS_MAX = 60000                        # a safety net only (a busy month is ~50 000 long waits)
_HERE = os.path.dirname(os.path.abspath(__file__))
FONT = os.path.join(_HERE, "DejaVuSansCondensed.ttf")
FONT_B = os.path.join(_HERE, "DejaVuSansCondensed-Bold.ttf")
_SUBST = {"™": "TM", "–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "„": '"', "…": "...", "•": "-", "′": "'", "≥": ">="}


# ------------------------------------------------------------------ data (server side)
def _hm(dt) -> str:
    return dt.astimezone(BERLIN).strftime("%H:%M") if dt else "–"


def _period_label(period: str, start: datetime, end: datetime, now: datetime) -> str:
    d = lambda x: x.astimezone(BERLIN).strftime("%a %d %b %Y")
    if len(period) == 10 and period[4] == "-":
        return d(start)
    names = {"today": "Today", "yesterday": "Yesterday", "week": "This week", "lastweek": "Last week", "month": "This month"}
    last = min(end, now)
    last = last - timedelta(seconds=1)                                      # end is exclusive: Mon 00:00 -> Sun
    if period == "Custom":                                                  # a from–to range picked on the dashboard
        return d(start) + (f" – {d(last)}" if d(last) != d(start) else "")
    return f"{names.get(period, period)} ({d(start)}" + (f" – {d(last)})" if d(last) != d(start) else ")")


def collect(orders: list, min_wait: int, start: datetime, end: datetime, now: datetime, period: str, city: str,
            reached_db: dict | None = None) -> dict:
    """Plain, picklable report data: every order that waited >= min_wait, grouped per restaurant."""
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
        phase = o.get("phase")
        wait, note = kitchen, ""
        if wait is None and phase == "at_restaurant" and not o.get("picked_up_at"):
            wait, note = (now - arrival).total_seconds() / 60, "still waiting"
        per_rider = [w for w, _ in waits.values() if w is not None]
        if wait is None and per_rider:
            wait = max(per_rider)                      # nobody picked it up: the longest a rider waited
        if wait is None or wait < min_wait:
            continue
        names = {h.get("rider_id"): h.get("rider") for h in hist if h.get("rider_id") and h.get("rider")}
        gave_up = [f"{names.get(r) or 'rider'} waited {round(w)}' and handed it back" for r, (w, g) in waits.items() if g and w is not None]
        if phase == "cancelled":
            why = o.get("cancel_reason") or ""
            note = "cancelled" + (f" ({why})" if why and why != "cancelled in MotionTools" else "")
        ptod = (o.get("phases") or {}).get("ptod")
        rows.append({
            "key": key, "t": arrival.timestamp(), "day": arrival.astimezone(BERLIN).strftime("%a %d.%m."),
            "ref": o.get("ref") or "", "rider": o.get("rider") or "–",
            "dispatched": _hm(o.get("dispatched_at")), "accepted": _hm(o.get("accepted_at")), "arrived": _hm(arrival),
            "picked": _hm(o.get("picked_up_at")), "wait": round(wait), "delivered": _hm(o.get("delivered_at")),
            "ptod": f"{round(ptod)} min" if ptod is not None and phase == "delivered" else "–",
            "note": "; ".join(x for x in [note] + gave_up if x)})
        if len(rows) >= ROWS_MAX:
            break
    if reached_db:                              # the database counted every order that reached each restaurant
        reached = {k: max(v, reached.get(k, 0)) for k, v in {**reached, **reached_db}.items()}
    by = {}
    for r in rows:
        by.setdefault(r["key"], []).append(r)
    groups = sorted(by.items(), key=lambda kv: (-len(kv[1]), -max(x["wait"] for x in kv[1])))
    out_groups = []
    for (rest, cty), rs in groups:
        ws = [x["wait"] for x in rs]
        n_all = reached.get((rest, cty), 0)
        rs.sort(key=lambda x: x["t"])
        out_groups.append({"restaurant": rest, "city": cty, "n": len(rs), "of_all": n_all,
                           "share": f"{round(100 * len(rs) / n_all)} %" if n_all else "–",
                           "avg": round(sum(ws) / len(ws)), "max": max(ws),
                           "rows": [[x["day"], x["ref"], x["rider"], x["dispatched"], x["accepted"], x["arrived"], x["picked"],
                                     f"{x['wait']} min", x["delivered"], x["ptod"], x["note"]] for x in rs]})
    waits = [r["wait"] for r in rows]
    where = city or "All cities"
    plabel = _period_label(period, start, end, now)
    return {"where": where, "plabel": plabel, "min_wait": min_wait, "created": now.astimezone(BERLIN).strftime("%d %b %Y %H:%M"),
            "total_reached": sum(reached.values()), "n": len(rows), "avg": round(sum(waits) / len(waits)) if waits else None,
            "max": max(waits) if waits else None, "groups": out_groups, "capped": len(rows) >= ROWS_MAX,
            "title": f"Quickzi – long restaurant waits – {where} – {plabel}"}


# ------------------------------------------------------------------ drawing (separate process)
class _Doc(FPDF):
    title_line = ""
    fam = "Helvetica"
    txt = staticmethod(lambda s: s)

    def footer(self):
        self.set_y(-9)
        self.set_font(self.fam, "", 7)
        self.set_text_color(*GREY)
        self.cell(0, 5, self.txt(f"{self.title_line}  ·  page {self.page_no()}/{{nb}}"), align="R")


def _num(n) -> str:
    return f"{n:,}".replace(",", ".")                                       # 11.811 (German thousands)


def _orders(n) -> str:
    return f"{_num(n)} order" + ("" if n == 1 else "s")


def _latin(s) -> str:
    s = "".join(_SUBST.get(c, c) for c in str(s))
    return s.encode("latin-1", "replace").decode("latin-1")


def render(data: dict) -> bytes:
    pdf = _Doc(orientation="L", format="A4")
    if os.path.exists(FONT) and os.path.exists(FONT_B):            # every letter (ệ, ş, ł, ...)
        pdf.add_font("DejaVu", "", FONT); pdf.add_font("DejaVu", "B", FONT_B)
        fam, T = "DejaVu", (lambda s: str(s if s is not None else ""))
    else:                                                          # fallback: Western European letters only
        fam, T = "Helvetica", (lambda s: _latin(s if s is not None else ""))
    pdf.fam, pdf.txt = fam, T
    pdf.title_line = data["title"]
    pdf.set_title(T(data["title"])); pdf.set_author("Quickzi")
    pdf.set_auto_page_break(True, margin=13)
    pdf.set_margins(12, 11, 12)
    pdf.add_page()
    W = pdf.w - 24

    def fit(text, width):
        text = T(text)
        if not text or pdf.get_string_width(text) <= width:
            return text
        while text and pdf.get_string_width(text + "…") > width:
            text = text[:-1]
        return text + "…"

    # ---- title + summary
    pdf.set_fill_color(*PURPLE); pdf.rect(0, 0, pdf.w, 3, "F")
    pdf.set_font(fam, "B", 18); pdf.set_text_color(*INK)
    pdf.cell(0, 10, T("Long restaurant waits"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(fam, "", 9.5); pdf.set_text_color(*GREY)
    pdf.cell(0, 5.5, T(f"Quickzi  ·  {data['where']}  ·  {data['plabel']}  ·  created {data['created']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.multi_cell(0, 5, T(f"Every order where the rider waited {data['min_wait']} min or more at the restaurant. Waited = from the first "
                           f"rider's arrival to pickup (a rider who gave up and handed the order back still counts). "
                           f"Delivery time = dispatch to delivered."), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2.5)
    pdf.set_font(fam, "B", 12); pdf.set_text_color(*INK)
    if not data["n"]:
        pdf.cell(0, 7, T(f"No order waited {data['min_wait']} min or more at a restaurant "
                         f"({_orders(data['total_reached'])} reached a restaurant)."), new_x="LMARGIN", new_y="NEXT")
        return bytes(pdf.output())
    share = f"{round(100 * data['n'] / data['total_reached'])} %" if data["total_reached"] else "–"
    pdf.cell(0, 7, T(f"{_orders(data['n'])} waited {data['min_wait']}+ min  ·  {share} of {_orders(data['total_reached'])} that "
                     f"reached a restaurant  ·  average wait {data['avg']} min  ·  longest {data['max']} min"), new_x="LMARGIN", new_y="NEXT")
    if data["capped"]:
        pdf.set_font(fam, "", 9); pdf.set_text_color(180, 30, 30)
        pdf.cell(0, 5, T(f"Very large period: the first {_orders(ROWS_MAX)} are listed — choose one city for the rest."),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ---- 1) restaurants
    def table_head(cols, widths, aligns, size):
        pdf.set_font(fam, "B", size); pdf.set_fill_color(*PURPLE); pdf.set_text_color(255, 255, 255)
        for c, w, a in zip(cols, widths, aligns):
            pdf.cell(w, 5.6, T(c), align=a, fill=True)
        pdf.ln(5.6)
        pdf.set_font(fam, "", size); pdf.set_text_color(*INK); pdf.set_fill_color(*LILAC)

    pdf.set_font(fam, "B", 11); pdf.set_text_color(*INK)
    pdf.cell(0, 7, T(f"By restaurant ({len(data['groups'])})"), new_x="LMARGIN", new_y="NEXT")
    rc = ("Restaurant", "City", "Long waits", "Of its orders", "Share", "Avg wait", "Longest")
    rw = (W - 52 - 26 * 5, 52, 26, 26, 26, 26, 26)
    ra = ("L", "L", "R", "R", "R", "R", "R")
    table_head(rc, rw, ra, 8.5)
    for i, g in enumerate(data["groups"]):
        if pdf.get_y() + 5 > pdf.page_break_trigger:
            pdf.add_page(); table_head(rc, rw, ra, 8.5)
        vals = (g["restaurant"], g["city"], str(g["n"]), str(g["of_all"] or "–"), g["share"], f"{g['avg']} min", f"{g['max']} min")
        for v, w, a in zip(vals, rw, ra):
            pdf.cell(w, 5, fit(v, w - 1.5) if a == "L" else T(v), align=a, fill=(i % 2 == 1))
        pdf.ln(5)

    # ---- 2) every order, grouped per restaurant
    pdf.add_page()
    pdf.set_font(fam, "B", 11); pdf.set_text_color(*INK)
    pdf.cell(0, 7, T(f"All {_orders(data['n'])}, per restaurant"), new_x="LMARGIN", new_y="NEXT")
    oc = ("Date", "Order", "Rider", "Dispatched", "Accepted", "Arrived", "Picked up", "Waited", "Delivered", "Delivery time", "Note")
    fixed = (19, 17, 34, 17, 16, 15, 17, 15, 17, 20)
    ow = fixed + (W - sum(fixed),)
    oa = ("L", "L", "L", "R", "R", "R", "R", "R", "R", "R", "L")
    H = 4.7

    def group_bar(g, cont=False):
        pdf.set_font(fam, "B", 9); pdf.set_fill_color(*GROUP); pdf.set_text_color(*INK)
        label = (f"{g['restaurant']}" + (f"  ·  {g['city']}" if g["city"] else "") + f"  —  {_orders(g['n'])} waited {data['min_wait']}+ min"
                 f"  ·  {g['share']} of its orders  ·  avg {g['avg']} min  ·  longest {g['max']} min") + ("  (continued)" if cont else "")
        pdf.cell(W, 6, fit(label, W - 2), fill=True, new_x="LMARGIN", new_y="NEXT")

    for g in data["groups"]:
        if pdf.get_y() + 6 + 5.6 + 2 * H > pdf.page_break_trigger:
            pdf.add_page()
        pdf.ln(1.5)
        group_bar(g)
        table_head(oc, ow, oa, 7.6)
        for i, r in enumerate(g["rows"]):
            if pdf.get_y() + H > pdf.page_break_trigger:
                pdf.add_page(); group_bar(g, cont=True); table_head(oc, ow, oa, 7.6)
            for j, (v, w, a) in enumerate(zip(r, ow, oa)):
                pdf.cell(w, H, fit(v, w - 1.4) if j in (2, 10) else T(v), align=a, fill=(i % 2 == 1))
            pdf.ln(H)
    return bytes(pdf.output())


def build_pdf(orders: list, period: str, start: datetime, end: datetime, now: datetime, city: str, min_wait: int,
              reached: dict | None = None) -> bytes:
    """Both steps in one call (tests / small reports)."""
    return render(collect(orders, min_wait, start, end, now, period, city, reached))
