"""In-memory background job runner for the dashboard.

A "job" is one scrape run (N routes x N days x N sources). Jobs run in a
background thread so the HTTP request that starts them returns immediately;
the dashboard polls ``GET /api/jobs/{id}`` for live progress, exactly like the
progress bar in the mockup — except the bar now tracks a real, rate-limited
scrape that can take anywhere from 10s to several minutes, not a fake 650ms
timeout.

This is intentionally a single-process, in-memory registry (a dict + lock) —
fine for one operator running this on their own machine. If it ever needs to
survive a server restart or run across multiple workers, swap this for a real
queue (e.g. RQ/Celery) without touching the scraping code itself.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from ..config import route_specs
from ..markup import BaseFareTable
from ..models import RouteSpec, ScrapeResult
from ..orchestrator import CIRCUIT_BREAKER_PREFIX, build_fetcher, run_scrapers, total_tasks
from ..pattern import PatternConfig
from ..report import comparison_frame
from ..storage import Storage

#: host each scraper actually talks to, so we can surface "this site is
#: throttling us" without every scraper having to report it itself.
SOURCE_HOST = {
    "flytoday": "www.flytoday.ir",
    "alibaba": "ws.alibaba.ir",
    "snapptrip": "ift.snapptrip.com",
    "mysafar": "api.mysafar.com",
    "tktfly": "tktfly.ir",
    "mrbilit": "flight.atighgasht.com",
}


@dataclass
class SourceProgress:
    status: str = "pending"  # pending | running | ok | rate_limited | error | skipped
    done: int = 0
    total: int = 0
    offers: int = 0
    last_error: Optional[str] = None


@dataclass
class Job:
    id: str
    cfg: Dict[str, Any]
    routes: List[RouteSpec]
    days: List[str]
    sources: List[str]
    adults: int
    base_strategy: str
    base_table: Optional[BaseFareTable]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat(timespec="seconds") + "Z")
    status: str = "pending"  # pending | running | done | error | cancelled
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    results: List[ScrapeResult] = field(default_factory=list)
    per_source: Dict[str, SourceProgress] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: Optional[threading.Thread] = None
    run_id: Optional[int] = None

    def total(self) -> int:
        return total_tasks(self.routes, self.days, self.sources)

    def done_count(self) -> int:
        with self.lock:
            return sum(sp.done for sp in self.per_source.values())

    def to_public(self) -> Dict[str, Any]:
        with self.lock:
            per_source = {k: vars(v) for k, v in self.per_source.items()}
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "routes": [r.id for r in self.routes],
            "days": {"start": self.days[0], "end": self.days[-1], "count": len(self.days)},
            "sources": self.sources,
            "total": self.total(),
            "done": self.done_count(),
            "per_source": per_source,
            "error": self.error,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1) if self.started_at else None,
        }


class JobManager:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        # last successful job per route -> used to serve "current" comparison
        # data instantly on page load, before the user clicks "تحلیل نرخ‌ها".
        self._last_done_by_routes: Dict[str, str] = {}

    def start(
        self,
        cfg: Dict[str, Any],
        start_date: str,
        end_date: str,
        sources: List[str],
        route_ids: Optional[List[str]] = None,
        origin: Optional[Dict[str, Any]] = None,
        destination: Optional[Dict[str, Any]] = None,
        adults: int = 1,
        both_directions: bool = True,
        base_strategy: str = "mysafar",
        base_table: Optional[BaseFareTable] = None,
    ) -> Job:
        """Either ``route_ids`` (a pinned entry from config.yaml) or
        ``origin``+``destination`` (an ad-hoc pick from the live airport
        search) must be given. The ad-hoc path builds a :class:`RouteSpec` on
        the fly — see ``adhoc_route`` — so *any* city MySafar's own airport
        search knows about can be scraped without a config change.
        """
        if origin and destination:
            routes = [adhoc_route(origin, destination)]
        else:
            routes = route_specs(cfg, route_ids)
        if both_directions:
            routes = routes + [r.reversed() for r in routes]
        days = _date_span(start_date, end_date)

        job = Job(
            id=uuid.uuid4().hex[:12],
            cfg=cfg,
            routes=routes,
            days=days,
            sources=sources,
            adults=adults,
            base_strategy=base_strategy,
            base_table=base_table,
        )
        for src in sources:
            job.per_source[src] = SourceProgress(total=len(routes) * len(days))

        with self._lock:
            self._jobs[job.id] = job

        job.thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.thread.start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.cancel_event.set()
        return True

    def last_job_for(self, route_key: str) -> Optional[Job]:
        job_id = self._last_done_by_routes.get(route_key)
        return self._jobs.get(job_id) if job_id else None

    # -- internals ------------------------------------------------------

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started_at = time.time()
        fetcher = build_fetcher(job.cfg)

        def on_progress(res: ScrapeResult) -> None:
            with job.lock:
                job.results.append(res)  # populated incrementally, not just at the end —
                # lets /api/comparison serve whatever's collected so far even
                # while a slow source is still running (or has been skipped
                # by the circuit breaker but the others are still going).
                sp = job.per_source.setdefault(res.source, SourceProgress(total=job.total()))
                sp.done += 1
                sp.offers += len(res.offers)
                host = SOURCE_HOST.get(res.source)
                throttled = bool(
                    host and fetcher.limiter.per_host.get(host, 0)
                    > job.cfg["rate_limit"].get("per_host", {}).get(host, job.cfg["rate_limit"]["min_interval"])
                )
                if not res.ok and res.error and res.error.startswith(CIRCUIT_BREAKER_PREFIX):
                    sp.status = "skipped"
                    sp.last_error = res.error
                elif not res.ok:
                    sp.status = "error"
                    sp.last_error = res.error
                elif throttled:
                    sp.status = "rate_limited"
                else:
                    sp.status = "running" if sp.done < sp.total else "ok"
            if job.run_id is not None:
                self.storage.log_result(job.run_id, res)

        job.run_id = self.storage.start_run(
            note="dashboard job {} routes={} days={}".format(job.id, [r.id for r in job.routes], len(job.days))
        )
        run_id = job.run_id

        try:
            results = run_scrapers(
                job.cfg,
                job.routes,
                job.days,
                job.sources,
                adults=job.adults,
                workers=len(job.sources) or 1,
                fetcher=fetcher,
                on_progress=on_progress,
                cancel_event=job.cancel_event,
            )
            # job.results was already filled incrementally by on_progress;
            # `results` here is the same list run_scrapers also returns.
            offers = [o for r in results for o in r.offers]
            self.storage.save_offers(run_id, offers)

            with job.lock:
                for src, sp in job.per_source.items():
                    if sp.status not in ("error", "skipped"):
                        sp.status = "ok"

            job.status = "cancelled" if job.cancel_event.is_set() else "done"
            for r in job.routes:
                self._last_done_by_routes[r.id] = job.id
                base = r.id.split("-")
                self._last_done_by_routes["{}-{}".format(base[-1], base[0])] = job.id
        except Exception as exc:  # pragma: no cover
            job.status = "error"
            job.error = "{}: {}".format(type(exc).__name__, exc)
        finally:
            job.finished_at = time.time()
            fetcher.close()

    def comparison_for(self, job: Job, pattern_cfg: PatternConfig) -> Any:
        """Recompute the comparison table from a job's already-scraped offers.

        ``pattern_cfg`` is passed in fresh (from :class:`PatternSettingsStore`)
        rather than read off the job — thresholds are a pure post-processing
        step over data that's already sitting in memory, so editing them in
        the dashboard re-flags rows instantly with no re-scrape.
        """
        with job.lock:
            results_snapshot = list(job.results)
        return comparison_frame(
            [o for r in results_snapshot for o in r.offers],
            job.base_strategy, job.base_table, pattern_cfg=pattern_cfg,
            regulated_cfg=job.cfg.get("regulated_airlines"),
        )


def adhoc_route(origin: Dict[str, Any], destination: Dict[str, Any]) -> RouteSpec:
    """Build a :class:`RouteSpec` from two airport-search results (see
    ``msbot.airports.CityMatch.to_dict``) instead of a config.yaml entry.

    tktfly needs ``english_name`` to build its URL; if a city search didn't
    turn one up, tktfly will simply fail for that route (surfaced honestly as
    a per-source error, not silently skipped) rather than guessing a name.
    """
    o_code, d_code = origin["city_code"], destination["city_code"]
    return RouteSpec(
        id="{}-{}".format(o_code, d_code),
        origin_city=o_code,
        destination_city=d_code,
        origin_airport=o_code,
        destination_airport=d_code,
        tktfly_origin=origin.get("english_name"),
        tktfly_destination=destination.get("english_name"),
        mysafar_origin=origin.get("mysafar_code") or o_code,
        mysafar_destination=destination.get("mysafar_code") or d_code,
        label_fa="{} به {}".format(origin.get("city_fa", o_code), destination.get("city_fa", d_code)),
        origin_label_fa=origin.get("city_fa"),
        destination_label_fa=destination.get("city_fa"),
    )


def _date_span(start: str, end: str) -> List[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end, "%Y-%m-%d").date()
    if d1 < d0:
        d0, d1 = d1, d0
    n = (d1 - d0).days + 1
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]
