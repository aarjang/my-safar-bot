"""Live city/airport lookup, sourced from MySafar's own public search API.

This is what lets the dashboard offer *any* destination MySafar sells instead
of a hand-maintained list in config.yaml: MySafar's

    POST https://api.mysafar.com/v1/airport/list
    {"search": "استانبول", "isInternational": true}

already returns everything every scraper needs to search a route:

* ``cityCode`` — the plain IATA city code (``THR``/``IST``) that
  FlyToday/Alibaba/SnappTrip search on directly.
* ``iata`` on the ``isCity: true`` entry — MySafar's own "all airports of this
  city" code (``THRALL``/``ISTALL``), needed for our own ``mysafar`` scraper.
* ``state`` on an airport-level entry sharing that ``cityCode`` — the English
  city name (e.g. "Tehran"), which is what tktfly's URL path needs and which
  no other source in this project exposes.

If a city truly has no English name available (rare — most do), tktfly will
simply fail for that specific route with a clear per-source error; that's
preferable to guessing a spelling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .http import Fetcher

SEARCH_URL = "https://api.mysafar.com/v1/airport/list"


@dataclass
class CityMatch:
    city_code: str  # THR, IST — what flytoday/alibaba/snapptrip search on
    city_fa: str  # تهران
    english_name: Optional[str]  # Tehran — what tktfly's URL needs
    mysafar_code: str  # THRALL, ISTALL (or a single airport's own IATA as fallback)
    country_fa: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "city_code": self.city_code,
            "city_fa": self.city_fa,
            "english_name": self.english_name,
            "mysafar_code": self.mysafar_code,
            "country_fa": self.country_fa,
        }


def search_cities(
    fetcher: Fetcher, query: str, international: Optional[bool] = None, limit: int = 12
) -> List[CityMatch]:
    if not query or len(query.strip()) < 2:
        return []
    payload: Dict[str, Any] = {"search": query.strip()}
    if international is not None:
        payload["isInternational"] = international
    data = fetcher.post(
        SEARCH_URL,
        json_body=payload,
        headers={"Origin": "https://www.mysafar.com", "Content-Type": "application/json"},
    )
    items = data.get("items") or []

    city_entries: Dict[str, Dict[str, Any]] = {}
    english_by_code: Dict[str, str] = {}
    airport_fallback: Dict[str, Dict[str, Any]] = {}

    for it in items:
        code = it.get("cityCode")
        if not code:
            continue
        if it.get("isCity"):
            city_entries.setdefault(code, it)
        else:
            airport_fallback.setdefault(code, it)
            if it.get("state") and code not in english_by_code:
                english_by_code[code] = it["state"]

    matches: List[CityMatch] = []
    seen = set()
    # city-level entries first (these carry the "ALL airports" code, the best
    # destination value to search with), then any airport-only cities that
    # never surfaced a dedicated city entry in this result page.
    for code, it in list(city_entries.items()) + [
        (c, a) for c, a in airport_fallback.items() if c not in city_entries
    ]:
        if code in seen:
            continue
        seen.add(code)
        matches.append(
            CityMatch(
                city_code=code,
                city_fa=it.get("cityFa") or "",
                english_name=english_by_code.get(code),
                mysafar_code=it.get("iata") or code,
                country_fa=it.get("countryFa"),
            )
        )
        if len(matches) >= limit:
            break
    return matches
