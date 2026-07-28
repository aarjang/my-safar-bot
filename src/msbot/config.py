"""YAML config loading with sensible defaults for THR-IST."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import yaml

from .models import RouteSpec

DEFAULT_ROUTES: List[Dict[str, Any]] = [
    {
        "id": "THR-IST",
        "origin_city": "THR",
        "destination_city": "IST",
        "origin_airport": "IKA",
        "destination_airport": "IST",
        "tktfly_origin": "Tehran",
        "tktfly_destination": "Istanbul",
        "mysafar_origin": "IKA",
        "mysafar_destination": "ISTALL",
        "label_fa": "تهران به استانبول",
        "origin_label_fa": "تهران",
        "destination_label_fa": "استانبول",
    }
]

DEFAULTS: Dict[str, Any] = {
    "routes": DEFAULT_ROUTES,
    "sources": ["mysafar", "tktfly", "flytoday", "alibaba", "snapptrip"],
    "days": 30,
    "adults": 1,
    "both_directions": True,
    "rate_limit": {
        "min_interval": 2.5,
        "jitter": 1.5,
        "timeout": 60,
        "retries": 3,
        # flytoday's gateway starts returning 429 well before the others do
        "per_host": {"www.flytoday.ir": 12.0, "ws.alibaba.ir": 3.0},
        # a 429-stretched host's floor can never be asked to wait longer than
        # this, no matter how many consecutive 429s it's had (see http.py's
        # 2026-07-28 incident note — this is the fix for the 41-minute stall).
        "per_host_ceiling": 45.0,
        # a single request gives up (raises) past this much total wall-clock
        # time across all its retries/backoff, rather than compounding forever.
        "max_request_budget": 120.0,
        # a source that fails this many times in a row within one run trips a
        # circuit breaker: its remaining tasks are marked skipped instantly
        # (no network calls) instead of grinding through the same failure.
        "max_consecutive_errors": 3,
    },
    "storage": {"path": "data/history.sqlite"},
    "reports": {"dir": "reports"},
    "markup": {"base": "mysafar", "base_file": None},
    # the agency's own described workflow: tktfly is the primary benchmark,
    # we expect to sit ~10k Toman below it and 100-200k below everyone else.
    # See src/msbot/pattern.py for the full rationale.
    "expected_pattern": {
        "primary_source": "tktfly",
        "primary_diff_toman_min": 5000,
        "primary_diff_toman_max": 20000,
        "other_diff_toman_min": 80000,
        "other_diff_toman_max": 250000,
    },
    "proxy": None,
    "scraper_options": {"mrbilit": {"wait_ms": 25000, "headless": True}},
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    if not path:
        return dict(DEFAULTS)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _merge(DEFAULTS, data)


def route_specs(cfg: Dict[str, Any], only: Optional[List[str]] = None) -> List[RouteSpec]:
    specs = [RouteSpec(**r) for r in cfg["routes"]]
    if only:
        wanted = {r.upper() for r in only}
        specs = [s for s in specs if s.id.upper() in wanted]
        if not specs:
            raise KeyError("no configured route matches {}".format(sorted(wanted)))
    return specs
