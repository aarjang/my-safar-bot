"""FastAPI dashboard — a real, rate-limited front-end over the msbot scrapers.

Run with:

    python -m msbot.web

then open http://127.0.0.1:8765

The page is modeled on the client-provided mockup (RTL, Persian, Jalali
calendar, source chips, KPI cards, rate-diff table, competitor tab, coverage
tab) but every number on it comes from a real scrape through the same
rate-limited ``Fetcher`` the CLI uses — nothing here is randomly generated.

Origin/destination are not limited to a hand-maintained route list: the
``/api/airports/search`` endpoint proxies MySafar's own public airport search,
so any city MySafar sells can be picked and scraped ad hoc (see
``msbot.airports`` and ``jobs.adhoc_route`` for how that turns into a route).
"""
from __future__ import annotations

import io
import logging
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..airports import search_cities
from ..config import load_config, route_specs
from ..http import Fetcher
from ..markup import BaseFareTable
from ..pattern import CABIN_IDS, PatternConfig, PatternSettingsStore
from .. import regulated as regulatedmod
from ..report import comparison_frame, offers_frame, light_report_frame
from ..scrapers.base import get_scraper_classes
from ..storage import Storage
from .auth import BasicAuthMiddleware, resolve_credentials
from .jobs import SOURCE_HOST, JobManager

log = logging.getLogger("msbot.web")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="مای‌سفر — سامانهٔ مقایسه و پایش نرخ پروازها",
    # this dashboard has no unauthenticated surface at all — hide the
    # interactive API explorer from search engines/scanners; it's still
    # reachable (behind the same login) at /docs if you need it yourself.
    docs_url="/docs",
    redoc_url=None,
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


