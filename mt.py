"""MotionTools API client (read-only) for the Quickzi ops dashboard.

Endpoints used (all documented at docs.motiontools.io):
  GET /api/hailing/bookings          all bookings (admin / org_manager) — filters[status][]=…, filters[service_area_id]=…
  GET /api/hailing/bookings/{id}     one booking with stops, driver, events timeline
  GET /api/users                     users — filters[role]=driver gives riders with online status, GPS, active orders
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

log = logging.getLogger("mt")
BASE = "https://api.motiontools.io"

ACTIVE_STATUSES = ["to_be_dispatched", "dispatched", "partially_dispatched", "pickable", "claimed", "en_route"]
DONE_STATUSES = ["done", "paid", "processing_payment", "cancelled"]


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


class MotionTools:
    def __init__(self, token: str):
        self.token = token
        self.stats = {"calls": 0, "errors": 0, "last_status": None, "last_error": None,
                      "bookings_filter_mode": None, "drivers_filter_mode": None}
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "Accept-Language": "en"}

    async def _get(self, path: str, params: list):
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
            return res.status_code, None
        try:
            return 200, res.json()
        except ValueError:
            return 200, None

    async def _paged(self, path: str, base_params: list, max_pages: int = 10) -> Optional[list]:
        """Follow simple pagination; returns None on error, list otherwise."""
        results, page = [], 1
        while page <= max_pages:
            status, data = await self._get(path, base_params + [("page", str(page)), ("limit", "100"),
                                                                ("pagination", "simple")])
            if status != 200 or not isinstance(data, dict):
                return None if not results else results
            results.extend(data.get("results") or [])
            pag = ((data.get("meta") or {}).get("pagination") or {})
            if not pag.get("next"):
                break
            page += 1
        return results

    # ---------- bookings ----------
    async def list_bookings(self, area_ids: list, statuses: list, extra: Optional[dict] = None) -> Optional[list]:
        """Try the most specific filter first; if the API rejects it, fall back and filter here."""
        attempts = [
            ("area+status", {"service_area_id": area_ids, "status": statuses, **(extra or {})}),
            ("area", {"service_area_id": area_ids, **(extra or {})}),
            ("none", dict(extra or {})),
        ]
        if not area_ids:
            attempts = [("status", {"status": statuses, **(extra or {})}), ("none", dict(extra or {}))]
        for mode, filters in attempts:
            params = enc_filters(filters) + [("view", "standard"), ("order_by", "scheduled_at"), ("direction", "desc")]
            rows = await self._paged("/api/hailing/bookings", params)
            if rows is None:
                continue
            self.stats["bookings_filter_mode"] = mode
            out = []
            for b in rows:
                if statuses and b.get("status") not in statuses:
                    continue
                area = (b.get("service_area") or {}).get("id")
                if area_ids and area and area not in area_ids:
                    continue
                out.append(b)
            return out
        return None

    async def get_booking(self, booking_id: str) -> Optional[dict]:
        status, data = await self._get(f"/api/hailing/bookings/{booking_id}", [("view", "standard")])
        if status != 200 or not isinstance(data, dict):
            return None
        return data.get("booking") or data.get("hailing_booking") or data

    # ---------- riders ----------
    async def list_drivers(self, area_ids: list) -> Optional[list]:
        attempts = [("role+area", {"role": "driver", "service_area_id": area_ids}), ("role", {"role": "driver"})]
        if not area_ids:
            attempts = attempts[1:]
        for mode, filters in attempts:
            rows = await self._paged("/api/users", enc_filters(filters))
            if rows is None:
                continue
            self.stats["drivers_filter_mode"] = mode
            out = []
            for u in rows:
                if u.get("role") not in (None, "driver"):
                    continue
                area = (u.get("service_area") or {}).get("id")
                if area_ids and area and area not in area_ids:
                    continue
                out.append(u)
            return out
        return None
