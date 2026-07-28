"""FlyToday — https://www.flytoday.ir

Internal JSON API, no auth, no cookies needed:

    POST https://www.flytoday.ir/api/gateway/V1/flight/search
    {
      "pricingSourceType": 0,
      "adultCount": 1, "childCount": 0, "infantCount": 0,
      "travelPreference": {"cabinType": 1, "maxStopsQuantity": "All",
                           "airTripType": "OneWay"},
      "originDestinationInformations": [
        {"departureDateTime": "2026-08-05",
         "originLocationCode": "THR", "originType": "City",
         "destinationLocationCode": "IST", "destinationType": "City"}],
      "isJalali": false
    }

Prices come back in Rial under
``airItineraryPricingInfo.itinTotalFare.totalFare``.

The web UI reaches this endpoint from
``/flight/search?departure=THR,1&arrival=IST,1&departureDate=...`` — note the
comma before the station-type code; ``THR_c`` style values are rejected with
«فرودگاه انتخابی اشتباه است».
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

SEARCH_URL = "https://www.flytoday.ir/api/gateway/V1/flight/search"

# cabinType in the API: 1=economy, 2=premium economy, 3=business, 4=first
CABIN_TYPE = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}


@register
class FlyTodayScraper(BaseScraper):
    name = "flytoday"
    label = "فلای‌تودی"

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        # cabinType acts as a *floor*, not a filter: one economy search returns
        # economy + premium economy + business in the same payload, while
        # cabinType=3 returns nothing at all. So one request per (route, date)
        # is both complete and three times gentler on their rate limiter.
        payload = {
            "pricingSourceType": 0,
            "adultCount": adults,
            "childCount": 0,
            "infantCount": 0,
            "travelPreference": {
                "cabinType": CABIN_TYPE["economy"],
                "maxStopsQuantity": "All",
                "airTripType": "OneWay",
            },
            "originDestinationInformations": [
                {
                    "departureDateTime": day,
                    "originLocationCode": route.origin_city,
                    "originType": "City",
                    "destinationLocationCode": route.destination_city,
                    "destinationType": "City",
                }
            ],
            "isJalali": False,
        }
        data = self.fetcher.post(
            SEARCH_URL,
            json_body=payload,
            headers={
                "Origin": "https://www.flytoday.ir",
                "Referer": "https://www.flytoday.ir/flight/search",
                "Content-Type": "application/json",
            },
        )
        return _dedupe(self._parse(data, route, day, "economy"))

    def _parse(self, data: Dict[str, Any], route: RouteSpec, day: str, asked_cabin: str) -> List[FlightOffer]:
        out: List[FlightOffer] = []
        for item in data.get("pricedItineraries") or []:
            fare = ((item.get("airItineraryPricingInfo") or {}).get("itinTotalFare") or {})
            total = fare.get("totalFare")
            if not total:
                continue
            legs = item.get("originDestinationOptions") or []
            segments = legs[0].get("flightSegments") if legs else []
            if not segments:
                continue
            first, last = segments[0], segments[-1]
            cabin_raw = first.get("cabinClassName") or first.get("cabinClassCode")
            cabin = normalize_cabin(cabin_raw)
            if cabin == "unknown":
                cabin = asked_cabin
            out.append(
                FlightOffer(
                    source=self.name,
                    route=route.id,
                    search_date=day,
                    price_rial=int(total),
                    airline_code=item.get("validatingAirlineCode") or first.get("marketingAirlineCode"),
                    airline_name=(first.get("operatingAirline") or {}).get("code"),
                    flight_number=first.get("flightNumber"),
                    cabin=cabin,
                    cabin_raw=cabin_raw,
                    origin=(first.get("departureAirportLocationCode") or "").upper() or None,
                    destination=(last.get("arrivalAirportLocationCode") or "").upper() or None,
                    departure_time=first.get("departureDateTime"),
                    arrival_time=last.get("arrivalDateTime"),
                    duration_min=legs[0].get("journeyDurationPerMinute"),
                    stops=max(len(segments) - 1, 0),
                    seats_available=first.get("seatsRemaining"),
                    baggage=first.get("baggage"),
                    fare_type=(item.get("airItineraryPricingInfo") or {}).get("fareType"),
                    is_charter=bool(item.get("isCharter")),
                    raw={"key": item.get("key")},
                )
            )
        return out


def _dedupe(offers: List[FlightOffer]) -> List[FlightOffer]:
    seen: Dict[str, FlightOffer] = {}
    for o in offers:
        k = o.offer_key()
        if k not in seen or o.price_rial < seen[k].price_rial:
            seen[k] = o
    return list(seen.values())
