"""آماده سفر / Charter724 — https://tktfly.ir

No JSON API: results are server-rendered into the page, so a plain GET is enough
(no headless browser). The URL the site's own search button builds is

    https://tktfly.ir/Ticket-<OriginEnglish>-<DestEnglish>.html?t=<JALALI YYYY-MM-DD>

e.g. ``/Ticket-Tehran-Istanbul.html?t=1405-05-14``. Slashes in the date give a
404 — the dashed form is required.

Each result is a ``div.resu`` block:

    <div class="resu">
      <div class="price"><span>16,000</span><div class="icon11">اکونومی</div></div>
      <div class="date">04:30 …</div>
      <div class="user">9+</div>
      <div class="code"> … tooltip with airline / aircraft / baggage …
           <span class="code_inn">9214</span></div>
      <div class="select" rel="16,000,000*5*5"> … </div>
    </div>

``div.price span`` is thousands-of-Toman; ``div.select[rel]`` carries the exact
Toman figure, which is what we use.

This is the client's primary benchmark ("اولویت اصلی همیشه آماده سفره") — MySafar
is normally ~10,000 Toman below it.
"""
from __future__ import annotations

import re
from typing import List, Optional

from bs4 import BeautifulSoup

from ..jalali import to_jalali_dash
from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

BASE_URL = "https://tktfly.ir/Ticket-{origin}-{destination}.html"
_DIGITS = re.compile(r"[\d,]+")
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


@register
class TktFlyScraper(BaseScraper):
    name = "tktfly"
    label = "آماده سفر (tktfly)"

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        origin = route.tktfly_origin or route.origin_city
        destination = route.tktfly_destination or route.destination_city
        url = BASE_URL.format(origin=origin, destination=destination)
        html = self.fetcher.get(
            url,
            params={"t": to_jalali_dash(day)},
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": "https://tktfly.ir/"},
            expect_json=False,
        )
        return self.parse(html, route, day)

    # exposed for unit tests against a saved page
    def parse(self, html: str, route: RouteSpec, day: str) -> List[FlightOffer]:
        soup = BeautifulSoup(html, "lxml")
        offers: List[FlightOffer] = []

        for block in soup.select("div.resu"):
            price_rial = _price_from_block(block)
            if price_rial is None:
                continue  # "CLOSE" rows — sold out, no price

            price_div = block.select_one("div.price")
            cabin_raw = None
            if price_div is not None:
                icon = price_div.select_one(".icon11")
                if icon is not None:
                    cabin_raw = icon.get_text(strip=True)

            date_div = block.select_one("div.date")
            dep_time = _clean(date_div.get_text(" ", strip=True)) if date_div else None
            if dep_time:
                m = re.search(r"\d{1,2}:\d{2}", dep_time)
                dep_time = m.group(0) if m else None

            seats = None
            user_div = block.select_one("div.user")
            if user_div is not None:
                txt = _clean(user_div.get_text(strip=True)).replace("+", "")
                seats = int(txt) if txt.isdigit() else None

            code_el = block.select_one("span.code_inn")
            flight_number = code_el.get_text(strip=True) if code_el else None

            cabin = normalize_cabin(cabin_raw)
            cabin_inferred = False
            if cabin == "unknown":
                # Rows without a class chip still encode it in the flight code:
                # "7902C"/"5291C" carry the بیزینس label, "6846P" the پرمیوم one.
                inferred = _cabin_from_code(flight_number)
                if inferred:
                    cabin, cabin_inferred = inferred, True
                else:
                    # Neither a chip nor a coded suffix means economy — tktfly
                    # only marks the premium classes, economy is the unlabelled
                    # default. Leaving these "unknown" was splitting a flight
                    # into two report rows (an economy one and a phantom
                    # "نامشخص" one carrying only tktfly), because a cabin that
                    # matches no other site can never join their row.
                    # Checked against the other five sites on 419 offers: all
                    # 48 such rows priced inside the economy range for their
                    # own flight, none anywhere near business.
                    cabin, cabin_inferred = "economy", True

            tooltip = block.select_one(".efitooltip")
            airline_name = baggage = aircraft = None
            if tooltip is not None:
                name_el = tooltip.select_one(".airline_name")
                airline_name = name_el.get_text(strip=True) if name_el else None
                ttext = _clean(tooltip.get_text(" ", strip=True))
                baggage = _after(ttext, "بار مجاز:")
                aircraft = _after(ttext, "نوع هواپیما:")

            offers.append(
                FlightOffer(
                    source=self.name,
                    route=route.id,
                    search_date=day,
                    price_rial=price_rial,
                    airline_name=airline_name,
                    flight_number=flight_number,
                    cabin=cabin,
                    cabin_raw=cabin_raw,
                    origin=route.origin_airport or route.origin_city,
                    destination=route.destination_airport or route.destination_city,
                    departure_time=dep_time,
                    seats_available=seats,
                    baggage=baggage,
                    fare_type="charter",
                    is_charter=True,
                    deeplink="https://tktfly.ir/Ticket-{}-{}.html?t={}".format(
                        route.tktfly_origin or route.origin_city,
                        route.tktfly_destination or route.destination_city,
                        to_jalali_dash(day),
                    ),
                    raw={
                        "aircraft": aircraft,
                        "fare_options": _fare_options(block),
                        "cabin_inferred": cabin_inferred,
                    },
                )
            )
        return offers


def _price_from_block(block) -> Optional[int]:
    """The displayed ``div.price span`` value, in thousands of Toman.

    ``div.select[rel]`` looks tempting (exact Rial figures) but it is a
    ``|``-separated list of *all* fare options for that flight — for a business
    row its first entry is often the flight's economy fare. The rendered price
    is the one the row actually sells at, so that wins; the rel list is kept in
    ``raw`` for reference. Cost: the figure is rounded to the nearest 1,000
    Toman, which is noise against a 16,000,000-Toman fare.
    """
    price_span = block.select_one("div.price span")
    if price_span is not None:
        txt = _clean(price_span.get_text(strip=True))
        m = _DIGITS.search(txt)
        if m:
            digits = m.group(0).replace(",", "")
            if digits.isdigit() and int(digits) > 0:
                return int(digits) * 1000 * 10  # thousand-Toman -> Rial

    # "CLOSE" rows have no price at all; fall back to the fare list if present
    sel = block.select_one("div.select")
    if sel is not None and sel.get("rel"):
        rel = sel.get("rel")
        rel = rel[0] if isinstance(rel, list) else rel
        head = str(rel).split("|")[0].split("*")[0].replace(",", "").strip()
        if head.isdigit() and int(head) > 0:
            return int(head) * 10
    return None


def _fare_options(block) -> Optional[str]:
    sel = block.select_one("div.select")
    if sel is None or not sel.get("rel"):
        return None
    rel = sel.get("rel")
    rel = rel[0] if isinstance(rel, list) else rel
    return str(rel)[:200]


#: Suffix on tktfly's flight code when the class chip is missing.
_CODE_SUFFIX_CABIN = {"C": "business", "P": "premium_economy"}


def _cabin_from_code(flight_number: Optional[str]) -> Optional[str]:
    if not flight_number or len(flight_number) < 2:
        return None
    return _CODE_SUFFIX_CABIN.get(flight_number[-1].upper())


def _clean(text: str) -> str:
    return text.translate(_PERSIAN_DIGITS).replace("‌", " ").strip()


def _after(text: str, label: str) -> Optional[str]:
    idx = text.find(label)
    if idx < 0:
        return None
    tail = text[idx + len(label):].strip()
    return tail.split("  ")[0].split("قیمت")[0].split("کلاس")[0].strip()[:40] or None
