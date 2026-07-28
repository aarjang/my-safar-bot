"""Shared HTTP plumbing: browser-ish headers, per-host rate limiting, retries.

Every scraper goes through :class:`Fetcher`. The rate limiter is per *host* and
sequential, so even if we widen concurrency later, no single site sees a
burst — and it's built to behave like a single patient human clicking around,
not a script hammering the endpoint: real headers, a floor between requests,
and backoff that goes back down once a site recovers instead of only ever
going up.

**Incident (2026-07-28):** a live run against flytoday spiraled — 53s, 98s,
218s, 489s, 1098s, 2469s per request, each roughly 2.25x the last. Cause: on
every 429 the per-host floor was multiplied by 1.5x with no ceiling and never
decayed back down, so a handful of consecutive 429s compounded into a
41-minute wait on a single request, and because scraping within one source is
sequential, that one request stalled the *entire* source (and the dashboard's
progress bar) for the rest of the run. Fixed by ``per_host_ceiling`` (a hard
cap the penalty can never cross) and ``note_success`` (decay back toward the
configured baseline once requests start succeeding again).
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

#: A real Chrome tab sends all of these on every XHR/fetch — a client that only
#: sends User-Agent + Accept is itself a fingerprint some anti-bot layers key
#: on. None of this defeats a determined block; it just avoids being the
#: easiest, laziest signal to flag.
BASE_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "sec-ch-ua": '"Not/A)Brand";v="99", "Chromium";v="128", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

RETRY_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}

#: however many consecutive 429s a host's floor is bumped for, it can never be
#: asked to wait longer than this between requests.
DEFAULT_PER_HOST_CEILING = 45.0
#: a single request will not be retried past this much total wall-clock time
#: (waiting + attempts) — better to surface a clear failure fast than to sit
#: silently for tens of minutes.
DEFAULT_MAX_REQUEST_BUDGET = 120.0


class RateLimiter:
    """Minimum delay between requests to the same host, with jitter.

    ``per_host`` sets each host's *starting* floor (flytoday needs a much
    bigger gap than the others before it starts answering 429). That floor
    can stretch further at runtime when a host actually 429s, but never past
    ``per_host_ceiling`` — and once requests succeed again, :meth:`note_success`
    eases it back down toward the configured floor rather than leaving it
    stuck at whatever peak it reached.
    """

    def __init__(
        self,
        min_interval: float = 2.0,
        jitter: float = 1.0,
        per_host: Optional[Dict[str, float]] = None,
        per_host_ceiling: float = DEFAULT_PER_HOST_CEILING,
    ) -> None:
        self.min_interval = min_interval
        self.jitter = jitter
        self.per_host = dict(per_host or {})
        # the *configured* floor per host — note_success decays back toward
        # this, never below it; per_host itself is mutated up/down at runtime.
        self._configured_floor = dict(per_host or {})
        self.per_host_ceiling = per_host_ceiling
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def current_floor(self, host: str) -> float:
        return self.per_host.get(host, self.min_interval)

    def note_429(self, host: str) -> float:
        """Stretch this host's floor after a 429 — capped, never unbounded."""
        with self._lock:
            current = self.per_host.get(host, self.min_interval)
            bumped = min(max(current * 1.5, 8.0), self.per_host_ceiling)
            self.per_host[host] = bumped
            return bumped

    def note_success(self, host: str) -> None:
        """Ease a stretched floor back down once a host is behaving again."""
        if host not in self.per_host:
            return
        with self._lock:
            floor = self._configured_floor.get(host, self.min_interval)
            current = self.per_host[host]
            if current > floor:
                self.per_host[host] = max(floor, current * 0.85)

    def wait(self, host: str, cancel_event: Optional[threading.Event] = None) -> bool:
        """Sleep until this host's floor has elapsed. Returns False if
        ``cancel_event`` fired while waiting (caller should abort), True
        otherwise. Sleeps in short slices so a cancel is noticed within ~1s
        instead of only after the whole wait elapses.
        """
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host, 0.0)
            base = self.per_host.get(host, self.min_interval)
            gap = base + random.uniform(0, self.jitter)
            sleep_for = last + gap - now
            self._last[host] = max(now, last + gap)
        return _interruptible_sleep(sleep_for, cancel_event)


