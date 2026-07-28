"""The agency's actual workflow, encoded.

Straight from the client (Mohammadi / My Safar), describing how they check
markup by hand today::

    اولویت اصلی همیشه سایت آماده سفره، چون معمولاً قیمت‌هاش از همه به نرخ‌های
    ما نزدیک‌تره. اکثر مواقع هم نرخ مای سفر حدود ۱۰ هزار تومان از آماده سفر
    ارزون‌تره و اختلاف قیمت ما با بقیه سایت‌ها معمولاً بین ۱۰۰ تا ۲۰۰ هزار
    تومانه.

Translated into a rule:

* **tktfly (آماده سفر) is the primary benchmark** — not just one of several
  equally-weighted competitors.
* MySafar is expected to sit **~10,000 Toman below tktfly**.
* MySafar is expected to sit **100,000–200,000 Toman below every other site**.

The point of automating this isn't to print a wall of prices — it's to do
exactly what the agent does by hand: scan every flight and flag the ones that
fall *outside* that expected band, because those are the ones that need a
markup correction. Everything in range needs no attention.

**Per-cabin bands.** The client's numbers describe economy-class shopping
behavior. A fixed Toman gap does not scale: a business or long-haul fare can
be 2-4x an economy fare on the same route, so the same absolute band either
never triggers (too loose) or triggers on noise (too tight — this is exactly
what happened to business-cabin rows in the first version of this tool, which
applied the economy band everywhere). So thresholds are keyed by cabin, with
sane multiplier-based defaults for business/premium that the agency should
tune — see ``DEFAULT_CABIN_BANDS`` below, all overridable in `config.yaml`
under ``expected_pattern.cabins`` or live from the dashboard's settings panel
(``PatternSettingsStore``), no restart required.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PRIMARY_SOURCE = "tktfly"

#: (primary_min, primary_max, other_min, other_max) in Toman, per cabin.
#: Only "economy" comes from the client verbatim; business/premium are scaled
#: defaults (fares run ~1.5-4x economy on the same route) — clearly a
#: starting point to be tuned, not a validated number.
DEFAULT_CABIN_BANDS: Dict[str, Tuple[int, int, int, int]] = {
    "economy": (5_000, 20_000, 80_000, 250_000),
    "premium_economy": (8_000, 35_000, 120_000, 400_000),
    "business": (10_000, 60_000, 150_000, 800_000),
    "first": (15_000, 100_000, 200_000, 1_200_000),
    "unknown": (5_000, 20_000, 80_000, 250_000),
}

CABIN_IDS = ["economy", "premium_economy", "business", "first", "unknown"]


@dataclass
class CabinRange:
    primary_min: int
    primary_max: int
    other_min: int
    other_max: int

    def to_dict(self) -> Dict[str, int]:
        return {
            "primary_diff_toman_min": self.primary_min,
            "primary_diff_toman_max": self.primary_max,
            "other_diff_toman_min": self.other_min,
            "other_diff_toman_max": self.other_max,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any], fallback: "CabinRange") -> "CabinRange":
        return cls(
            primary_min=int(d.get("primary_diff_toman_min", fallback.primary_min)),
            primary_max=int(d.get("primary_diff_toman_max", fallback.primary_max)),
            other_min=int(d.get("other_diff_toman_min", fallback.other_min)),
            other_max=int(d.get("other_diff_toman_max", fallback.other_max)),
        )


@dataclass
class PatternConfig:
    primary_source: str = PRIMARY_SOURCE
    #: default band, used for any cabin without its own explicit entry
    default: CabinRange = field(
        default_factory=lambda: CabinRange(*DEFAULT_CABIN_BANDS["economy"])
    )
    per_cabin: Dict[str, CabinRange] = field(default_factory=dict)

    def for_cabin(self, cabin: str) -> CabinRange:
        return self.per_cabin.get(cabin, self.default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_source": self.primary_source,
            "default": self.default.to_dict(),
            "cabins": {c: r.to_dict() for c, r in self.per_cabin.items()},
        }

    @classmethod
    def from_cfg(cls, cfg: Optional[Dict[str, Any]]) -> "PatternConfig":
        cfg = cfg or {}
        primary_source = str(cfg.get("primary_source", PRIMARY_SOURCE))

        # backward-compatible: flat keys at the top level become "default"
        default = CabinRange.from_dict(
            cfg.get("default", cfg), CabinRange(*DEFAULT_CABIN_BANDS["economy"])
        )

        per_cabin: Dict[str, CabinRange] = {}
        cabins_cfg = cfg.get("cabins") or {}
        for cabin_id in CABIN_IDS:
            builtin_default = CabinRange(*DEFAULT_CABIN_BANDS.get(cabin_id, DEFAULT_CABIN_BANDS["economy"]))
            if cabin_id in cabins_cfg:
                per_cabin[cabin_id] = CabinRange.from_dict(cabins_cfg[cabin_id], builtin_default)
            elif cabin_id not in ("economy",):
                # no explicit config: still apply the cabin-scaled builtin
                # default rather than silently falling back to economy's band
                per_cabin[cabin_id] = builtin_default
        return cls(primary_source=primary_source, default=default, per_cabin=per_cabin)


class PatternSettingsStore:
    """Runtime-editable overrides for :class:`PatternConfig`, persisted to a
    small JSON file so a threshold tweak in the dashboard survives a restart
    without touching config.yaml. The scrape/rate-limit config stays static
    (it governs outbound requests); this governs an in-memory recompute over
    already-scraped data, so changes apply instantly with no re-scrape.
    """

    def __init__(self, path: str = "data/pattern_overrides.json") -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self, base_cfg: Optional[Dict[str, Any]]) -> PatternConfig:
        merged = dict(base_cfg or {})
        with self._lock:
            if self.path.exists():
                try:
                    overrides = json.loads(self.path.read_text(encoding="utf-8"))
                    merged = _deep_merge(merged, overrides)
                except (json.JSONDecodeError, OSError):
                    pass
        return PatternConfig.from_cfg(merged)

    def save(self, overrides: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def evaluate(
    row: Dict[str, object],
    competitor_ids: List[str],
    pattern: PatternConfig,
) -> Dict[str, object]:
    """Given one comparison row (already has ``mysafar_toman``, ``cabin`` and
    ``<source>_toman`` keys), compute the primary-benchmark diff and flag
    whether the row sits inside the agency's expected band **for that row's
    cabin**.

    Diff convention throughout: ``competitor_toman - mysafar_toman``. Positive
    means we're cheaper (the normal, expected case); too small, or negative,
    means we've drifted too close to (or above) a competitor and the markup
    on that flight probably needs raising.
    """
    us = row.get("mysafar_toman")
    band = pattern.for_cabin(str(row.get("cabin") or "unknown"))
    out: Dict[str, object] = {
        "primary_source": pattern.primary_source,
        "primary_diff_toman": None,
        "primary_in_range": None,
        "other_in_range": None,
        "worst_other_source": None,
        "worst_other_diff_toman": None,
        "pattern_ok": None,
        "anomaly_reason": None,
        "band_primary_min": band.primary_min,
        "band_primary_max": band.primary_max,
        "band_other_min": band.other_min,
        "band_other_max": band.other_max,
    }
    if us is None:
        return out

    reasons: List[str] = []

    primary_price = row.get("{}_toman".format(pattern.primary_source))
    if primary_price is not None:
        diff = primary_price - us
        out["primary_diff_toman"] = diff
        in_range = band.primary_min <= diff <= band.primary_max
        out["primary_in_range"] = in_range
        if not in_range:
            direction = "کمتر از حد انتظار" if diff < band.primary_min else "بیشتر از حد انتظار"
            reasons.append(
                "اختلاف با آماده‌سفر {} ({:,} تومان به‌جای {:,}–{:,})".format(
                    direction, diff, band.primary_min, band.primary_max
                )
            )

    others = [c for c in competitor_ids if c != pattern.primary_source]
    other_flags: List[bool] = []
    worst_id: Optional[str] = None
    worst_diff: Optional[int] = None
    for src in others:
        price = row.get("{}_toman".format(src))
        if price is None:
            continue
        diff = price - us
        in_range = band.other_min <= diff <= band.other_max
        other_flags.append(in_range)
        # "worst" = the one furthest outside the expected band (most likely to need action)
        if not in_range:
            shortfall = (band.other_min - diff) if diff < band.other_min else (diff - band.other_max)
            if worst_diff is None or shortfall > worst_diff:
                worst_diff = shortfall
                worst_id = src

    if other_flags:
        out["other_in_range"] = all(other_flags)
        if worst_id is not None:
            out["worst_other_source"] = worst_id
            out["worst_other_diff_toman"] = row.get("{}_toman".format(worst_id)) - us
            reasons.append(
                "اختلاف با {} خارج از الگو ({:,} تومان به‌جای {:,}–{:,})".format(
                    worst_id, out["worst_other_diff_toman"], band.other_min, band.other_max
                )
            )

    flags = [f for f in (out["primary_in_range"], out["other_in_range"]) if f is not None]
    out["pattern_ok"] = all(flags) if flags else None
    out["anomaly_reason"] = " · ".join(reasons) if reasons else None
    return out
