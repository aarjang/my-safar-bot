"""Scraper contract + registry.

Adding a competitor = drop a module in this package, subclass ``BaseScraper``,
set ``name``, implement ``fetch``, and register it. Nothing else in the pipeline
needs to change.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional, Type

from ..http import Fetcher, RequestCancelled
from ..models import FlightOffer, RouteSpec, ScrapeResult

log = logging.getLogger(__name__)


class BaseScraper:
    name: str = "base"
    #: human label for reports
    label: str = "Base"
    #: needs a headless browser (excluded unless --with-browser)
    requires_browser: bool = False

    def __init__(self, fetcher: Fetcher, options: Optional[Dict[str, Any]] = None) -> None:
        self.fetcher = fetcher
        self.options = options or {}

    # --- to implement -------------------------------------------------------
    def fetch(self, route: RouteSpec, day: str, adults: int = 1) -> List[FlightOffer]:
        raise NotImplementedError

    # --- shared -------------------------------------------------------------
    def scrape(self, route: RouteSpec, day: str, adults: int = 1) -> ScrapeResult:
        started = time.monotonic()
        try:
            offers = self.fetch(route, day, adults=adults)
            return ScrapeResult(
                source=self.name,
                route=route.id,
                search_date=day,
                offers=offers,
                ok=True,
                elapsed_s=round(time.monotonic() - started, 2),
            )
        except RequestCancelled:
            # a user-initiated cancel, not a scraper failure — let the
            # orchestrator's per-source loop see this and stop immediately
            # instead of logging it as a competitor error.
            raise
        except Exception as exc:  # a dead competitor must not kill the run
            log.error("[%s] %s %s failed: %s", self.name, route.id, day, exc)
            return ScrapeResult(
                source=self.name,
                route=route.id,
                search_date=day,
                offers=[],
                ok=False,
                error="{}: {}".format(type(exc).__name__, exc),
                elapsed_s=round(time.monotonic() - started, 2),
            )


_REGISTRY: Dict[str, Type[BaseScraper]] = {}


def register(cls: Type[BaseScraper]) -> Type[BaseScraper]:
    _REGISTRY[cls.name] = cls
    return cls


def get_scraper_classes(names: Optional[List[str]] = None) -> List[Type[BaseScraper]]:
    from . import alibaba, flytoday, mrbilit, mysafar, snapptrip, tktfly  # noqa: F401  (import side effect: registration)

    if names:
        missing = [n for n in names if n not in _REGISTRY]
        if missing:
            raise KeyError("unknown scraper(s): {} — available: {}".format(missing, sorted(_REGISTRY)))
        return [_REGISTRY[n] for n in names]
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]


def all_scraper_names() -> List[str]:
    get_scraper_classes()
    return sorted(_REGISTRY)