def _interruptible_sleep(seconds: float, cancel_event: Optional[threading.Event]) -> bool:
    if seconds <= 0:
        return not (cancel_event is not None and cancel_event.is_set())
    if cancel_event is None:
        time.sleep(seconds)
        return True
    return not cancel_event.wait(seconds)  # Event.wait returns True if *set*


class RequestCancelled(Exception):
    """Raised when a cancel_event fires mid-wait — caller (a scraper) sees
    this as an ordinary failed result, not a crash."""


class Fetcher:
    def __init__(
        self,
        min_interval: float = 2.0,
        jitter: float = 1.0,
        timeout: float = 60.0,
        retries: int = 3,
        proxy: Optional[str] = None,
        per_host: Optional[Dict[str, float]] = None,
        per_host_ceiling: float = DEFAULT_PER_HOST_CEILING,
        max_request_budget: float = DEFAULT_MAX_REQUEST_BUDGET,
    ) -> None:
        self.limiter = RateLimiter(min_interval, jitter, per_host, per_host_ceiling)
        self.retries = retries
        self.max_request_budget = max_request_budget
        #: set by the job runner so a cancelled job interrupts an in-flight
        #: wait/backoff within ~1s, without every scraper needing to pass
        #: cancel_event through its own fetch() signature explicitly.
        self.cancel_event: Optional[threading.Event] = None
        kwargs: Dict[str, Any] = dict(
            timeout=httpx.Timeout(timeout),
            follow_redirects=True,
            headers=BASE_HEADERS,
        )
        if proxy:
            # httpx>=0.26 uses `proxy`, older used `proxies`
            try:
                self.client = httpx.Client(proxy=proxy, **kwargs)
            except TypeError:  # pragma: no cover
                self.client = httpx.Client(proxies=proxy, **kwargs)
        else:
            self.client = httpx.Client(**kwargs)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        json_body: Any = None,
        content: Optional[bytes] = None,
        params: Optional[Dict[str, Any]] = None,
        expect_json: bool = True,
        cancel_event: Optional[threading.Event] = None,
    ) -> Any:
        host = urlparse(url).netloc
        last_exc: Optional[Exception] = None
        started = time.monotonic()
        cancel_event = cancel_event or self.cancel_event

        for attempt in range(1, self.retries + 1):
            if time.monotonic() - started > self.max_request_budget:
                log.warning(
                    "%s %s exceeded %.0fs request budget, giving up (attempt %d/%d)",
                    method, url, self.max_request_budget, attempt, self.retries,
                )
                break
            if not self.limiter.wait(host, cancel_event):
                raise RequestCancelled("cancelled while waiting for {} rate limit".format(host))
            try:
                resp = self.client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    content=content,
                    params=params,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt, self.retries, exc)
                if not _interruptible_sleep(min(2 ** attempt, 15), cancel_event):
                    raise RequestCancelled("cancelled after transport error on {}".format(host))
                continue

            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                if resp.status_code == 429:
                    floor = self.limiter.note_429(host)
                    backoff = min(15 * attempt, 60)
                else:
                    floor = None
                    backoff = min(2 ** attempt, 20)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    backoff = max(backoff, int(retry_after))
                # hard stop: never let a single request's own backoff blow the budget
                remaining_budget = self.max_request_budget - (time.monotonic() - started)
                backoff = max(0.0, min(backoff, remaining_budget))
                log.warning(
                    "%s %s -> HTTP %d, sleeping %.0fs (attempt %d/%d)%s",
                    method, url, resp.status_code, backoff, attempt, self.retries,
                    "" if floor is None else " — {} floor now {:.1f}s".format(host, floor),
                )
                if not _interruptible_sleep(backoff + random.uniform(0, 2), cancel_event):
                    raise RequestCancelled("cancelled during {} backoff".format(host))
                continue

            if resp.status_code < 400:
                self.limiter.note_success(host)
            resp.raise_for_status()
            return resp.json() if expect_json else resp.text

        raise last_exc if last_exc else RuntimeError("request failed: {} {}".format(method, url))

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Any:
        return self.request("POST", url, **kw)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
