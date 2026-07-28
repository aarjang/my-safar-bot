"""MySafar — https://www.mysafar.com (our own site; the reference price)

    POST https://api.mysafar.com/v1/flight/find
    {"origin":"IKA","destination":"ISTALL",
     "fromDate":"2026-08-05","toDate":"2026-08-05","seats":1}
    -> 201 {"items":[...], "totalItems":n, "searchID":"68e094hw"}

    GET  https://api.mysafar.com/v1/flight/search-result/<searchID>?ts=<epoch_ms>
    -> further items as suppliers answer (the site polls this every ~3s)

``adultFare`` is the *public selling* price in Rial — i.e. net fare **plus** the
markup already applied. The purchase price / commission / applied-markup
breakdown seen in the admin tooltip comes from the authenticated endpoint
``/v1/flight/client/find`` and is not exposed publicly; see ``markup.py`` for how
base fares are supplied.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

FIND_URL = "https://api.mysafar.com/v1/flight/find"
RESULT_URL = "https://api.mysafar.com/v1/flight/search-result/{}"
MAX_POLLS = 6


@register
class MySafarScraper(BaseScraper):
    name = "mysafar"
    label = "مای سفر"

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        headers = {
            "Origin": "https://www.mysafar.com",
            "Referer": "https://www.mysafar.com/",
            "Content-Type": "application/json",
        }
        payload = {
            "origin": route.mysafar_origin or route.origin_airport or route.origin_city,
            "destination": route.mysafar_destination or route.destination_airport or route.destination_city,
            "fromDate": day,
            "toDate": day,
            "seats": adults,
        }
        data = self.fetcher.post(FIND_URL, json_body=payload, headers=headers)

        items: Dict[str, Dict[str, Any]] = {}
        _collect(items, data.get("items") or [])

        search_id = data.get("searchID")
        if search_id and self.options.get("poll", True):
            for _ in range(MAX_POLLS):
                time.sleep(3.0)
                more = self.fetcher.get(
                    RESULT_URL.format(search_id),
                    params={"ts": int(time.time() * 1000)},
                    headers=headers,
                )
                new = more.get("items") or []
                before = len(items)
                _collect(items, new)
                if len(items) == before:
                    break

        return [self._to_offer(it, route, day) for it in items.values() if it.get("adultFare")]

    def _to_offer(self, it: Dict[str, Any], route: RouteSpec, day: str) -> FlightOffer:
        airline = it.get("airlineDetail") or {}
        info = it.get("flightInfo") or {}
        return FlightOffer(
            source=self.name,
            route=route.id,
            search_date=day,
            price_rial=int(it["adultFare"]),
            airline_code=it.get("airline"),
            airline_name=airline.get("nameFa") or airline.get("name"),
            flight_number=info.get("flightNumber") or (it.get("flightKey") or "").split("-")[0],
            cabin=normalize_cabin(it.get("cabinType")),
            cabin_raw=it.get("cabinType"),
            origin=it.get("origin"),
            destination=it.get("destination"),
            departure_time=it.get("originLocalTime") or it.get("departureDate"),
            arrival_time=it.get("destinationLocalTime") or it.get("arrivalDate"),
            duration_min=it.get("duration"),
            stops=0,
            seats_available=it.get("availableSeats"),
            baggage=info.get("baggage") or it.get("fareName"),
            fare_type=it.get("flightType"),
            is_charter=(str(it.get("flightType") or "").lower() == "charter"),
            raw={
                "supplierId": it.get("supplierId"),
                "fareName": it.get("fareName"),
                "childFare": it.get("childFare"),
                "infantFare": it.get("infantFare"),
            },
        )


def _collect(bucket: Dict[str, Dict[str, Any]], items: List[Dict[str, Any]]) -> None:
    for it in items:
        key = "{}|{}|{}|{}".format(
            it.get("flightKey"), it.get("cabinType"), it.get("supplierId"), it.get("adultFare")
        )
        bucket[key] = it
