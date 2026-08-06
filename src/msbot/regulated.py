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
against the airline's own circular), not evidence our markup is wrong.

So offers from any of these five are dropped before the comparison/markup
table is built — for *both* our own offers and competitors' — rather than
trying to special-case "only ignore it when the competitor is the one
undercutting." They still show up in the raw offer dump (`offers_frame`/
`offers_<ts>.csv`) for the record; they just don't participate in the
markup-comparison math, since that price isn't a markup decision at all.

**All fares, charter included.** An earlier reading of this rule carved out
confirmed-charter fares, on the theory that the circular only fixes these
carriers' *system* tickets. The client has since settled it, verbatim:

    اون ۵ تا ایرلاینی که نرخ مصوب بودند (ساها، کاسپین، ایرتور، ماهان،
    سپهران) اگر نرخ ما بالاتر هم بود نیاز نیست تو لیست گزارش اکسل باشه

— all five carriers, out of the report, regardless of whether we happen to be
the more expensive one. So there is no charter exemption here any more.

Dropping the carve-out also removes a real inconsistency it caused: sites
report charter status differently (MySafar and Alibaba flag Caspian charter
fares as ``is_charter=True``, mrbilit doesn't report it at all), so the same
Caspian flight was being excluded on one site and compared on another — which
produced comparison rows built from a partial set of sources.

Airline identity is resolved through :mod:`msbot.airlines`, the same table the
comparison uses to line up flights across sites. That matters: the previous
local alias list here missed MySafar's "ایران ایر تور" entirely — the spelling
has a space, so neither "ایرتور" nor "ایران ایرتور" was a substring of it and
the code ``B9`` wasn't listed either, so every MySafar Iran Airtour fare
slipped into the comparison. One shared table means a carrier spelling only
has to be taught once.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from . import airlines as airlinesmod
from .models import FlightOffer

#: canonical keys (see msbot.airlines.AIRLINE_ALIASES) of the five carriers
#: whose price is set by the airline's circular.
DEFAULT_REGULATED_KEYS: List[str] = [
    "mahan",
    "saha",
    "caspian",
    "iran_airtour",
    "sepehran",
]


def default_keys() -> List[str]:
    return list(DEFAULT_REGULATED_KEYS)


def resolve_keys(cfg: Optional[Dict[str, Any]]) -> Set[str]:
    """``cfg`` is the ``regulated_airlines`` block from config.yaml —
    ``{"enabled": bool, "aliases": [...]}``. Returns the set of canonical
    airline keys to exclude, or an empty set (no filtering) when disabled.

    ``aliases`` entries may be canonical keys (``"mahan"``), IATA/site codes
    (``"w5"``) or names in either script (``"ماهان"``) — each is resolved
    through the shared airline table, so older configs keep working.
    """
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return set()
    aliases = cfg.get("aliases")
    if not aliases:
        return set(DEFAULT_REGULATED_KEYS)

    keys: Set[str] = set()
    for entry in aliases:
        text = str(entry or "").strip()
        if not text:
            continue
        if text in airlinesmod.AIRLINE_ALIASES:
            keys.add(text)
            continue
        # try it as a code first, then as a name fragment
        keys.add(airlinesmod.canonical_from(text, text))
    return keys


def is_regulated(offer: FlightOffer, keys: Iterable[str]) -> bool:
    return airlinesmod.canonical(offer) in set(keys)


def split_regulated(
    offers: List[FlightOffer], cfg: Optional[Dict[str, Any]] = None
) -> "tuple[List[FlightOffer], List[FlightOffer]]":
    """Returns ``(comparable, regulated)`` — ``comparable`` is what should
    feed the markup/comparison table; ``regulated`` is kept only for the raw
    offer dump.
    """
    keys = resolve_keys(cfg)
    if not keys:
        return offers, []
    comparable, regulated = [], []
    for o in offers:
        (regulated if is_regulated(o, keys) else comparable).append(o)
    return comparable, regulated