# CORS must run before auth (a cross-origin preflight OPTIONS request never
# carries credentials, so if BasicAuth were outermost it would 401 every
# preflight before CORSMiddleware got a chance to answer it) — middleware
# added earlier wraps outer, so this has to be added before BasicAuthMiddleware.
# Same-origin dashboard talking to its own API — no third party has any
# business calling this cross-origin, so it's locked down rather than "*".
_allowed_origins = [o.strip() for o in os.environ.get("DASHBOARD_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins or ["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

app.add_middleware(BasicAuthMiddleware, get_credentials=resolve_credentials)

_cfg: Dict[str, Any] = load_config()
_storage = Storage(_cfg["storage"]["path"])
_jobs = JobManager(_storage)
_pattern_store = PatternSettingsStore(
    str(Path(_cfg["storage"]["path"]).parent / "pattern_overrides.json")
)
# a separate, gentler-limited fetcher for autocomplete — small, frequent
# lookups, distinct from the scrape fetcher's per-host 429 backoff tuning.
_airport_fetcher = Fetcher(min_interval=0.3, jitter=0.2, timeout=15, retries=2)


def _browser_available() -> bool:
    """Whether a Playwright-driven scraper (mrbilit) can actually run in this
    process — checked at boot so the dashboard can grey the checkbox out
    instead of letting the user enable a source that's guaranteed to fail 3
    times and get circuit-broken on every single run (which is exactly what
    used to happen: the ``playwright`` package can be installed with no
    browser binary behind it, e.g. a deployment that skips
    ``playwright install`` — importing the package alone isn't proof it can
    launch anything).
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    from ..scrapers.mrbilit import local_chrome_path
    return local_chrome_path() is not None


_BROWSER_AVAILABLE = _browser_available()

#: display labels for /api/meta — keyed the same as regulated.DEFAULT_REGULATED_ALIASES
REGULATED_LABELS_FA = {
    "mahan": "ماهان",
    "saha": "ساها",
    "caspian": "کاسپین",
    "iran_airtour": "ایران ایرتور",
    "sepehran": "سپهران",
}


def _current_pattern_cfg() -> PatternConfig:
    return _pattern_store.load(_cfg.get("expected_pattern"))


class CityRef(BaseModel):
    city_code: str
    city_fa: str = ""
    english_name: Optional[str] = None
    mysafar_code: Optional[str] = None
    country_fa: Optional[str] = None


class ScrapeRequest(BaseModel):
    route_id: Optional[str] = None
    origin: Optional[CityRef] = None
    destination: Optional[CityRef] = None
    start_date: str
    end_date: str
    sources: List[str] = Field(default_factory=lambda: list(_cfg["sources"]))
    adults: int = 1
    both_directions: bool = True
    base_strategy: str = "mysafar"


class CabinBand(BaseModel):
    primary_diff_toman_min: int
    primary_diff_toman_max: int
    other_diff_toman_min: int
    other_diff_toman_max: int


class PatternUpdate(BaseModel):
    primary_source: Optional[str] = None
    default: Optional[CabinBand] = None
    cabins: Optional[Dict[str, CabinBand]] = None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    routes = route_specs(_cfg)
    scraper_classes = get_scraper_classes()
    today = date.today()
    competitor_ids = [c.name for c in scraper_classes if c.name != "mysafar"]
    return {
        "routes": [
            {
                "id": r.id,
                "origin_city": r.origin_city,
                "destination_city": r.destination_city,
                "origin_label": r.origin_label_fa or r.origin_city,
                "destination_label": r.destination_label_fa or r.destination_city,
                "label_fa": r.label_fa or "{} ↔ {}".format(r.origin_city, r.destination_city),
            }
            for r in routes
        ],
        "sources": [
            {
                "id": c.name,
                "label": c.label,
                "requires_browser": c.requires_browser,
                "available": (not c.requires_browser) or _BROWSER_AVAILABLE,
                "host": SOURCE_HOST.get(c.name),
                "default_enabled": c.name in _cfg["sources"] and ((not c.requires_browser) or _BROWSER_AVAILABLE),
            }
            for c in scraper_classes
        ],
        "competitor_ids": competitor_ids,
        "cabin_ids": [c for c in CABIN_IDS if c != "unknown"],
        "rate_limit": {
            "min_interval": _cfg["rate_limit"]["min_interval"],
            "per_host": _cfg["rate_limit"].get("per_host", {}),
        },
        "expected_pattern": _current_pattern_cfg().to_dict(),
        "regulated_airlines": {
            "enabled": bool((_cfg.get("regulated_airlines") or {}).get("enabled", True)),
            "names_fa": [REGULATED_LABELS_FA[k] for k in regulatedmod.DEFAULT_REGULATED_ALIASES],
        },
        "default_start": (today + timedelta(days=1)).isoformat(),
        "default_days": min(_cfg.get("days", 14), 14),
    }


@app.get("/api/airports/search")
def airports_search(q: str, international: Optional[bool] = None) -> Dict[str, Any]:
    try:
        matches = search_cities(_airport_fetcher, q, international=international)
    except Exception as exc:
        log.warning("airport search failed for %r: %s", q, exc)
        raise HTTPException(status_code=502, detail="جستجوی فرودگاه ناموفق بود")
    return {"matches": [m.to_dict() for m in matches]}


@app.get("/api/pattern-config")
def get_pattern_config() -> Dict[str, Any]:
    return _current_pattern_cfg().to_dict()


@app.post("/api/pattern-config")
def update_pattern_config(update: PatternUpdate) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    if update.primary_source:
        overrides["primary_source"] = update.primary_source
    if update.default:
        overrides["default"] = update.default.model_dump()
    if update.cabins:
        overrides["cabins"] = {k: v.model_dump() for k, v in update.cabins.items()}
    _pattern_store.save(overrides)
    return _current_pattern_cfg().to_dict()


@app.post("/api/pattern-config/reset")
def reset_pattern_config() -> Dict[str, Any]:
    _pattern_store.reset()
    return _current_pattern_cfg().to_dict()


@app.get("/api/last-job")
def last_job(route_id: str) -> Dict[str, Any]:
    job = _jobs.last_job_for(route_id)
    if not job:
        return {"job": None}
    return {"job": job.to_public()}


@app.post("/api/scrape")
def start_scrape(req: ScrapeRequest) -> Dict[str, Any]:
    if not req.route_id and not (req.origin and req.destination):
        raise HTTPException(status_code=400, detail="route_id یا origin+destination لازم است")
    sources = req.sources or list(_cfg["sources"])
    if not _BROWSER_AVAILABLE:
        browser_only = {c.name for c in get_scraper_classes() if c.requires_browser}
        sources = [s for s in sources if s not in browser_only]
    try:
        job = _jobs.start(
            _cfg,
            start_date=req.start_date,
            end_date=req.end_date,
            sources=sources,
            route_ids=[req.route_id] if req.route_id else None,
            origin=req.origin.model_dump() if req.origin else None,
            destination=req.destination.model_dump() if req.destination else None,
            adults=req.adults,
            both_directions=req.both_directions,
            base_strategy=req.base_strategy,
            base_table=None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> Dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> Dict[str, Any]:
    ok = _jobs.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    return {"cancelled": True}


@app.get("/api/comparison")
def get_comparison(job_id: str) -> Dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # served from whatever's been collected so far — job.results fills in
    # incrementally, so a still-running (or slow/circuit-broken) source
    # doesn't block seeing the sources that already answered.
    df = _jobs.comparison_for(job, _current_pattern_cfg())
    rows = df.where(df.notnull(), None).to_dict(orient="records") if not df.empty else []
    return {"status": job.status, "partial": job.status not in ("done", "cancelled"), "rows": rows}


@app.get("/api/csv")
def get_csv(job_id: str, kind: str = Query("comparison", pattern="^(comparison|offers|light)$")):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    offers = [o for r in job.results for o in r.offers]
    if kind == "comparison":
        df = comparison_frame(
            offers, job.base_strategy, job.base_table, pattern_cfg=_current_pattern_cfg(),
            regulated_cfg=job.cfg.get("regulated_airlines"),
        )
    elif kind == "light":
        df = light_report_frame(
            offers, job.base_strategy, job.base_table, pattern_cfg=_current_pattern_cfg(),
            regulated_cfg=job.cfg.get("regulated_airlines"),
        )
    else:
        df = offers_frame(offers)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel opens Persian text correctly
    filename = "{}_{}.csv".format(kind, job.id)
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="{}"'.format(filename)},
    )


@app.get("/api/history")
def get_history(route: str, search_date: str, source: Optional[str] = None) -> Dict[str, Any]:
    rows = _storage.price_history(route, search_date, source)
    return {
        "rows": [
            {
                "scraped_at": r["scraped_at"],
                "source": r["source"],
                "cabin": r["cabin"],
                "min_price_toman": r["min_price"] // 10,
            }
            for r in rows
        ]
    }
