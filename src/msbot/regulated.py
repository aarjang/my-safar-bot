"""Airlines whose fares are fixed by airline circular, not agency markup.

From the client, verbatim:

    طبق بخشنامه‌ای که خود ایرلاین‌ها دادن، نرخ بلیت‌های ماهان، ساها، کاسپین،
    ایرتور و سپهران باید با همون نرخ مصوب روی سایت‌ها نمایش داده بشه.
    ایرتور و سپهران روی همه سایت‌ها با همون نرخ مصوب نمایش داده میشن و
    مشکلی ندارن. ولی برای ماهان، ساها و کاسپین، بعضی از سایت‌ها قیمت‌ها رو با
    کسر کمیسیون و پایین‌تر از نرخ مصوب نشون میدن که نیازی نیست توی جدول
    گزارش مقایسه نرخ باشه. در مورد این سه ایرلاین کمیسیون و مارکاپشون از قبل
    تنظیم شده و همون قیمتی که الان روی سایت خودمون نمایش داده میشه، قیمت
    درست و مدنظر ایرلاینه.

In plain terms: for these five airlines, the *airline itself* sets the
selling price — it isn't ours to discount, and if a competitor shows a lower
number, that's the competitor quietly rebating their commission (arguably
against the airline's own circular), not evidence our markup is wrong. Iran
Airtour and Sepehran are reported as already consistent everywhere, so
excluding them changes nothing in practice; Mahan/Saha/Caspian are the ones
where a competitor's non-compliant lower price would otherwise drag down
"market_min" and trigger a false "we're too expensive, lower your price" flag.

So offers from any of these five are dropped before the comparison/markup
table is built — for *both* our own offers and competitors' — rather than
trying to special-case "only ignore it when the competitor is the one
undercutting." They still show up in the raw offer dump (`offers_frame`/
`offers_<ts>.csv`) for the record; they just don't participate in the
markup-comparison math, since that price isn't a markup decision at all.

**System fares only, not charter.** Follow-up from the client:

    پروازهای سیستمی ساها، کاسپین، ماهان با اینکه ما قیمتمون بالاتر هست،
    تو لیست نرخ گران‌تر نباشه

The airline circular fixes the price of these carriers' *system* (published/
GDS) tickets specifically — it says nothing about charter capacity they sell
on the side, which is priced the normal negotiated way and *is* a fair
markup comparison. So the exclusion only applies when ``is_charter`` is not
``True`` (covers both confirmed-system offers and sources that don't report
charter/system at all, e.g. mrbilit — safer to under-compare a possible
charter fare than to keep producing the false "we're too expensive" flag
this rule exists to fix). A confirmed charter offer from Mahan/Saha/Caspian
is treated like any other competitor offer.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .models import FlightOffer

#: name/code fragments (lowercased) that identify each regulated airline
#: across sites — spellings and codes are inconsistent source to source, so
#: this matches loosely on whatever the scraper populated in
#: ``airline_name``/``airline_code`` rather than relying on one exact IATA
#: code.
DEFAULT_REGULATED_ALIASES: Dict[str, List[str]] = {
    "mahan": ["ماهان", "mahan", "w5"],
    "saha": ["ساها", "saha", "irz"],
    "caspian": ["کاسپین", "caspian"],
    "iran_airtour": ["ایرتور", "ایران ایرتور", "airtour"],
    "sepehran": ["سپهران", "sepehran"],
}


def _normalize(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def default_aliases() -> List[str]:
    flat: List[str] = []
    for group in DEFAULT_REGULATED_ALIASES.values():
        flat.extend(group)
    return flat


def resolve_aliases(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """``cfg`` is the ``regulated_airlines`` block from config.yaml —
    ``{"enabled": bool, "aliases": [...]}``. Falls back to the defaults above
    when unset, and returns ``[]`` (no filtering) when explicitly disabled.
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return []
    aliases = cfg.get("aliases")
    return list(aliases) if aliases else default_aliases()


def is_regulated(offer: FlightOffer, aliases: Iterable[str]) -> bool:
    if offer.is_charter is True:
        return False  # confirmed charter — the airline circular doesn't cover this fare
    name = _normalize(offer.airline_name)
    code = _normalize(offer.airline_code)
    for alias in aliases:
        a = _normalize(alias)
        if not a:
            continue
        if a == code or (len(a) >= 3 and a in name):
            return True
    return False


def split_regulated(
    offers: List[FlightOffer], cfg: Optional[Dict[str, Any]] = None
) -> "tuple[List[FlightOffer], List[FlightOffer]]":
    """Returns ``(comparable, regulated)`` — ``comparable`` is what should
    feed the markup/comparison table; ``regulated`` is kept only for the raw
    offer dump.
    """
    aliases = resolve_aliases(cfg)
    if not aliases:
        return offers, []
    comparable, regulated = [], []
    for o in offers:
        (regulated if is_regulated(o, aliases) else comparable).append(o)
    return comparable, regulated
