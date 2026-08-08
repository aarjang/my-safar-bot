"""CLI entry point.

    python -m msbot scrape --route THR-IST --days 1
    python -m msbot scrape --days 30 --both-directions --base file --base-file data/base_fares.csv
    python -m msbot probe                      # one-shot connectivity check of every source
    python -m msbot base-template --days 30    # emit the net-fare CSV for the agency to fill
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from .config import load_config, route_specs
from .markup import BaseFareTable, write_base_fare_template
from .models import FlightOffer, RouteSpec, ScrapeResult
from .orchestrator import run_scrapers
from .pattern import PatternConfig
from .report import comparison_frame, write_reports
from .scrapers.base import all_scraper_names
from .storage import Storage

log = logging.getLogger("msbot")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _days(start: str, count: int) -> List[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d").date() if start else date.today() + timedelta(days=1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(count)]


def _run_scrapers(
    cfg: Dict[str, Any],
    routes: List[RouteSpec],
    days: List[str],
    sources: List[str],
    adults: int,
    workers: int,
) -> List[ScrapeResult]:
    def on_progress(res: ScrapeResult) -> None:
        log.info(
            "%-10s %s %s : %s offer(s) in %.1fs%s",
            res.source, res.route, res.search_date, len(res.offers), res.elapsed_s,
            "" if res.ok else "  ERROR: {}".format(res.error),
        )

    return run_scrapers(cfg, routes, days, sources, adults, workers, on_progress=on_progress)


def cmd_scrape(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    routes = route_specs(cfg, args.route)
    if args.both_directions:
        routes = routes + [r.reversed() for r in routes]
    days = _days(args.start, args.days or cfg["days"])
    sources = args.sources or cfg["sources"]
    if args.with_browser:
        sources = list(dict.fromkeys(sources + ["mrbilit"]))

    log.info(
        "routes=%s days=%s (%s … %s) sources=%s",
        [r.id for r in routes], len(days), days[0], days[-1], sources,
    )

    results = _run_scrapers(cfg, routes, days, sources, args.adults, args.workers)
    offers: List[FlightOffer] = [o for r in results for o in r.offers]

    store = Storage(cfg["storage"]["path"])
    run_id = store.start_run(note="routes={} days={}".format([r.id for r in routes], len(days)))
    for r in results:
        store.log_result(run_id, r)
    saved = store.save_offers(run_id, offers)
    log.info("saved %d offers to %s (run #%d)", saved, cfg["storage"]["path"], run_id)

    base_table = None
    base_strategy = args.base or cfg["markup"]["base"]
    base_file = args.base_file or cfg["markup"].get("base_file")
    if base_strategy == "file":
        if not base_file:
            log.warning("--base file needs --base-file; falling back to mysafar public price")
            base_strategy = "mysafar"
        else:
            base_table = BaseFareTable.from_csv(base_file)

    pattern_cfg = PatternConfig.from_cfg(cfg.get("expected_pattern"))
    regulated_cfg = cfg.get("regulated_airlines")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = write_reports(offers, cfg["reports"]["dir"], stamp, base_strategy, base_table, pattern_cfg, regulated_cfg)
    for name, path in paths.items():
        log.info("report %-15s %s", name, path)

    comp = comparison_frame(offers, base_strategy, base_table, pattern_cfg=pattern_cfg, regulated_cfg=regulated_cfg)
    if not comp.empty:
        cols = [c for c in comp.columns if c.endswith("_toman") or c in ("route", "date", "departure", "airline", "cabin", "fare_type")]
        print()
        print(comp[cols].to_string(index=False))

        anomalies = comp[comp["pattern_ok"] == False]  # noqa: E712 (pandas nullable bool)
        if not anomalies.empty:
            eco = pattern_cfg.for_cabin("economy")
            print()
            print("{} flight(s) fall outside the usual pattern for their cabin ({} ~{:,}-{:,}T, others ~{:,}-{:,}T below us, for economy) — likely need a markup look:".format(
                len(anomalies), pattern_cfg.primary_source, eco.primary_min, eco.primary_max,
                eco.other_min, eco.other_max,
            ))
            print(anomalies[["route", "date", "departure", "airline", "cabin", "fare_type", "mysafar_toman", "primary_diff_toman", "anomaly_reason"]].to_string(index=False))

    failures = [r for r in results if not r.ok]
    if failures:
        log.warning("%d/%d scrapes failed:", len(failures), len(results))
        for f in failures[:20]:
            log.warning("  %s %s %s -> %s", f.source, f.route, f.search_date, f.error)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Hit every source once for tomorrow — quickest way to see what still works."""
    cfg = load_config(args.config)
    routes = route_specs(cfg, args.route)[:1]
    day = args.start or (date.today() + timedelta(days=args.offset)).isoformat()
    sources = args.sources or all_scraper_names()
    if not args.with_browser:
        sources = [s for s in sources if s != "mrbilit"]

    results = _run_scrapers(cfg, routes, [day], sources, 1, args.workers)
    print("\n{:<12} {:>7} {:>8} {:>16} {:>16}  {}".format(
        "source", "offers", "time", "cheapest(Toman)", "cabins", "status"))
    print("-" * 96)
    for r in sorted(results, key=lambda x: x.source):
        cheapest = r.cheapest()
        cabins = ",".join(sorted({o.cabin for o in r.offers})) or "-"
        print("{:<12} {:>7} {:>7.1f}s {:>16} {:>16}  {}".format(
            r.source,
            len(r.offers),
            r.elapsed_s,
            "{:,}".format(cheapest.price_toman) if cheapest else "-",
            cabins,
            "OK" if r.ok else (r.error or "")[:40],
        ))
    return 0 if all(r.ok for r in results) else 1


def cmd_base_template(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    routes = route_specs(cfg, args.route)
    ids = [r.id for r in routes]
    if args.both_directions:
        ids += [r.reversed().id for r in routes]
    days = _days(args.start, args.days or cfg["days"])
    out = args.out or "data/base_fares.csv"
    write_base_fare_template(out, ids, days)
    print("wrote {} ({} rows to fill)".format(out, len(ids) * len(days) * 2))
    return 0


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser("msbot", description="Competitor flight-price / markup monitor")
    p.add_argument("-c", "--config", help="YAML config path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--route", action="append", help="route id, repeatable (default: all configured)")
        sp.add_argument("--sources", nargs="+", help="subset of {}".format(all_scraper_names()))
        sp.add_argument("--start", help="first departure date, YYYY-MM-DD (default: tomorrow)")
        sp.add_argument("--workers", type=int, default=5, help="parallel sources (default 5)")
        sp.add_argument("--with-browser", action="store_true", help="include Playwright sources (mrbilit)")

    sp = sub.add_parser("scrape", help="full run + reports")
    common(sp)
    sp.add_argument("--days", type=int, help="number of departure dates (default from config)")
    sp.add_argument("--adults", type=int, default=1)
    sp.add_argument("--both-directions", action="store_true", help="also scrape the return leg")
    sp.add_argument("--base", choices=["file", "mysafar", "min"], help="base-fare strategy")
    sp.add_argument("--base-file", help="CSV of real net fares (with --base file)")
    sp.set_defaults(func=cmd_scrape)

    sp = sub.add_parser("probe", help="one date, every source — connectivity check")
    common(sp)
    sp.add_argument("--offset", type=int, default=7, help="days from today (default 7)")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("base-template", help="emit net-fare CSV template")
    common(sp)
    sp.add_argument("--days", type=int)
    sp.add_argument("--both-directions", action="store_true")
    sp.add_argument("--out")
    sp.set_defaults(func=cmd_base_template)

    args = p.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
