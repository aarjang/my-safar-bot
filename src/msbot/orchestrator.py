"""Shared scrape-running logic used by both the CLI and the web dashboard.

Centralized here (rather than duplicated in cli.py and web/jobs.py) so the
rate-limiting behavior and progress reporting are identical no matter which
front-end triggered the run.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from .http import Fetcher, RequestCancelled
from .models import RouteSpec, ScrapeResult
from .scrapers.base import get_scraper_classes

log = logging.getLogger(__name__)

ProgressCallback = Callable[[ScrapeResult], None]

#: prefix on a synthetic ScrapeResult's error when a source is skipped by the
#: circuit breaker rather than actually failing a request — jobs.py keys off
#: this to show "رد شد" (skipped) instead of "خطا" (error) in the dashboard.
CIRCUIT_BREAKER_PREFIX = "circuit-breaker:"

#: how many *consecutive* failed requests a single source tolerates in one run
#: before we stop hitting it entirely and skip its remaining tasks instantly.
#: This is what turns "one source spirals into a 40-minute retry and the
#: whole run just sits there" into "that source gets marked skipped in a
#: couple seconds and everything else finishes normally."
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3


def build_fetcher(cfg: Dict[str, Any]) -> Fetcher:
    rl = cfg["rate_limit"]
    return Fetcher(
        min_interval=rl["min_interval"],
        jitter=rl["jitter"],
        timeout=rl["timeout"],
        retries=rl["retries"],
        proxy=cfg.get("proxy"),
        per_host=dict(rl.get("per_host") or {}),
        per_host_ceiling=rl.get("per_host_ceiling", 45.0),
        max_request_budget=rl.get("max_request_budget", 180.0),
        cooldown_after=rl.get("cooldown_after", 2),
        cooldown_seconds=rl.get("cooldown_seconds", 60.0),
    )


def run_scrapers(
    cfg: Dict[str, Any],
    routes: List[RouteSpec],
    days: List[str],
    sources: List[str],
    adults: int = 1,
    workers: int = 5,
    fetcher: Optional[Fetcher] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    max_consecutive_errors: Optional[int] = None,
) -> List[ScrapeResult]:
    """Run every (source x route x day) combination.

    Parallel across sources (so a slow/rate-limited site never blocks the
    others); sequential per source over routes/dates, since that is what the
    rate limiter is built to pace. ``on_progress`` fires after each single
    (route, day) result — this is what lets a UI show live status instead of
    waiting for the whole batch.

    A source that fails ``max_consecutive_errors`` times in a row (429s, site
    down, etc.) trips a per-source circuit breaker: remaining tasks for that
    source are marked skipped immediately, with no network call, instead of
    grinding through the same failure with ever-longer backoff. Other sources
    are unaffected and the run still finishes.
    """
    owns_fetcher = fetcher is None
    fetcher = fetcher or build_fetcher(cfg)
    if fetcher.cancel_event is None:
        fetcher.cancel_event = cancel_event
    max_errors = max_consecutive_errors or cfg["rate_limit"].get(
        "max_consecutive_errors", DEFAULT_MAX_CONSECUTIVE_ERRORS
    )
    opts = cfg.get("scraper_options") or {}
    scrapers = [cls(fetcher, opts.get(cls.name)) for cls in get_scraper_classes(sources)]
    results: List[ScrapeResult] = []
    results_lock = threading.Lock()

    def emit(res: ScrapeResult) -> None:
        with results_lock:
            results.append(res)
        if on_progress:
            try:
                on_progress(res)
            except Exception:  # a broken UI callback must not kill the scrape
                log.exception("on_progress callback failed")

    def per_source(scraper) -> None:
        consecutive_errors = 0
        tripped = False
        for route in routes:
            for day in days:
                if cancel_event is not None and cancel_event.is_set():
                    return
                if tripped:
                    emit(ScrapeResult(
                        source=scraper.name, route=route.id, search_date=day, offers=[],
                        ok=False,
                        error="{} skipped after {} consecutive failures this run".format(
                            CIRCUIT_BREAKER_PREFIX, max_errors
                        ),
                    ))
                    continue
                try:
                    res = scraper.scrape(route, day, adults=adults)
                except RequestCancelled:
                    return
                emit(res)
                if res.ok:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= max_errors:
                        tripped = True
                        log.warning(
                            "%s: %d consecutive failures, skipping its remaining %d task(s) this run",
                            scraper.name, consecutive_errors,
                            len(routes) * len(days) - (routes.index(route) * len(days) + days.index(day) + 1),
                        )

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(scrapers) or 1))) as pool:
            list(pool.map(per_source, scrapers))
    finally:
        if owns_fetcher:
            fetcher.close()

    return results


def total_tasks(routes: List[RouteSpec], days: List[str], sources: List[str]) -> int:
    return len(routes) * len(days) * len(sources)
