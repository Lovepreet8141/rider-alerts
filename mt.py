"""MotionTools API client (read-only) for the Quickzi ops dashboard.

MotionTools can put an account in "restricted API mode": some endpoints answer
403 restricted_endpoint, others may still work.  This client therefore
  * knows several paths for the same data (documented /api/bookings… first, legacy /api/hailing/… last),
  * remembers which endpoints are restricted and stops calling them (until the next probe()),
  * exposes stats["endpoints"] so the dashboard can show what is open and what is not.

Endpoints (docs.motiontools.io):
  GET /api/bookings/active            active bookings
  GET /api/bookings                   all bookings — filters[status][]=…, filters[service_area_id]=…, filters[local_done_at]=YYYY-MM-DD
  GET /api/bookings/{id}              one booking with stops, driver, events timeline
  GET /api/hailing/bookings[/{id}]    legacy paths for the same
  GET /api/users?filters[role]=driver riders with online status, GPS, active orders
  GET /api/users/{id}                 one user (name, phone)
  GET /api/places/{id}                one place (restaurant name, address)
  GET /api/user                       the token owner (sanity check)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("mt")
BASE = "https://api.motiontools.io"

ACTIVE_STATUSES = ["to_be_dispatched", "dispatched", "partially_dispatched", "pickable", "claimed", "en_route"]
DONE_STATUSES = ["done", "paid", "processing_payment", "cancelled"]

LIST_PATHS = ["/api/bookings/active", "/api/bookings", "/api/hailing/bookings"]
HISTORY_PATHS = ["/api/bookings", "/api/hailing/bookings"]
DETAIL_PATHS = ["/api/bookings/{id}", "/api/hailing/bookings/{id}"]
USERS_PATH = "/api/users"
USER_PATH = "/api/users/{id}"
PLACE_PATH = "/api/places/{id}"
ME_PATH = "/api/user"

LABELS = {"/api/user": "token owner", "/api/bookings/active": "active bookings", "/api/bookings": "bookings",
          "/api/hailing/bookings": "bookings (legacy)", "/api/bookings/{id}": "booking detail",
          "/api/hailing/bookings/{id}": "booking detail (legacy)", "/api/users": "riders list",
          "/api/users/{id}": "rider detail", "/api/places/{id}": "restaurant detail"}


def enc_filters(filters: dict) -> list:
    """Rails-style query encoding: filters[key]=v and filters[key][]=v1&filters[key][]=v2."""
    out = []
    for k, v in filters.items():
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, (list, tuple, set)):
            for x in v:
                out.append((f"filters[{k}][]", str(x)))
        else:
            out.append((f"filters[{k}]", str(v)))
    return out


def _unwrap(data, *keys):
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), dict):
                return data[k]
    return data if isinstance(data, dict) else None


class MotionTools:
    def __init__(self, token: str):
        self.token = token
        self.stats = {"calls": 0, "errors": 0, "last_status": None, "last_error": None,
                      "bookings_filter_mode": None, "drivers_filter_mode": None,
                      "bookings_path": None, "detail_path": None, "endpoints": {}, "probed_at": None}
        self.blocked_paths: dict = {}          # endpoint key -> reason (restricted / http 404 / rate limit)
        self.blocked_until: dict = {}          # endpoint key -> datetime when a rate-limit block expires
        self.permanent: set = set()            # endpoint keys that answered 404 with a real id -> path does not exist here
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def blocked(self, key: str) -> bool:
        until = self.blocked_until.get(key)
        if until and datetime.now(timezone.utc) >= until:          # hourly quota is back
            self.blocked_until.pop(key, None)
            self.blocked_paths.pop(key, None)
            self.stats["endpoints"][key] = "quota back"
            return False
        return key in self.blocked_paths

    def quota_left(self, key: str) -> bool:
        """True when the endpoint is usable right now (not restricted, not out of hourly quota, not missing)."""
        return not self.blocked(key)

    def endpoint_ok(self, key: str) -> bool:
        return self.stats["endpoints"].get(key) == "ok"

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Accept-Language": "en"}

    async def _get(self, path: str, params: list, key: str = None):
        key = key or path
        if self.blocked(key):
            return 403, None
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=BASE, timeout=20)
        self.stats["calls"] += 1
        try:
            res = await self._client.get(path, params=params, headers=self._headers())
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{path}: {e}"
            log.warning("MotionTools request failed %s: %s", path, e)
            return None, None
        self.stats["last_status"] = res.status_code
        if res.status_code != 200:
            self.stats["errors"] += 1
            self.stats["last_error"] = f"{path} -> {res.status_code} {res.text[:200]}"
            log.warning("MotionTools %s -> %s %s", path, res.status_code, res.text[:200])
            now = datetime.now(timezone.utc)
            if "restricted_endpoint" in res.text:
                self.blocked_paths[key] = "restricted"
                self.stats["endpoints"][key] = "restricted"
            elif res.status_code == 429 or "rate_limit" in res.text:
                # "restricted API access mode ... reached the hourly limit for this endpoint" -> usable again next hour
                self.blocked_paths[key] = "hourly quota used"
                self.blocked_until[key] = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, minutes=1))
                self.stats["endpoints"][key] = "hourly quota used (back next hour)"
            elif res.status_code in (401, 403):
                self.blocked_paths[key] = f"http {res.status_code}"
                self.stats["endpoints"][key] = f"http {res.status_code}"
            elif res.status_code == 404:
                # a 404 with a real id (or on a list path) means this path does not exist on this tenant -> never retry
                self.blocked_paths[key] = "not available (404)"
                self.permanent.add(key)
                self.stats["endpoints"][key] = "not available (404)"
            else:
                self.stats["endpoints"].setdefault(key, f"http {res.status_code}")
            return res.status_code, None
        self.stats["endpoints"][key] = "ok"
        try:
            return 200, res.json()
        except ValueError:
            return 200, None

    async def _paged(self, path: str, base_params: list, key: str = None, max_pages: int = 10) -> Optional[list]:
        """Follow simple pagination; returns None on error, list otherwise."""
        results, page = [], 1
        while page <= max_pages:
            status, data = await self._get(path, base_params + [("page", str(page)), ("limit", "100"),
                                                                ("pagination", "simple")], key)
            if status != 200:
                return None if not results else results
            if isinstance(data, list):
                results.extend(data)
                break
            if not isinstance(data, dict):
                return None if not results else results
            rows = data.get("results") or data.get("bookings") or data.get("users") or data.get("data") or []
            results.extend(rows if isinstance(rows, list) else [])
            pag = ((data.get("meta") or {}).get("pagination") or {})
            if not pag.get("next"):
                break
            page += 1
        return results

    # ---------- bookings ----------
    async def list_bookings(self, area_ids: list, statuses: list, extra: Optional[dict] = None,
                            history: bool = False) -> Optional[list]:
        """Try every known path, most specific filter first; if the API rejects a filter, fall back and filter here."""
        attempts = [
            ("area+status", {"service_area_id": area_ids, "status": statuses, **(extra or {})}),
            ("area", {"service_area_id": area_ids, **(extra or {})}),
            ("none", dict(extra or {})),
        ]
        if not area_ids:
            attempts = [("status", {"status": statuses, **(extra or {})}), ("none", dict(extra or {}))]
        paths = list(HISTORY_PATHS if history else LIST_PATHS)
        known = self.stats["bookings_path"]
        if known in paths:
            paths = [known] + [p for p in paths if p != known]
        for path in paths:
            if self.blocked(path):
                continue
            for mode, filters in attempts:
                params = enc_filters(filters) + [("view", "standard"), ("order_by", "scheduled_at"), ("direction", "desc")]
                rows = await self._paged(path, params)
                if rows is None:
                    if self.blocked(path):
                        break                          # path is dead — try the next path
                    continue                           # filter rejected — try a simpler filter
                self.stats["bookings_filter_mode"] = mode
                if not history:
                    self.stats["bookings_path"] = path
                out = []
                for b in rows:
                    if not isinstance(b, dict):
                        continue
                    if statuses and b.get("status") not in statuses:
                        continue
                    area = (b.get("service_area") or {}).get("id") or b.get("service_area_id")
                    if area_ids and area and area not in area_ids:
                        continue
                    out.append(b)
                return out
        return None

    async def get_booking(self, booking_id: str) -> Optional[dict]:
        paths = list(DETAIL_PATHS)
        if self.stats["detail_path"] in paths:
            paths = [self.stats["detail_path"]] + [p for p in paths if p != self.stats["detail_path"]]
        for tpl in paths:
            if self.blocked(tpl):
                continue
            status, data = await self._get(tpl.format(id=booking_id), [("view", "standard")], key=tpl)
            if status == 200 and isinstance(data, dict):
                self.stats["detail_path"] = tpl
                return _unwrap(data, "booking", "hailing_booking")
        return None

    def detail_available(self) -> bool:
        return any(not self.blocked(t) for t in DETAIL_PATHS)

    # ---------- riders ----------
    async def list_drivers(self, area_ids: list) -> Optional[list]:
        if self.blocked(USERS_PATH):
            return None
        attempts = [("role+area", {"role": "driver", "service_area_id": area_ids}), ("role", {"role": "driver"})]
        if not area_ids:
            attempts = attempts[1:]
        for mode, filters in attempts:
            rows = await self._paged(USERS_PATH, enc_filters(filters))
            if rows is None:
                if self.blocked(USERS_PATH):
                    return None
                continue
            self.stats["drivers_filter_mode"] = mode
            out = []
            for u in rows:
                if not isinstance(u, dict) or u.get("role") not in (None, "driver"):
                    continue
                area = (u.get("service_area") or {}).get("id") or u.get("service_area_id")
                if area_ids and area and area not in area_ids:
                    continue
                out.append(u)
            return out
        return None

    async def get_user(self, user_id: str) -> Optional[dict]:
        if self.blocked(USER_PATH):
            return None
        status, data = await self._get(USER_PATH.format(id=user_id), [], key=USER_PATH)
        return _unwrap(data, "user") if status == 200 else None

    async def get_place(self, place_id: str) -> Optional[dict]:
        if self.blocked(PLACE_PATH):
            return None
        status, data = await self._get(PLACE_PATH.format(id=place_id), [], key=PLACE_PATH)
        return _unwrap(data, "place") if status == 200 else None

    async def me(self) -> Optional[dict]:
        status, data = await self._get(ME_PATH, [], key=ME_PATH)
        return _unwrap(data, "user") if status == 200 else None

    # ---------- discovery ----------
    async def probe(self, booking_id: str = None, place_id: str = None, user_id: str = None) -> dict:
        """Re-test the endpoints that answered 'restricted' earlier (cheap: a few calls), so the dashboard can show
        what this token may read. Paths that do not exist (404) are never asked again; hourly quotas are respected."""
        for key in [k for k, why in self.blocked_paths.items() if why == "restricted"]:
            self.blocked_paths.pop(key, None)
        tests = [(ME_PATH, ME_PATH, []),
                 ("/api/bookings/active", "/api/bookings/active", [("limit", "1")]),
                 ("/api/bookings", "/api/bookings", [("limit", "1")]),
                 ("/api/hailing/bookings", "/api/hailing/bookings", [("limit", "1")]),
                 (USERS_PATH, USERS_PATH, enc_filters({"role": "driver"}) + [("limit", "1")])]
        if booking_id:
            tests += [(t, t.format(id=booking_id), [("view", "standard")]) for t in DETAIL_PATHS]
        if place_id:
            tests.append((PLACE_PATH, PLACE_PATH.format(id=place_id), []))
        if user_id:
            tests.append((USER_PATH, USER_PATH.format(id=user_id), []))
        for key, path, params in tests:
            if key in self.permanent or self.blocked(key) or self.stats["endpoints"].get(key) == "ok":
                continue                                   # known: missing, out of quota, or already working
            status, data = await self._get(path, params, key=key)
            if status == 200 and key in DETAIL_PATHS and self.stats["detail_path"] is None:
                self.stats["detail_path"] = key
        self.stats["probed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return dict(self.stats["endpoints"])

    def endpoint_summary(self) -> str:
        parts = []
        for key, label in LABELS.items():
            st = self.stats["endpoints"].get(key)
            if st:
                parts.append(f"{label} {'✓' if st == 'ok' else '✗ ' + st}")
        return " · ".join(parts) or "not checked yet"
