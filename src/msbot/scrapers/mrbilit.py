"""مستر بلیط — https://mrbilit.com  (headless browser required)

Unlike the other four competitors, mrbilit has **no usable REST search**:

* the page at ``/flights/THR-IST?departureDate=2026-08-05&adult=1`` is not
  server-rendered (the raw HTML contains no fares, no ``__NUXT_DATA__`` payload);
* the only REST calls the page makes are ``/api/Airports`` and
  ``/api/Flights/MinPrices`` on ``https://flight.atighgasht.com``;
* the result list itself streams in over **SignalR (WebSocket)** — the generated
  client does expose ``POST https://flight.atighgasht.com/api/Flights``
  (``Content-Type: application/json-patch+json``) but every payload shape we
  tried returns HTTP 500, so it is not a public path.

So this scraper drives a real browser and reads the rendered cards. Install with::

    pip install playwright && playwright install chromium

Each card is ``div.trip-package-info`` and its text reads:

    آتا | 06:45 | 3 س 35 د | 09:50 | تهران (IKA) | استانبول (IST)
      | اکونومی | ایرباس A320 | 25 کیلوگرم | 3 صندلی | 15,935,500 | تومانء
"""
from __future__ import annotations

import random
import re
from typing import List, Optional

from ..models import FlightOffer, RouteSpec, normalize_cabin
from .base import BaseScraper, register

SEARCH_URL = "https://mrbilit.com/flights/{origin}-{destination}?departureDate={day}&adult={adults}"

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٬،", "0123456789,,")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_PRICE = re.compile(r"^[\d,]{4,}$")
_AIRPORT = re.compile(r"\(([A-Z]{3})\)")
_DURATION = re.compile(r"(?:(\d+)\s*س)?\s*(?:(\d+)\s*د)?")
_SEATS = re.compile(r"(\d+)\s*صندلی")


@register
class MrBilitScraper(BaseScraper):
    name = "mrbilit"
    label = "مستر بلیط"
    requires_browser = True

    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "mrbilit needs playwright: pip install playwright && playwright install chromium"
            ) from exc

        url = SEARCH_URL.format(
            origin=route.origin_city, destination=route.destination_city, day=day, adults=adults
        )
        wait_ms = int(self.options.get("wait_ms", 20000))
        texts: List[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=bool(self.options.get("headless", True)),
                # a real Chrome window doesn't carry this CDP-automation
                # marker; disabling it is standard practice for browser
                # testing/scraping (not aimed at any deliberate challenge —
                # we still don't touch CAPTCHAs or WAF fingerprint checks).
                args=["--disable-blink-features=AutomationControlled"],
                **_launch_target(self.options)
            )
            try:
                ctx = browser.new_context(
                    locale="fa-IR",
                    timezone_id="Asia/Tehran",  # a fa-IR locale from a non-Iran clock is its own tell
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1440, "height": 900},
                )
                # Playwright's default CDP session leaves navigator.webdriver
                # readable as true; a normal tab never has it set. This one
                # init script is the extent of the fingerprint work here —
                # no canvas/WebGL spoofing, no proxying around a real
                # challenge (CAPTCHA, WAF JS puzzle, etc.), which we won't do.
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.wait_for_selector("div.trip-package-info", timeout=wait_ms)
                except Exception:
                    return []  # genuinely no flights, or the stream never finished
                # a fixed, perfectly-repeatable wait is itself a robotic tell;
                # a little jitter costs nothing and looks more like a person
                # reading the page before it finishes loading.
                page.wait_for_timeout(3500 + int(1500 * random.random()))
                texts = page.eval_on_selector_all(
                    "div.trip-package-info", "els => els.map(e => e.innerText)"
                )
            finally:
                browser.close()

        return [o for o in (self._parse_card(t, route, day) for t in texts) if o]

    def _parse_card(self, text: str, route: RouteSpec, day: str) -> Optional[FlightOffer]:
        lines = [l.strip() for l in text.translate(_PERSIAN_DIGITS).splitlines() if l.strip()]
        if not lines:
            return None

        times = [l for l in lines if _TIME.match(l)]
        prices = [int(l.replace(",", "")) for l in lines if _PRICE.match(l) and len(l.replace(",", "")) >= 6]
        if not prices:
            return None
        price_toman = max(prices)  # the payable total is the largest figure on the card

        airports = _AIRPORT.findall(text)
        cabin_raw = next((l for l in lines if normalize_cabin(l) != "unknown"), None)
        seats_m = _SEATS.search(text)
        baggage = next((l for l in lines if "کیلوگرم" in l), None)
        duration = next((l for l in lines if "س" in l and "د" in l and any(c.isdigit() for c in l)), None)

        return FlightOffer(
            source=self.name,
            route=route.id,
            search_date=day,
            price_rial=price_toman * 10,
            airline_name=lines[0],
            cabin=normalize_cabin(cabin_raw),
            cabin_raw=cabin_raw,
            origin=airports[0] if airports else route.origin_airport,
            destination=airports[-1] if len(airports) > 1 else route.destination_airport,
            departure_time=times[0] if times else None,
            arrival_time=times[1] if len(times) > 1 else None,
            duration_min=_duration_minutes(duration),
            seats_available=int(seats_m.group(1)) if seats_m else None,
            baggage=baggage,
            deeplink=SEARCH_URL.format(
                origin=route.origin_city, destination=route.destination_city, day=day, adults=1
            ),
        )


#: Playwright's own CDN (cdn.playwright.dev) is geo-blocked from Iran — the
#: chromium download fails with HTTP 403 "not available in your location". So we
#: default to whatever Chrome/Edge is already installed instead of the bundled
#: build. Override with ``scraper_options.mrbilit.executable_path`` or
#: ``.channel`` in the config if the binary lives elsewhere.
_LOCAL_CHROMES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def _launch_target(options: dict) -> dict:
    import os
    from pathlib import Path

    explicit = options.get("executable_path") or os.environ.get("MSBOT_CHROME")
    if explicit:
        return {"executable_path": explicit}
    if options.get("channel"):
        return {"channel": options["channel"]}
    if options.get("use_bundled_chromium"):
        return {}
    for candidate in _LOCAL_CHROMES:
        if Path(candidate).exists():
            return {"executable_path": candidate}
    return {}  # fall back to the bundled build, if it ever downloaded


def _duration_minutes(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = _DURATION.search(text)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    total = hours * 60 + minutes
    return total or None
