"""Alibaba — https://www.alibaba.ir  (international flights)

Two-step async API, no auth:

1. POST https://ws.alibaba.ir/api/v1/flights/international/proposal-requests
   {"origin":"THR","destination":"IST","departureDate":"2026-08-05",
    "adult":1,"child":0,"infant":0,"cabinType":"ECONOMY"}
   -> {"result":{"requestId":"<base64>", "nextRequestThreshold":500}}

2. GET  https://ws.alibaba.ir/api/v1/flights/international/proposal-requests/<urlencoded requestId>
   -> {"result":{"proposals":[...], "isCompleted":bool, "nextRequestThreshold":ms}}
   Poll until ``isCompleted`` (proposals arrive incrementally).

Prices are Rial in ``proposal.total`` / ``proposal.prices[].total``.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List
from urllib.parse import quote

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

BASE = "https://ws.alibaba.ir/api/v1/flights/international/proposal-requests"
MAX_POLLS = 25


@register
class AlibabaScraper(BaseScraper):
    name = "alibaba"
    label = "علی‌بابا"

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        headers = {
            "Origin": "https://www.alibaba.ir",
            "Referer": "https://www.alibaba.ir/",
            "Content-Type": "application/json",
        }
        started = self.fetcher.post(
            BASE,
            json_body={
                "origin": route.origin_city,
                "destination": route.destination_city,
                "departureDate": day,
                "adult": adults,
                "child": 0,
                "infant": 0,
                "cabinType": "ECONOMY",  # cabin acts as a floor, all classes come back
            },
            headers=headers,
        )
        if not started.get("success"):
            raise RuntimeError("alibaba search rejected: {}".format(started.get("error")))
        request_id = started["result"]["requestId"]
        delay_ms = started["result"].get("nextRequestThreshold") or 800

        poll_url = "{}/{}".format(BASE, quote(request_id, safe=""))
        proposals: Dict[str, Dict[str, Any]] = {}
        for _ in range(MAX_POLLS):
            time.sleep(min(delay_ms, 3000) / 1000.0)
            page = self.fetcher.get(poll_url, headers=headers)
            result = page.get("result") or {}
            for p in result.get("proposals") or []:
                proposals[p.get("uniqueId") or p.get("proposalId")] = p
            delay_ms = result.get("nextRequestThreshold") or 1500
            if result.get("isCompleted"):
                break

        return [self._to_offer(p, route, day) for p in proposals.values() if p.get("total")]

    def _to_offer(self, p: Dict[str, Any], route: RouteSpec, day: str) -> FlightOffer:
        group = p.get("leavingFlightGroup") or {}
        details = group.get("flightDetails") or []
        first = details[0] if details else {}
        last = details[-1] if details else {}
        cabin_raw = group.get("cabinTypeName") or first.get("cabinType")
        baggage = first.get("baggage")
        return FlightOffer(
            source=self.name,
            route=route.id,
            search_date=day,
            price_rial=int(p["total"]),
            airline_code=first.get("marketingCarrier") or p.get("airlineCode"),
            airline_name=group.get("airlineNamePersian") or group.get("airlineName"),
            flight_number=first.get("flightNumber"),
            cabin=normalize_cabin(cabin_raw),
            cabin_raw=cabin_raw,
            origin=group.get("origin"),
            destination=group.get("destination"),
            departure_time=group.get("departureDateTime") or first.get("departureDateTime"),
            arrival_time=last.get("arrivalDateTime"),
            duration_min=group.get("durationMin"),
            stops=group.get("numberOfStop"),
            baggage=", ".join(baggage) if isinstance(baggage, list) else baggage,
            fare_type=p.get("invoiceType"),
            is_charter=(str(p.get("invoiceType") or "").lower() == "charter"),
            raw={"proposalId": p.get("proposalId")},
        )
