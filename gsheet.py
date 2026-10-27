"""The daily "Long restaurant waits" Google Sheet (9.7).

Access: a Google *service account* (a robot user). Its key — the JSON file Google gives you — lives ONLY in Railway as
GOOGLE_SERVICE_ACCOUNT_JSON: never in the dashboard, never on GitHub. Share the sheet with the robot's e-mail address
(…@….iam.gserviceaccount.com) as Editor; the system can then write that one sheet and nothing else of yours.
The sheet's link is pasted in Settings → Slack.

Only these tabs are written (other tabs are left alone): Yesterday · Last week · Yesterday orders · Last week orders ·
How it works. Everything is sorted by city A–Z. No customer data: order numbers, restaurants, riders and times only.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime

import httpx

log = logging.getLogger("gsheet")
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = (os.environ.get("GOOGLE_SHEETS_API") or "https://sheets.googleapis.com").rstrip("/")     # override only for tests
TOKEN_URL = (os.environ.get("GOOGLE_TOKEN_URL") or "").strip()                                 # override only for tests
TABS = ["Yesterday", "Last week", "Yesterday orders", "Last week orders", "How it works"]
CHUNK = 1500                                   # rows per write call (keeps every request far below Google's 10 MB)


# ------------------------------------------------------------------ access
def _info() -> dict | None:
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):                # also accepted: the JSON as base64
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    return d if d.get("client_email") and d.get("private_key") else None


def robot_email() -> str:
    i = _info()
    return i["client_email"] if i else ""


def key_problem() -> str:
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return "GOOGLE_SERVICE_ACCOUNT_JSON is not set in Railway"
    return "" if _info() else "GOOGLE_SERVICE_ACCOUNT_JSON in Railway is not a valid service-account key (paste the whole JSON file)"


def sheet_id_from(text: str) -> str:
    """The sheet id from a Google Sheets link (or the bare id)."""
    t = (text or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", t)
    if m:
        return m.group(1)
    return t if re.fullmatch(r"[A-Za-z0-9_-]{20,}", t) else ""


def sheet_url(sid: str, gid=None) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" + (f"#gid={gid}" if gid is not None else "")


class GoogleError(RuntimeError):
    pass


class Client:
    def __init__(self):
        self._tok, self._exp = "", 0.0
        self.http = httpx.Client(timeout=60)

    def token(self) -> str:
        if self._tok and time.time() < self._exp - 120:
            return self._tok
        info = _info()
        if not info:
            raise GoogleError(key_problem())
        from google.auth import crypt, jwt as gjwt
        signer = crypt.RSASigner.from_service_account_info(info)
        aud = info.get("token_uri") or "https://oauth2.googleapis.com/token"
        now = int(time.time())
        assertion = gjwt.encode(signer, {"iss": info["client_email"], "scope": SCOPE, "aud": aud, "iat": now, "exp": now + 3600})
        r = self.http.post(TOKEN_URL or aud, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                   "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
        if r.status_code != 200:
            raise GoogleError(f"Google login failed ({r.status_code}): {r.text[:160]}")
        d = r.json()
        self._tok, self._exp = d["access_token"], time.time() + float(d.get("expires_in", 3600))
        return self._tok

    def call(self, method: str, path: str, **kw):
        for attempt in range(5):
            r = self.http.request(method, API + path, headers={"Authorization": "Bearer " + self.token()}, **kw)
            if r.status_code in (429, 500, 502, 503) and attempt < 4:
                time.sleep(min(30, 2 ** attempt * 2))
                continue
            if r.status_code == 403:
                raise GoogleError(f"Google says no access to the sheet — share it with {robot_email()} as Editor")
            if r.status_code == 404:
                raise GoogleError("Google can't find the sheet — check the link in Settings → Slack")
            if r.status_code >= 300:
                raise GoogleError(f"Google Sheets {r.status_code}: {r.text[:200]}")
            return r.json() if r.content else {}
        raise GoogleError("Google Sheets is busy — try again later")

    def batch(self, sid: str, requests: list):
        if requests:
            return self.call("POST", f"/v4/spreadsheets/{sid}:batchUpdate", json={"requests": requests})


# ------------------------------------------------------------------ cells and formats
EPOCH = date(1899, 12, 30)


class Link:
    def __init__(self, text: str, url: str):
        self.text, self.url = text, url


def _rgb(h: str) -> dict:
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _cell(v) -> dict:
    if v is None or v == "":
        return {}
    if isinstance(v, Link):
        c = {"userEnteredValue": {"stringValue": v.text}}
        if v.url:
            c["userEnteredFormat"] = {"textFormat": {"link": {"uri": v.url}}}
        return c
    if isinstance(v, bool):
        return {"userEnteredValue": {"stringValue": "yes" if v else ""}}
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": round(float(v), 4)}}
    if isinstance(v, datetime):
        v = v.date()
    if isinstance(v, date):
        return {"userEnteredValue": {"numberValue": (v - EPOCH).days}}
    if isinstance(v, dtime):
        return {"userEnteredValue": {"numberValue": round((v.hour * 3600 + v.minute * 60 + v.second) / 86400, 6)}}
    return {"userEnteredValue": {"stringValue": str(v)}}


def fmt(bg=None, bold=None, italic=None, size=None, color=None, align=None, valign=None, wrap=None, num=None, underline=None, font=None):
    """A userEnteredFormat and its field mask (only the given parts are changed)."""
    f, tf, fields = {}, {}, []
    if bg:
        f["backgroundColor"] = _rgb(bg); fields.append("backgroundColor")
    for k, v in (("bold", bold), ("italic", italic), ("fontSize", size), ("underline", underline), ("fontFamily", font)):
        if v is not None:
            tf[k] = v; fields.append("textFormat." + k)
    if color:
        tf["foregroundColor"] = _rgb(color); fields.append("textFormat.foregroundColor")
    if tf:
        f["textFormat"] = tf
    if align:
        f["horizontalAlignment"] = align; fields.append("horizontalAlignment")
    if valign:
        f["verticalAlignment"] = valign; fields.append("verticalAlignment")
    if wrap is not None:
        f["wrapStrategy"] = "WRAP" if wrap else "OVERFLOW_CELL"; fields.append("wrapStrategy")
    if num:
        f["numberFormat"] = {"type": num[0], "pattern": num[1]}; fields.append("numberFormat")
    return f, ",".join("userEnteredFormat." + x for x in fields)


VIOLET, INK, AMBER, RED, LINK = "4B3591", "1F1A2B", "B45309", "B42318", "2B5CD6"
BAND = ["FFFFFF", "F4F1FB"]
DATE_F, TIME_F = ("DATE", "ddd dd.mm."), ("TIME", "hh:mm")
INT_F, ONE_F, PCT_F = ("NUMBER", "#,##0"), ("NUMBER", "0.0"), ("PERCENT", "0%")


class Tab:
    """One tab: values plus formatting, turned into Sheets API requests by write()."""
    def __init__(self, title, ncols, color, freeze=(0, 0), grid=True):
        self.title, self.ncols, self.color, self.freeze, self.grid = title, ncols, color, freeze, grid
        self.rows, self.styles, self.borders, self.cond, self.widths, self.heights, self.filter = [], [], [], [], {}, {}, None

    def add(self, row):
        self.rows.append(list(row) + [None] * (self.ncols - len(row)))
        return len(self.rows) - 1

    def style(self, r0, r1, c0, c1, **kw):
        self.styles.append((r0, r1, c0, c1) + fmt(**kw))

    def header(self, r, labels):
        for j, x in enumerate(labels):
            self.rows[r][j] = x
        self.style(r, r + 1, 0, len(labels), bg=VIOLET, bold=True, color="FFFFFF", align="CENTER", valign="MIDDLE", wrap=True)
        self.style(r, r + 1, 0, 1, align="LEFT")
        self.heights[r] = 40

    def wait_colors(self, r0, r1, c):
        g = (r0, r1, c, c + 1)
        self.cond += [(g, "NUMBER_GREATER_THAN_EQ", ["20"], {"backgroundColor": _rgb("F4CCCC"), "textFormat": {"bold": True, "foregroundColor": _rgb(RED)}}),
                      (g, "NUMBER_BETWEEN", ["15", "19.99"], {"backgroundColor": _rgb("FCE4D6"), "textFormat": {"bold": True, "foregroundColor": _rgb(AMBER)}}),
                      (g, "NUMBER_BETWEEN", ["10", "14.99"], {"backgroundColor": _rgb("FFF2CC")})]


# ------------------------------------------------------------------ the tabs
def _ck(s: str) -> str:
    return (s or "").casefold().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def _hours(h):
    return f"{h:02d}:00–{(h + 1) % 24:02d}:00" if h is not None else ""


def orders_tab(title: str, rows: list, color: str) -> Tab:
    """Every long-wait order: city A–Z → inside a city the restaurant with the most long waits first → day → time."""
    cols = ["Date", "City", "Restaurant", "Order", "Rider", "Dispatched", "Rider at\nrestaurant", "Picked up", "Waited\n(min)", "Lost\n(min)",
            "Delivered", "PTOD\n(min)", "Late?", "Note", "MotionTools"]
    t = Tab(title, len(cols), color, freeze=(1, 3))
    t.add([None] * len(cols)); t.header(0, cols)
    per = Counter((r["city"], r["rest"]) for r in rows)
    rows = sorted(rows, key=lambda r: (_ck(r["city"]), -per[(r["city"], r["rest"])], _ck(r["rest"]), r["date"], r["arr"] or dtime(0)))
    blocks, start, prev = [], 1, None
    for r in rows:
        i = t.add([r["date"], r["city"], r["rest"], r["ref"], r["rider"], r["disp"], r["arr"], r["pick"], r["wait"], max(0, r["wait"] - 5),
                   r["deliv"], r["ptod"], "late" if r["ptod"] is not None and r["ptod"] > 30 else "", r["note"],
                   Link("open ↗", r["link"]) if r.get("link") else ""])
        if r["city"] != prev and prev is not None:
            blocks.append((start, i)); start = i
        prev = r["city"]
    if rows:
        blocks.append((start, len(t.rows)))
    n = len(t.rows)
    t.style(1, n, 0, len(cols), font="Arial", size=10, valign="MIDDLE", align="CENTER")
    for c in (1, 2, 4, 13):
        t.style(1, n, c, c + 1, align="LEFT")
    t.style(1, n, 1, 2, bold=True); t.style(1, n, 8, 9, bold=True)
    t.style(1, n, 0, 1, num=DATE_F)
    for c in (5, 6, 7, 10):
        t.style(1, n, c, c + 1, num=TIME_F)
    t.style(1, n, 14, 15, color=LINK, underline=True)
    for k, (a, b) in enumerate(blocks):
        t.style(a, b, 0, len(cols), bg=BAND[k % 2])
        if k:
            t.borders.append((a, a + 1, 0, len(cols), "top"))
    t.wait_colors(1, n, 8)
    t.cond.append(((1, n, 12, 13), "TEXT_EQ", ["late"], {"textFormat": {"bold": True, "foregroundColor": _rgb(RED)}}))
    t.cond.append(((1, n, 11, 12), "NUMBER_GREATER", ["30"], {"textFormat": {"bold": True, "foregroundColor": _rgb(RED)}}))
    t.widths = dict(enumerate([84, 140, 250, 84, 120, 84, 84, 84, 70, 60, 84, 70, 60, 230, 96]))
    t.filter = (0, n, 0, len(cols))
    return t


def summary_tab(title: str, heading: str, period_text: str, refresh_text: str, rows: list, delivered: dict, weekly: bool, color: str) -> Tab:
    t = Tab(title, 10 if weekly else 9, color, freeze=(3, 0), grid=False)
    t.add([heading]); t.add([period_text, None, None, refresh_text]); t.add([])
    t.style(0, 1, 0, 1, size=16, bold=True, color=INK)
    t.style(1, 2, 0, 1, size=11, color="4B4560")
    t.style(1, 2, 3, 4, size=9, italic=True, color="6B6480")
    waits = [r["wait"] for r in rows]
    tot_del = sum(delivered.values())
    lost_h = lambda ws: sum(max(0, w - 5) for w in ws) / 60
    t.add(["Orders delivered", "Waited 10+ min", "Share", "Rider-hours lost", "Avg wait (min)", "Longest (min)"])
    t.add([tot_del, len(waits), (len(waits) / tot_del) if tot_del else 0, round(lost_h(waits), 1),
           round(sum(waits) / len(waits), 1) if waits else 0, max(waits) if waits else 0])
    t.style(3, 5, 0, 6, bg="F4F1FB", font="Arial")
    t.style(3, 4, 0, 6, size=9, color="6B6480", valign="BOTTOM", wrap=True, align="CENTER")
    t.style(4, 5, 0, 6, size=15, bold=True, color=INK, align="CENTER")
    t.style(3, 5, 0, 1, align="LEFT")
    t.style(4, 5, 1, 2, color=RED); t.style(4, 5, 3, 4, color=RED)
    for c, f in ((0, INT_F), (1, INT_F), (2, PCT_F), (3, INT_F), (4, ONE_F), (5, INT_F)):
        t.style(4, 5, c, c + 1, num=f)
    t.heights[3] = 34; t.heights[4] = 36
    t.add([])

    # by city, A–Z
    t.add(["By city (A–Z)"]); t.style(len(t.rows) - 1, len(t.rows), 0, 1, size=12, bold=True, color=INK)
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)
    cities = sorted(set(delivered) | set(by_city), key=_ck)
    if weekly:
        hdr = ["City", "Worst restaurant", "Delivered", "Waited\n10+ min", "Share", "Avg wait\n(min)", "Longest\n(min)", "Rider-hours\nlost", "Its long\nwaits"]
    else:
        hdr = ["City", "Delivered", "Waited\n10+ min", "Share", "Avg wait\n(min)", "Longest\n(min)", "Rider-hours\nlost", "Worst restaurant", "Its long\nwaits"]
    h0 = t.add([]); t.header(h0, hdr)
    first = len(t.rows)
    for c in cities:
        rs = by_city[c]
        ws = [r["wait"] for r in rs]
        worst = Counter(r["rest"] for r in rs).most_common(1)
        dl = delivered.get(c, 0)
        nums = [dl, len(ws), (len(ws) / dl) if dl else 0, round(sum(ws) / len(ws), 1) if ws else None, max(ws) if ws else None, round(lost_h(ws), 1)]
        wname, wn = (worst[0][0], worst[0][1]) if worst else ("", None)
        t.add([c, wname] + nums + [wn] if weekly else [c] + nums + [wname, wn])
    last = len(t.rows)
    tot = [tot_del, len(waits), (len(waits) / tot_del) if tot_del else 0, round(sum(waits) / len(waits), 1) if waits else None,
           max(waits) if waits else None, round(lost_h(waits), 1)]
    t.add(["All cities", ""] + tot + [""] if weekly else ["All cities"] + tot + ["", ""])
    off = 2 if weekly else 1                                   # first number column
    t.style(first, last + 1, 0, 9, font="Arial", size=10, align="CENTER", valign="MIDDLE")
    t.style(first, last, 0, 1, bold=True, align="LEFT")
    t.style(first, last, off + 1, off + 2, bold=True)
    t.style(first, last + 1, 1 if weekly else 7, 2 if weekly else 8, align="LEFT")
    for k in range(first, last):
        t.style(k, k + 1, 0, 9, bg=BAND[(k - first) % 2])
    for c, f in ((off, INT_F), (off + 1, INT_F), (off + 2, PCT_F), (off + 3, ONE_F), (off + 4, INT_F), (off + 5, ONE_F)):
        t.style(first, last + 1, c, c + 1, num=f)
    t.style(last, last + 1, 0, 9, bg=INK, bold=True, color="FFFFFF")
    t.style(last, last + 1, 0, 1, align="LEFT")
    t.cond.append(((first, last, off + 1, off + 2), "GRADIENT", ["0", "max"], ("FFFFFF", "C9B8F7")))
    t.cond.append(((first, last, off + 2, off + 3), "GRADIENT3", ["0.1", "0.25", "0.4"], ("FFFFFF", "FFE7B3", "F4B6B6")))
    t.wait_colors(first, last, off + 3)
    t.add([]); t.add([])

    if not weekly:                     # yesterday: the 10 restaurants with the most long waits
        t.add(["Top 10 restaurants (most long waits)"]); t.style(len(t.rows) - 1, len(t.rows), 0, 1, size=12, bold=True, color=INK)
        h1 = t.add([]); t.header(h1, ["Restaurant", "City", "Long\nwaits", "Avg wait\n(min)", "Longest\n(min)", "Rider-h\nlost", "Worst hour"])
        f1 = len(t.rows)
        by_r = defaultdict(list)
        for r in rows:
            by_r[(r["rest"], r["city"])].append(r)
        for (rest, c), rs in sorted(by_r.items(), key=lambda x: (-len(x[1]), _ck(x[0][0])))[:10]:
            ws = [r["wait"] for r in rs]
            hrs = Counter(r["arr"].hour for r in rs if r["arr"])
            t.add([rest, c, len(ws), round(sum(ws) / len(ws), 1), max(ws), round(lost_h(ws), 1), _hours(hrs.most_common(1)[0][0] if hrs else None)])
        l1 = len(t.rows)
        t.style(f1, l1, 0, 7, font="Arial", size=10, align="CENTER", valign="MIDDLE")
        t.style(f1, l1, 0, 2, align="LEFT"); t.style(f1, l1, 0, 1, bold=True); t.style(f1, l1, 2, 3, bold=True)
        for k in range(f1, l1):
            t.style(k, k + 1, 0, 7, bg=BAND[(k - f1) % 2])
        t.style(f1, l1, 3, 4, num=ONE_F); t.style(f1, l1, 5, 6, num=ONE_F)
        t.wait_colors(f1, l1, 3)
        t.widths = dict(enumerate([200, 96, 96, 80, 88, 80, 104, 270, 88]))
    else:                              # last week: the call list per city
        t.add(["Restaurants to call — per city (3+ long waits in the week)"]); t.style(len(t.rows) - 1, len(t.rows), 0, 1, size=12, bold=True, color=INK)
        h1 = t.add([]); t.header(h1, ["City", "Restaurant", "Long\nwaits", "Days with\nlong waits", "Avg wait\n(min)", "Longest\n(min)", "Rider-hours\nlost",
                                      "Worst hour", "Worst day", "What to do"])
        f1 = len(t.rows)
        agg = defaultdict(list)
        for r in rows:
            agg[(r["city"], r["rest"])].append(r)
        keep = sorted(((k, rs) for k, rs in agg.items() if len(rs) >= 3), key=lambda x: (_ck(x[0][0]), -lost_h([r["wait"] for r in x[1]])))
        starts, prev = [], None
        ndays = max(1, len({r["date"] for r in rows}))
        for (c, rest), rs in keep:
            ws = [r["wait"] for r in rs]
            hrs = Counter(r["arr"].hour for r in rs if r["arr"])
            wh = hrs.most_common(1)[0][0] if hrs else None
            days = Counter(r["date"] for r in rs)
            per_day, avgw = len(ws) / 7, sum(ws) / len(ws)
            todo = (f"Call this week · send riders later around {wh:02d}:00" if per_day >= 3 and wh is not None else
                    f"Call · watch {wh:02d}:00" if per_day >= 1 and wh is not None else "Keep an eye on it")
            if avgw >= 15:
                todo += " · add to the Lieferando evidence pack"
            i = t.add([c, rest, len(ws), len(days), round(avgw, 1), max(ws), round(lost_h(ws), 1), _hours(wh), days.most_common(1)[0][0], todo])
            if c != prev:
                starts.append(i)
            prev = c
        l1 = len(t.rows)
        t.style(f1, l1, 0, 10, font="Arial", size=10, align="CENTER", valign="MIDDLE")
        t.style(f1, l1, 0, 2, align="LEFT"); t.style(f1, l1, 9, 10, align="LEFT")
        t.style(f1, l1, 0, 1, bold=True); t.style(f1, l1, 2, 3, bold=True)
        t.style(f1, l1, 4, 5, num=ONE_F); t.style(f1, l1, 6, 7, num=ONE_F); t.style(f1, l1, 8, 9, num=DATE_F)
        for k, a in enumerate(starts):
            b = starts[k + 1] if k + 1 < len(starts) else l1
            t.style(a, b, 0, 10, bg=BAND[k % 2])
            if k:
                t.borders.append((a, a + 1, 0, 10, "top"))
        t.wait_colors(f1, l1, 4)
        t.cond.append(((f1, l1, 6, 7), "GRADIENT", ["0", "max"], ("FFFFFF", "F2B8B5")))
        if l1 > f1:
            t.filter = (h1, l1, 0, 10)
        t.widths = dict(enumerate([180, 250, 80, 90, 80, 76, 96, 104, 90, 560]))
        _ = ndays
    t.style(0, len(t.rows), 0, t.ncols, font="Arial")
    return t


def help_tab() -> Tab:
    t = Tab("How it works", 2, "D1D5DB", grid=False)
    lines = [
        ("How this sheet works", None), ("", None),
        ("What counts", "An order where the rider waited 10 minutes or more for the food: the first rider's arrival at the restaurant → pickup. "
                        "A rider who gave up and handed the order back still counts. Orders the restaurant cancelled while the rider waited are included (see Note)."),
        ("Lost (min)", "Minutes beyond a normal 5-minute hand-over: Waited − 5. All of them together ÷ 60 = rider-hours lost."),
        ("Late?", "PTOD (dispatch → delivered) over 30 minutes."),
        ("Colours", "Waited 10–14 min = yellow · 15–19 min = orange · 20+ min = red."),
        ("Sorting", "Sorted by city A–Z. Inside a city: the restaurant with the most long waits first, then by time."),
        ("Yesterday", "'Yesterday' and 'Yesterday orders' are replaced every morning with the day before."),
        ("Last week", "'Last week' and 'Last week orders' are replaced every Monday with Monday–Sunday of the week before."),
        ("Slack", "After each refresh Quickzi Ops posts a short summary with the link in the team's own Slack channel."),
        ("Privacy", "No customer data: no names, addresses, phone numbers or zip codes. Order numbers, restaurants, riders and times only. "
                    "Keep the sheet shared inside the team."),
        ("Filters", "Use Data → Create a filter view to see one city or one restaurant without changing the sheet for everybody else."),
        ("Don't edit", "These tabs are rewritten by the system. Put your own notes in a tab of your own — those are never touched."),
    ]
    for a, b in lines:
        t.add([a, b])
    t.style(0, len(t.rows), 0, 2, font="Arial", size=10, valign="TOP")
    t.style(0, 1, 0, 1, size=16, bold=True, color=INK)
    t.style(2, len(t.rows), 0, 1, bold=True, color=INK)
    t.style(2, len(t.rows), 1, 2, wrap=True)
    t.widths = {0: 170, 1: 760}
    return t


def build(yesterday: dict, last_week: dict | None) -> list:
    """yesterday / last_week: {"rows": [...], "delivered": {city: n}, "label": "...", "a": date, "b": date}."""
    tabs = [summary_tab("Yesterday", "Long restaurant waits — riders who waited 10+ minutes for the food", yesterday["label"],
                        "Refreshed every morning · all cities · no customer data", yesterday["rows"], yesterday["delivered"], False, VIOLET)]
    if last_week is not None:
        tabs.append(summary_tab("Last week", "Last week — riders who waited 10+ minutes for the food", last_week["label"],
                                "Refreshed every Monday · all cities · no customer data", last_week["rows"], last_week["delivered"], True, "B9A4F5"))
    tabs.append(orders_tab("Yesterday orders", yesterday["rows"], "F59E0B"))
    if last_week is not None:
        tabs.append(orders_tab("Last week orders", last_week["rows"], "FCD34D"))
    tabs.append(help_tab())
    return tabs


# ------------------------------------------------------------------ writing
def _grid(sheet_id, r0, r1, c0, c1):
    return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}


def _cond_request(sheet_id, c, idx):
    (r0, r1, c0, c1), kind, vals, f = c
    rng = [_grid(sheet_id, r0, max(r1, r0 + 1), c0, c1)]
    if kind == "GRADIENT":
        rule = {"ranges": rng, "gradientRule": {"minpoint": {"color": _rgb(f[0]), "type": "NUMBER", "value": vals[0]},
                                                "maxpoint": {"color": _rgb(f[1]), "type": "MAX"}}}
    elif kind == "GRADIENT3":
        rule = {"ranges": rng, "gradientRule": {"minpoint": {"color": _rgb(f[0]), "type": "NUMBER", "value": vals[0]},
                                                "midpoint": {"color": _rgb(f[1]), "type": "NUMBER", "value": vals[1]},
                                                "maxpoint": {"color": _rgb(f[2]), "type": "NUMBER", "value": vals[2]}}}
    else:
        rule = {"ranges": rng, "booleanRule": {"condition": {"type": kind, "values": [{"userEnteredValue": v} for v in vals]}, "format": f}}
    return {"addConditionalFormatRule": {"rule": rule, "index": idx}}


def missing(sid: str, titles: list, client: Client | None = None) -> list:
    """Which of these tabs the sheet doesn't have (e.g. somebody deleted 'Last week')."""
    c = client or Client()
    have = {s["properties"]["title"] for s in c.call("GET", f"/v4/spreadsheets/{sid}", params={"fields": "sheets(properties(title))"}).get("sheets", [])}
    return [t for t in titles if t not in have]


