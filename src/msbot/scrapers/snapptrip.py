"""SnappTrip — https://www.snapptrip.com  (international / «پرواز خارجی»)

Single synchronous JSON call, no auth:

    POST https://ift.snapptrip.com/api/listing/v1/one-way/search?source=searchBox
    {"dateType":"gregorian","origin":"THR","destination":"IST",
     "originIsCity":true,"destinationIsCity":true,
     "adultCount":1,"childCount":0,"infantCount":0,
     "departureDate":"2026-08-05","cabinType":"ECONOMY"}

The site's own results page is
``/inter-flights/THR_city/IST_city?adultCount=1&departureDate=...&cabinType=ECONOMY``.
Sending ``THR_city`` to the API instead of ``THR`` returns HTTP 409.

Response shape::

    {"airfares": [{"airlineCode": "W5",
                   "pricing": {"totalPayablePrice": 266800000,    # Rial
                               "totalBasePrice": 186000000,
                               "totalTax": 80800000, "currency": "IRR"},
                   "routes": [{"segments": [{...}], "baggageDetail": {...}}]}]}

``totalBasePrice`` is the airline base fare before tax — *not* the agency net
fare, so it is kept in ``raw`` for reference but never used as a markup base.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

SEARCH_URL = "https://ift.snapptrip.com/api/listing/v1/one-way/search?source=searchBox"


@register
class SnappTripScraper(BaseScraper):
    name = "snapptrip"
    label = "اسنپ‌تریپ"

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        payload = {
            "dateType": "gregorian",
            "origin": route.origin_city,
            "destination": route.destination_city,
            "originIsCity": True,
            "destinationIsCity": True,
            "adultCount": adults,
            "childCount": 0,
            "infantCount": 0,
            "departureDate": day,
            "cabinType": "ECONOMY",  # acts as a floor; business fares still come back
        }
        data = self.fetcher.post(
            SEARCH_URL,
            json_body=payload,
            headers={
                "Origin": "https://www.snapptrip.com",
                "Referer": "https://www.snapptrip.com/",
                "Content-Type": "application/json",
            },
        )
        return [o for o in (self._to_offer(a, route, day) for a in data.get("airfares") or []) if o]

    def _to_offer(self, a: Dict[str, Any], route: RouteSpec, day: str) -> Optional[FlightOffer]:
        pricing = a.get("pricing") or {}
        price = pricing.get("totalPayablePrice") or pricing.get("totalOldPayablePrice")
        if not price:
            return None

        routes = a.get("routes") or []
        leg = routes[0] if routes else {}
        segs = leg.get("segments") or []
        first = segs[0] if segs else {}
        last = segs[-1] if segs else {}

        cabin_raw = first.get("cabinType")
        baggage = (leg.get("baggageDetail") or {}).get("description") or first.get("baggage")
        carrier = first.get("carrierAirlineInfo") or {}
        flight_no = first.get("flightNumber")
        airline_code = first.get("carrierAirlineCode") or a.get("airlineCode")
        if flight_no and airline_code and not str(flight_no).startswith(str(airline_code)):
            flight_no = "{}{}".format(airline_code, flight_no)

        return FlightOffer(
            source=self.name,
            route=route.id,
            search_date=day,
            price_rial=int(price),
            airline_code=airline_code,
            airline_name=carrier.get("faName") or carrier.get("name"),
            flight_number=flight_no,
            cabin=normalize_cabin(cabin_raw),
            cabin_raw=cabin_raw,
            origin=leg.get("originAirportCode") or first.get("departureAirportCode"),
            destination=leg.get("destinationAirportCode") or last.get("arrivalAirportCode"),
            departure_time=first.get("departureDateTime"),
            arrival_time=last.get("arrivalDateTime"),
            duration_min=leg.get("totalJourneyDuration"),
            stops=max(len(segs) - 1, 0) if segs else None,
            seats_available=first.get("seatsRemaining"),
            baggage=baggage,
            fare_type="charter" if first.get("isCharter") else a.get("refundType"),
            is_charter=bool(first.get("isCharter")),
            raw={
                "totalBasePrice": pricing.get("totalBasePrice"),
                "totalTax": pricing.get("totalTax"),
                "fareClass": first.get("fareClass"),
            },
        )
