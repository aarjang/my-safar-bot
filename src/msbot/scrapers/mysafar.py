"""MySafar — https://www.mysafar.com (our own site; the reference price)

    POST https://api.mysafar.com/v1/flight/find
    {"origin":"IKA","destination":"ISTALL",
     "fromDate":"2026-08-05","toDate":"2026-08-05","seats":1}
    -> 201 {"items":[...], "totalItems":n, "searchID":"68e094hw"}

    GET  https://api.mysafar.com/v1/flight/search-result/<searchID>?ts=<epoch_ms>
    -> {"flights":[...], "finished": bool}   -- polled every ~3s while the
       site shows its "در حال جستجو برای بهترین قیمت" spinner; more suppliers
       can still answer for 20-30+ seconds after the initial call.

``adultFare`` is the *public selling* price in Rial — i.e. net fare **plus** the
markup already applied. The purchase price / commission / applied-markup
breakdown seen in the admin tooltip comes from the authenticated endpoint
``/v1/flight/client/find`` and is not exposed publicly; see ``markup.py`` for how
base fares are supplied.

**Two bugs lived here until 2026-08-08, found by directly watching the poll
responses against the real site.** First: the poll response's flight list is
keyed ``"flights"``, not ``"items"`` like the initial call — this code read
``more.get("items")``, which is never present on a poll response, so it was
silently ``None`` on *every single poll*. In other words: from whenever this
scraper first shipped until this fix, everything a supplier answered *after*
the very first call was thrown away. The only offers ever captured were
whatever came back in that first, often-incomplete response.

Second, on top of reading the wrong field, the loop stopped as soon as one
poll cycle added zero new offers — which, combined with the first bug, meant
it *always* stopped after exactly one poll (three seconds), since "new
items" was always zero. Watching real searches end-to-end (IKA-ISTALL,
IKA-NJF) shows the site's own ``finished`` flag routinely stays ``false``
for 20-30 seconds and multiple consecutive quiet poll cycles before flipping
true and genuinely being done — "no new items this cycle" is not "search
complete", it can just mean the next supplier hasn't answered yet. Now the
loop polls until the site itself reports ``finished``, with a generous cap
as a safety net rather than a heuristic that can't tell "done" from "still
waiting".

Together these explain reports of an airline (e.g. Sepehran) showing up in
the client's report with no MySafar price and an unclear fare type: if that
airline's supplier answered anywhere other than the very first response, our
own price for it was being dropped before it ever reached the comparison.

**A third, separate issue, found chasing that same Sepehran report** — this
one not a scraping bug, a *data* one. For one route/date the API returned
*two* distinct offers for the exact same physical flight (same flight
number, same departure time): one ``flightKey": "7316-..."``,
``flightType: "webservice"`` (system), ``bookingPolicy: null``, priced
19,000,000 Toman; and one ``flightKey: "SP7316-..."`` (note the ``SP``
prefix), ``flightType: "charter"``, ``bookingPolicy: {"restrictedForTour":
true, ...}``, priced 15,190,400 Toman. Taking the cheapest of our own
offers per flight — the normal, correct rule everywhere else — picked the
second one. But a screenshot of mysafar.com's real search results for that
exact route/date shows only 3 flights, every one tagged سیستمی, at prices
matching the *first* (system) offer — the ``SP``-prefixed, tour-restricted
twin is never shown to a real customer at all.

The first version of this fix dropped *every* ``restrictedForTour`` offer
outright, on the theory that the flag itself means "not real, not sold".
That was one data point stretched into a universal rule, and it broke a
different flight almost immediately: a Qeshm Air system fare (19,170,800
Toman, IST-THR) vanished from the report entirely. There was no second,
non-restricted Qeshm offer for that flight to have preferred instead — this
was apparently the *only* price MySafar had for it, just carrying the same
flag for some unrelated reason (a round-trip-only condition, a fare rule,
who knows). Dropping a flight's only known price on an unverified guess
about one field is worse than the phantom-duplicate problem it was meant to
solve.

So the rule is now specifically "prefer the twin, never discard the only
copy": offers are grouped by the flight they actually describe — airline,
cabin, and departure time (*not* ``flightKey``/``supplierId``, which is
exactly what differs between the ``7316``/``SP7316`` pair) — and a
``restrictedForTour`` offer is dropped only when a non-restricted offer for
that *same* flight also exists in the same batch. A restricted offer with no
such twin is kept, restriction flag and all; it's still whatever real price
MySafar returned, and hiding it entirely was the actual bug being chased.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

FIND_URL = "https://api.mysafar.com/v1/flight/find"
RESULT_URL = "https://api.mysafar.com/v1/flight/search-result/{}"
#: real searches were observed taking up to ~25s (9 polls at the 3s
#: interval below) to report finished; this is a generous cap in case a
#: search never sets the flag, not the expected case.
MAX_POLLS = 40
POLL_INTERVAL_S = 3.0


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
                time.sleep(POLL_INTERVAL_S)
                more = self.fetcher.get(
                    RESULT_URL.format(search_id),
                    params={"ts": int(time.time() * 1000)},
                    headers=headers,
                )
                # the poll endpoint's own key — see the module docstring for
                # how long this went unnoticed reading "items" here instead.
                _collect(items, more.get("flights") or [])
                if more.get("finished"):
                    break

        priced = [it for it in items.values() if it.get("adultFare")]
        return [self._to_offer(it, route, day) for it in _drop_redundant_tour_offers(priced)]

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


def _is_tour_restricted(it: Dict[str, Any]) -> bool:
    return bool((it.get("bookingPolicy") or {}).get("restrictedForTour"))


def _flight_identity(it: Dict[str, Any]) -> tuple:
    """Same physical flight, independent of which fare/product wraps it —
    unlike ``flightKey``, which is exactly what differs between a system
    offer (``"7316-..."``) and its tour-restricted twin (``"SP7316-..."``).
    """
    return (it.get("airline"), it.get("cabinType"), it.get("originLocalTime"))


def _drop_redundant_tour_offers(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop a ``restrictedForTour`` offer only when a non-restricted offer
    for the *same flight* also exists — see the module docstring for why a
    restricted offer with no such twin must be kept rather than discarded.
    """
    has_real_twin = set()
    for it in items:
        if not _is_tour_restricted(it):
            has_real_twin.add(_flight_identity(it))
    return [
        it for it in items
        if not (_is_tour_restricted(it) and _flight_identity(it) in has_real_twin)
    ]


def _collect(bucket: Dict[str, Dict[str, Any]], items: List[Dict[str, Any]]) -> None:
    for it in items:
        key = "{}|{}|{}|{}".format(
            it.get("flightKey"), it.get("cabinType"), it.get("supplierId"), it.get("adultFare")
        )
        bucket[key] = it
