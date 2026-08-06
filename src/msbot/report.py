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
from . import airlines as airlinesmod
from . import suppliers as suppliersmod

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
    """One row per *flight* — (route, date, cabin, departure, airline) — with
    our price against each competitor's price for that same flight, + markup.

    Matching on the flight rather than just the cabin is what makes the
    numbers comparable at all: see the grouping comment below.

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

    # Group per *flight*, not per cabin. Grouping only by (route, date, cabin)
    # compared our cheapest business seat against a competitor's cheapest
    # business seat on an entirely different aircraft — which is how the
    # export came to show Alibaba at 57.9M against our 21.1M when the two
    # sites were within a few hundred thousand Toman of each other on every
    # flight they both sold. Departure time plus canonical airline identifies
    # the flight on all six sites (see msbot.airlines for why neither field
    # works alone).
    grouped: Dict[tuple, List[FlightOffer]] = {}
    for o in offers:
        if cabins and o.cabin not in cabins:
            continue
        key = (
            o.route,
            o.search_date,
            o.cabin,
            airlinesmod.departure_hhmm(o.departure_time),
            airlinesmod.canonical(o),
        )
        grouped.setdefault(key, []).append(o)

    for key in sorted(grouped, key=lambda k: tuple("" if p is None else p for p in k)):
        route, day, cabin, departure, _airline_key = key
        cell = grouped[key]
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
            "departure": departure,
            # the row's own airline, taken from whichever site named it — so a
            # flight we don't sell still shows its carrier rather than a blank.
            "airline": airlinesmod.display_name(cell),
            "base_fare_toman": base_fare // 10 if base_fare else None,
            "base_fare_source": base_label,
        }

        ours = by_source.get(OUR_SOURCE) or []
        our_min = min((o.price_rial for o in ours), default=None)
        cheapest_ours = min(ours, key=lambda o: o.price_rial) if ours else None
        row["mysafar_toman"] = our_min // 10 if our_min else None
        row["mysafar_airline"] = cheapest_ours.airline_name if cheapest_ours else None
        # the consolidator behind *our* listing — admin-visible on mysafar's
        # own site, and blank unless data/suppliers.json names the id.
        row["mysafar_supplier"] = suppliersmod.name_for(
            (cheapest_ours.raw or {}).get("supplierId") if cheapest_ours else None
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
    if df.empty:
        return df
    return df.sort_values(["route", "date", "cabin", "departure"], na_position="last")


#: display order + labels for the light report's competitor columns — the
#: client's own wording, verbatim (English site names, Persian for the rest).
LIGHT_SOURCE_COLUMNS = [
    ("tktfly", "نرخ TicketFly"),
    ("flytoday", "نرخ FlyToday"),
    ("snapptrip", "نرخ اسنپ‌تریپ"),
    ("mrbilit", "نرخ مستربلیط"),
    ("alibaba", "نرخ علی‌بابا"),
]

CABIN_FA = {
    "economy": "اکونومی",
    "premium_economy": "پرمیوم اکونومی",
    "business": "بیزینس",
    "first": "فرست",
    "unknown": "نامشخص",
}


def light_report_frame(
    offers: List[FlightOffer],
    base_strategy: str = "mysafar",
    base_table: Optional[BaseFareTable] = None,
    pattern_cfg: Optional[PatternConfig] = None,
    regulated_cfg: Optional[Dict[str, object]] = None,
) -> pd.DataFrame:
    """The client's own words: "خروجی اکسل رو یه مقدار سبک‌تر کنیم... فقط وقتی
    نرخ‌ها با الگوی مارکاپی که قبلاً تعریف شده فرق داشته باشن توی گزارش بیاد...
    پروازی که توی سایت ما موجود نیست رو هم نشون [بده]".

    Same underlying data as :func:`comparison_frame` — same pattern
    thresholds, same regulated-airline exclusion — just narrowed to what's
    actually actionable for a manual markup review:

    * only the 10 columns asked for (route/date/cabin/airline + each site's
      price), dropping the diff/%/base-fare columns that make the full
      export slow to scan;
    * only rows that need a look: outside the expected pattern for their
      cabin (``pattern_ok is False``), *or* a flight a competitor sells that
      we don't (``mysafar_toman`` is empty) — rows that are simply fine
      (``pattern_ok is True``) are the ones this is meant to filter out.
    """
    comp = comparison_frame(offers, base_strategy, base_table, pattern_cfg=pattern_cfg, regulated_cfg=regulated_cfg)
    if comp.empty:
        return comp

    flagged = comp[(comp["pattern_ok"] == False) | (comp["mysafar_toman"].isna())]  # noqa: E712
    if flagged.empty:
        return pd.DataFrame(columns=[
            "مسیر", "تاریخ", "ساعت", "کابین", "ایرلاین", "نرخ مای‌سفر",
            *[label for _, label in LIGHT_SOURCE_COLUMNS],
        ])

    out = pd.DataFrame({
        "مسیر": flagged["route"],
        "تاریخ": flagged["date_jalali"],
        # rows are per-flight now, so without the departure time several rows
        # would share a route/date/cabin and be impossible to tell apart —
        # it's also how the client identifies a flight when checking by hand.
        "ساعت": flagged["departure"],
        "کابین": flagged["cabin"].map(lambda c: CABIN_FA.get(c, c)),
        # the flight's own airline, not just the one on our listing, so rows
        # for flights we don't sell still name the carrier.
        "ایرلاین": flagged["airline"],
        "تأمین‌کننده": flagged["mysafar_supplier"],
        "نرخ مای‌سفر": flagged["mysafar_toman"],
    })
    for source_id, label in LIGHT_SOURCE_COLUMNS:
        col = "{}_toman".format(source_id)
        out[label] = flagged[col] if col in flagged.columns else None

    return out.sort_values(["مسیر", "تاریخ", "ساعت"], na_position="last")


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
    light = light_report_frame(offers, base_strategy, base_table, pattern_cfg=pattern_cfg, regulated_cfg=regulated_cfg)

    paths: Dict[str, str] = {}
    raw_csv = str(Path(outdir) / "offers_{}.csv".format(stamp))
    comp_csv = str(Path(outdir) / "comparison_{}.csv".format(stamp))
    light_csv = str(Path(outdir) / "light_{}.csv".format(stamp))
    raw.to_csv(raw_csv, index=False, encoding="utf-8-sig")
    comp.to_csv(comp_csv, index=False, encoding="utf-8-sig")
    light.to_csv(light_csv, index=False, encoding="utf-8-sig")
    paths["offers_csv"] = raw_csv
    paths["comparison_csv"] = comp_csv
    paths["light_csv"] = light_csv

    xlsx = str(Path(outdir) / "markup_{}.xlsx".format(stamp))
    try:
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            # sheet order matters for a human opening the file — put the
            # thing they actually asked to review first.
            light.to_excel(writer, sheet_name="نیاز به بررسی", index=False)
            comp.to_excel(writer, sheet_name="comparison", index=False)
            raw.to_excel(writer, sheet_name="offers", index=False)
        paths["excel"] = xlsx
    except Exception:  # openpyxl missing — CSVs are still written
        pass
    return paths