def write(sid: str, tabs: list, client: Client | None = None) -> dict:
    """Replace the given tabs in the sheet (create them when missing). Returns {title: gid}."""
    c = client or Client()
    meta = c.call("GET", f"/v4/spreadsheets/{sid}", params={"fields": "sheets(properties(sheetId,title,index),conditionalFormats,basicFilter)"})
    sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
    add = [{"addSheet": {"properties": {"title": t.title}}} for t in tabs if t.title not in sheets]
    if add:
        res = c.batch(sid, add)
        for rep in (res or {}).get("replies", []):
            p = rep.get("addSheet", {}).get("properties", {})
            if p:
                sheets[p["title"]] = {"properties": p}
    # the empty first tab Google creates ("Sheet1" / "Tabellenblatt1") goes away once ours are in
    for name in ("Sheet1", "Tabellenblatt1", "Blatt1", "Tabelle1"):
        s = sheets.get(name)
        if s and len(sheets) > 1:
            try:
                v = c.call("GET", f"/v4/spreadsheets/{sid}/values/{name}!A1:Z50")
                if not v.get("values"):
                    c.batch(sid, [{"deleteSheet": {"sheetId": s["properties"]["sheetId"]}}]); sheets.pop(name)
            except GoogleError:
                pass
    ours = [x for x in TABS if x in sheets]            # our tabs first, in our order (positions that always exist)
    gids = {}
    for t in tabs:
        s = sheets[t.title]; gid = s["properties"]["sheetId"]; gids[t.title] = gid
        n = max(len(t.rows), 1)
        req = [{"deleteConditionalFormatRule": {"sheetId": gid, "index": i}} for i in reversed(range(len(s.get("conditionalFormats") or [])))]
        if s.get("basicFilter"):
            req.append({"clearBasicFilter": {"sheetId": gid}})
        req.append({"updateSheetProperties": {"properties": {"sheetId": gid, "gridProperties": {"rowCount": n + 20, "columnCount": t.ncols + 1}},
                                              "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
        req.append({"updateCells": {"range": {"sheetId": gid}, "fields": "*"}})                     # clears values and formats
        req.append({"updateSheetProperties": {"properties": {"sheetId": gid, "index": ours.index(t.title) if t.title in ours else 0,
                                                             "tabColorStyle": {"rgbColor": _rgb(t.color)},
                                                             "gridProperties": {"frozenRowCount": t.freeze[0], "frozenColumnCount": t.freeze[1],
                                                                                "hideGridlines": not t.grid}},
                                              "fields": "index,tabColorStyle,gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines"}})
        for col, px in t.widths.items():
            req.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": col, "endIndex": col + 1},
                                                      "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        req.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 0, "endIndex": n},
                                                  "properties": {"pixelSize": 22}, "fields": "pixelSize"}})
        for row, px in t.heights.items():
            req.append({"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": row, "endIndex": row + 1},
                                                      "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        c.batch(sid, req)
        for a in range(0, len(t.rows), CHUNK):                                                         # values
            part = t.rows[a:a + CHUNK]
            c.batch(sid, [{"updateCells": {"start": {"sheetId": gid, "rowIndex": a, "columnIndex": 0},
                                           "rows": [{"values": [_cell(v) for v in row]} for row in part],
                                           "fields": "userEnteredValue,userEnteredFormat.textFormat.link"}}])
        req = []
        for (r0, r1, c0, c1, f, fields) in t.styles:                                                  # formats (in order: later ones win)
            if r1 > r0 and fields:
                req.append({"repeatCell": {"range": _grid(gid, r0, r1, c0, c1), "cell": {"userEnteredFormat": f}, "fields": fields}})
        for (r0, r1, c0, c1, side) in t.borders:
            req.append({"updateBorders": {"range": _grid(gid, r0, r1, c0, c1), side: {"style": "SOLID_MEDIUM", "color": _rgb(VIOLET)}}})
        for i, cf in enumerate(t.cond):
            if cf[0][1] > cf[0][0]:
                req.append(_cond_request(gid, cf, i))
        if t.filter and t.filter[1] > t.filter[0] + 1:
            req.append({"setBasicFilter": {"filter": {"range": _grid(gid, *t.filter)}}})
        for a in range(0, len(req), 400):
            c.batch(sid, req[a:a + 400])
    return gids
