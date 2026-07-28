"""Markup maths.

    markup% = ((competitor_price - base_fare) / base_fare) * 100

The honest caveat: **base_fare (نرخ نت / قیمت خرید) is not public on any of these
sites.** MySafar's own admin tooltip shows «قیمت خرید / کمیسیون پیشنهادی /
مارک‌آپ اعمال‌شده / قیمت فروش», but that breakdown comes from the authenticated
``/v1/flight/client/find`` endpoint. So the base fare has to come from us:

``base`` strategies
-------------------
``file``     read net fares from a CSV the agency exports
             (columns: route,date,cabin,airline,flight_number,base_fare_rial).
             Most accurate — use this in production.
``mysafar``  treat MySafar's own public price as the reference. The resulting
             number is not a true markup but a *relative premium* — how much
             each competitor sits above us. This is what the client actually
             eyeballs today.
``min``      use the cheapest price found across all sources as the reference.
             Zero-config sanity check; drifts whenever a competitor dumps stock.

Whichever is used is recorded in the report so nobody reads a relative premium
as a real markup.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .models import FlightOffer

BASE_MYSAFAR = "mysafar"
BASE_MIN = "min"
BASE_FILE = "file"


@dataclass
class BaseFare:
    route: str
    date: str
    cabin: str
    base_fare_rial: int
    airline: Optional[str] = None
    flight_number: Optional[str] = None


class BaseFareTable:
    """Net fares supplied by the agency, looked up most-specific-first."""

    def __init__(self, rows: Optional[List[BaseFare]] = None) -> None:
        self._rows: List[BaseFare] = rows or []

    @classmethod
    def from_csv(cls, path: str) -> "BaseFareTable":
        rows: List[BaseFare] = []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if not r.get("base_fare_rial"):
                    continue
                rows.append(
                    BaseFare(
                        route=(r.get("route") or "").strip(),
                        date=(r.get("date") or "").strip(),
                        cabin=(r.get("cabin") or "economy").strip(),
                        base_fare_rial=int(float(r["base_fare_rial"])),
                        airline=(r.get("airline") or "").strip() or None,
                        flight_number=(r.get("flight_number") or "").strip() or None,
                    )
                )
        return cls(rows)

    def lookup(self, route: str, date: str, cabin: str, airline: Optional[str] = None,
               flight_number: Optional[str] = None) -> Optional[int]:
        best: Optional[Tuple[int, int]] = None  # (specificity, fare)
        for r in self._rows:
            if r.route != route or r.date != date or r.cabin != cabin:
                continue
            score = 0
            if r.flight_number:
                if flight_number and r.flight_number == flight_number:
                    score += 2
                else:
                    continue
            if r.airline:
                if airline and r.airline.upper() == airline.upper():
                    score += 1
                else:
                    continue
            if best is None or score > best[0]:
                best = (score, r.base_fare_rial)
        return best[1] if best else None


def markup_percent(price_rial: int, base_fare_rial: int) -> Optional[float]:
    if not base_fare_rial:
        return None
    return round((price_rial - base_fare_rial) / base_fare_rial * 100.0, 2)


def resolve_base_fare(
    strategy: str,
    offers_by_source: Dict[str, List[FlightOffer]],
    route: str,
    day: str,
    cabin: str,
    table: Optional[BaseFareTable] = None,
) -> Tuple[Optional[int], str]:
    """Return ``(base_fare_rial, source_label)`` for one (route, date, cabin) cell."""
    if strategy == BASE_FILE and table is not None:
        fare = table.lookup(route, day, cabin)
        if fare:
            return fare, "file"

    if strategy in (BASE_FILE, BASE_MYSAFAR):
        ours = [o for o in offers_by_source.get("mysafar", []) if o.cabin == cabin]
        if ours:
            return min(o.price_rial for o in ours), "mysafar_public"

    pool = [o.price_rial for offers in offers_by_source.values() for o in offers if o.cabin == cabin]
    if pool:
        return min(pool), "market_min"
    return None, "none"


def write_base_fare_template(path: str, routes: List[str], days: List[str],
                             cabins: Optional[List[str]] = None) -> None:
    """Emit an empty CSV for the agency to fill with real net fares."""
    cabins = cabins or ["economy", "business"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["route", "date", "cabin", "airline", "flight_number", "base_fare_rial"])
        for route in routes:
            for day in days:
                for cabin in cabins:
                    w.writerow([route, day, cabin, "", "", ""])
