"""Reporting: raw offer dump + the comparison matrix the client actually reads."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .jalali import to_jalali_slash, weekday_fa
from .markup import BaseFareTable, markup_percent, resolve_base_fare
from .models import FlightOffer
from .pattern import PatternConfig
from . import pattern as patternmod
from . import regulated as regulatedmod

OUR_SOURCE = "mysafar"


def offers_frame(offers: List[FlightOffer]) -> pd.DataFrame:
    if not offers:
        return pd.DataFrame()
    df = pd.DataFrame([o.to_row() for o in offers])
    df["price_toman"] = df["price_rial"] // 10
    return df.sort_values(["route", "search_date", "cabin", "price_rial"])


def comparison_frame(
    offers: List[FlightOffer],
    base_strategy: str = "mysafar",
    base_table: Optional[BaseFareTable] = None,
    cabins: Optional[List[str]] = None,
    pattern_cfg: Optional[PatternConfig] = None,
    regulated_cfg: Optional[Dict[str, object]] = None,
) -> pd.DataFrame:
    """One row per (route, date, cabin): our price vs each competitor + markup.

    Offers for airline-regulated-fare carriers (Mahan/Saha/Caspian/Iran
    Airtour/Sepehran by default — see ``msbot.regulated``) are dropped before
    any of this: their price is set by the airline's own circular, not agency
    markup, so a competitor showing a lower number there isn't a pricing
    signal — including them would produce false "lower your price" flags.
    They still appear in ``offers_frame``/the raw CSV, just not here.
    """
    offers, _regulated_offers = regulatedmod.split_regulated(offers, regulated_cfg)
    if not offers:
        return pd.DataFrame()

    sources = sorted({o.source for o in offers})
    competitors = [s for s in sources if s != OUR_SOURCE]
    pattern_cfg = pattern_cfg or PatternConfig()
    # the primary benchmark (tktfly) leads the competitor columns — everything
    # else follows alphabetically, matching how the agency actually reads this:
    # check tktfly first, then the rest.
    if pattern_cfg.primary_source in competitors:
        competitors = [pattern_cfg.primary_source] + [
            s for s in competitors if s != pattern_cfg.primary_source
        ]
    rows: List[Dict[str, object]] = []

    keys = sorted({(o.route, o.search_date, o.cabin) for o in offers})
    for route, day, cabin in keys:
        if cabins and cabin not in cabins:
            continue
        cell = [o for o in offers if o.route == route and o.search_date == day and o.cabin == cabin]
        by_source: Dict[str, List[FlightOffer]] = {}
        for o in cell:
            by_source.setdefault(o.source, []).append(o)

        base_fare, base_label = resolve_base_fare(
            base_strategy, by_source, route, day, cabin, base_table
        )

        row: Dict[str, object] = {
            "route": route,
            "date": day,
            "date_jalali": to_jalali_slash(day),
            "weekday": weekday_fa(day),
            "cabin": cabin,
            "base_fare_toman": base_fare // 10 if base_fare else None,
            "base_fare_source": base_label,
        }

        ours = by_source.get(OUR_SOURCE) or []
        our_min = min((o.price_rial for o in ours), default=None)
        row["mysafar_toman"] = our_min // 10 if our_min else None
        row["mysafar_airline"] = (
            min(ours, key=lambda o: o.price_rial).airline_name if ours else None
        )
        row["mysafar_markup_pct"] = (
            markup_percent(our_min, base_fare) if our_min and base_fare else None
        )

        for src in competitors:
            group = by_source.get(src) or []
            price = min((o.price_rial for o in group), default=None)
            row["{}_toman".format(src)] = price // 10 if price else None
            row["{}_markup_pct".format(src)] = (
                markup_percent(price, base_fare) if price and base_fare else None
            )
            row["{}_diff_toman".format(src)] = (
                (price - our_min) // 10 if price and our_min else None
            )
            row["{}_n".format(src)] = len(group)

        comp_prices = [
            p for p in (
                min((o.price_rial for o in (by_source.get(s) or [])), default=None)
                for s in competitors
            ) if p
        ]
        row["market_min_toman"] = min(comp_prices) // 10 if comp_prices else None
        row["we_are_cheapest"] = (
            bool(our_min and comp_prices and our_min <= min(comp_prices)) if our_min else None
        )
        row.update(patternmod.evaluate(row, competitors, pattern_cfg))
        rows.append(row)

    df = pd.DataFrame(rows)
    return df.sort_values(["route", "date", "cabin"]) if not df.empty else df


def write_reports(
    offers: List[FlightOffer],
    outdir: str,
    stamp: str,
    base_strategy: str = "mysafar",
    base_table: Optional[BaseFareTable] = None,
    pattern_cfg: Optional[PatternConfig] = None,
    regulated_cfg: Optional[Dict[str, object]] = None,
) -> Dict[str, str]:
    Path(outdir).mkdir(parents=True, exist_ok=True)
    raw = offers_frame(offers)
    comp = comparison_frame(offers, base_strategy, base_table, pattern_cfg=pattern_cfg, regulated_cfg=regulated_cfg)

    paths: Dict[str, str] = {}
    raw_csv = str(Path(outdir) / "offers_{}.csv".format(stamp))
    comp_csv = str(Path(outdir) / "comparison_{}.csv".format(stamp))
    raw.to_csv(raw_csv, index=False, encoding="utf-8-sig")
    comp.to_csv(comp_csv, index=False, encoding="utf-8-sig")
    paths["offers_csv"] = raw_csv
    paths["comparison_csv"] = comp_csv

    xlsx = str(Path(outdir) / "markup_{}.xlsx".format(stamp))
    try:
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            comp.to_excel(writer, sheet_name="comparison", index=False)
            raw.to_excel(writer, sheet_name="offers", index=False)
        paths["excel"] = xlsx
    except Exception:  # openpyxl missing — CSVs are still written
        pass
    return paths
