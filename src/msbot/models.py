"""Normalized data model shared by every scraper.

All monetary values are stored in **Rial (IRR)**. Sites publish prices in a mix
of Rial and Toman; each scraper is responsible for converting to Rial so that
downstream comparison / markup math never has to care about the source.
"""
from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# --- cabin classes -----------------------------------------------------------

ECONOMY = "economy"
PREMIUM_ECONOMY = "premium_economy"
BUSINESS = "business"
FIRST = "first"
UNKNOWN_CABIN = "unknown"

_CABIN_ALIASES = {
    # english / api values
    "economy": ECONOMY,
    "eco": ECONOMY,
    "y": ECONOMY,
    "coach": ECONOMY,
    "economystandard": ECONOMY,
    "premiumeconomy": PREMIUM_ECONOMY,
    "premium_economy": PREMIUM_ECONOMY,
    "premium": PREMIUM_ECONOMY,
    "economyplus": PREMIUM_ECONOMY,
    "economy plus": PREMIUM_ECONOMY,
    "business": BUSINESS,
    "businessclass": BUSINESS,
    "c": BUSINESS,
    "j": BUSINESS,
    "first": FIRST,
    "firstclass": FIRST,
    "f": FIRST,
    # persian
    "اکونومی": ECONOMY,
    "اقتصادی": ECONOMY,
    "کلاس اقتصادی": ECONOMY,
    "پرمیوم": PREMIUM_ECONOMY,
    "پرمیوم اکونومی": PREMIUM_ECONOMY,
    "اکونومی پلاس": PREMIUM_ECONOMY,
    "بیزینس": BUSINESS,
    "بیزنس": BUSINESS,
    "تجاری": BUSINESS,
    "فرست": FIRST,
    "درجه یک": FIRST,
}


def normalize_cabin(raw: Optional[str]) -> str:
    """Map any site-specific cabin label onto our four canonical classes."""
    if not raw:
        return UNKNOWN_CABIN
    key = str(raw).strip().lower().replace("‌", " ")
    if key in _CABIN_ALIASES:
        return _CABIN_ALIASES[key]
    compact = key.replace(" ", "").replace("-", "").replace("_", "")
    if compact in _CABIN_ALIASES:
        return _CABIN_ALIASES[compact]
    # substring fallback — sites like to append fare-family names
    for alias, canonical in _CABIN_ALIASES.items():
        if len(alias) > 2 and alias in key:
            return canonical
    return UNKNOWN_CABIN


# --- offers ------------------------------------------------------------------


@dataclass
class FlightOffer:
    """One purchasable fare for one flight on one site."""

    source: str  # scraper id, e.g. "flytoday"
    route: str  # "THR-IST"
    search_date: str  # requested departure date, ISO "YYYY-MM-DD"
    price_rial: int  # total adult fare, Rial

    airline_code: Optional[str] = None
    airline_name: Optional[str] = None
    flight_number: Optional[str] = None
    cabin: str = UNKNOWN_CABIN
    cabin_raw: Optional[str] = None
    origin: Optional[str] = None  # airport iata actually flown, e.g. IKA
    destination: Optional[str] = None
    departure_time: Optional[str] = None  # local ISO or "HH:MM"
    arrival_time: Optional[str] = None
    duration_min: Optional[int] = None
    stops: Optional[int] = None
    seats_available: Optional[int] = None
    baggage: Optional[str] = None
    fare_type: Optional[str] = None  # charter / system / publish ...
    is_charter: Optional[bool] = None
    deeplink: Optional[str] = None
    scraped_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def price_toman(self) -> int:
        return self.price_rial // 10

    def offer_key(self) -> str:
        """Stable id for the same flight+fare across runs (for history dedup)."""
        parts = [
            self.source,
            self.route,
            self.search_date,
            self.flight_number or "",
            self.airline_code or "",
            self.cabin,
            (self.departure_time or "")[-5:],
            self.fare_type or "",
        ]
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("raw", None)
        d["price_toman"] = self.price_toman
        d["offer_key"] = self.offer_key()
        return d


@dataclass
class ScrapeResult:
    """What a scraper returns for a single (route, date) request."""

    source: str
    route: str
    search_date: str
    offers: List[FlightOffer] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None
    elapsed_s: float = 0.0

    def cheapest(self, cabin: Optional[str] = None) -> Optional[FlightOffer]:
        pool = [o for o in self.offers if cabin is None or o.cabin == cabin]
        return min(pool, key=lambda o: o.price_rial) if pool else None


@dataclass
class RouteSpec:
    """A route plus the per-site identifiers needed to query it.

    Sites disagree on how to name a city: FlyToday/Alibaba/SnappTrip want the
    city IATA (THR/IST), MySafar wants airport-ish codes (IKA/ISTALL), tktfly
    wants english city names in the URL path.
    """

    id: str  # "THR-IST"
    origin_city: str  # THR
    destination_city: str  # IST
    origin_airport: Optional[str] = None  # IKA
    destination_airport: Optional[str] = None  # IST
    tktfly_origin: Optional[str] = None  # "Tehran"
    tktfly_destination: Optional[str] = None  # "Istanbul"
    mysafar_origin: Optional[str] = None  # "IKA"
    mysafar_destination: Optional[str] = None  # "ISTALL"
    label_fa: Optional[str] = None
    #: single-city Persian labels, used by the dashboard's origin/destination
    #: pickers — e.g. "تهران" / "استانبول". Falls back to the IATA code if unset.
    origin_label_fa: Optional[str] = None
    destination_label_fa: Optional[str] = None

    def reversed(self) -> "RouteSpec":
        """Return-leg spec — the client always re-checks the opposite direction."""
        return RouteSpec(
            id="{}-{}".format(self.destination_city, self.origin_city),
            origin_city=self.destination_city,
            destination_city=self.origin_city,
            origin_airport=self.destination_airport,
            destination_airport=self.origin_airport,
            tktfly_origin=self.tktfly_destination,
            tktfly_destination=self.tktfly_origin,
            mysafar_origin=self.mysafar_destination,
            mysafar_destination=self.mysafar_origin,
            label_fa=None,
            origin_label_fa=self.destination_label_fa,
            destination_label_fa=self.origin_label_fa,
        )


def date_range(start: date, days: int) -> List[str]:
    from datetime import timedelta

    return [(start + timedelta(days=i)).isoformat() for i in range(days)]
