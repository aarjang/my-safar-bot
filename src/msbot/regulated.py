"""Airlines whose *system* fares are fixed by airline circular, not agency markup.

The client's full, settled explanation (2026-08-08), after two earlier partial
readings of this rule turned out to each be half right:

    میخواد که در گزارش پروازهایی که مربوط به هواپیمایی کاسپین، ایران‌ایرتورز،
    ماهان، ساها و سپهران هست و سیستمی هست در گزارش نمایش داده نشود. ممکنه
    پروازی باشد که مربوط به این هواپیمایی‌ها باشد و چارتری باشد، این پروازها
    رو می‌خوام داشته باشم. یا حداقل با یه تگی و یا یه روشی دقیق مشخص کنیم که
    این‌ها سیستمی هستن و این‌ها چارتری.

So the rule is specifically about *system* (published/GDS) fares:

* Mahan / Saha / Caspian / Iran Airtour / Sepehran **system** fares are
  dropped before the comparison/markup table is built — for both our own
  offers and competitors' — because the airline's own circular fixes that
  price; a competitor showing a lower number there is the competitor quietly
  rebating commission, not evidence our markup is wrong.
* **Charter** fares from these same five airlines are a different product —
  negotiated capacity, priced the normal way — and stay in the comparison
  like any other flight.
* Every offer keeps its fare type visible: :func:`fare_type` labels each
  reported flight چارتری/سیستمی/نامشخص (see below), and the raw dump
  (``offers_frame``/``offers_<ts>.csv``) always carries every offer
  regardless of type, so nothing is ever silently discarded — only the
  *comparison* table drops the confirmed-system ones from these five.

**Determining system vs. charter.** ``FlightOffer.is_charter`` is site-reported
per offer: ``False`` means the site explicitly marked it a system/published
fare, ``True`` means charter, ``None`` means the site gives no signal at all
(mrbilit's browser-rendered cards carry no such marker; tktfly is the
opposite case — the site is literally "Charter724", so every one of its
offers is confirmed charter, correctly always ``True``). An offer is only
excluded when the airline matches **and** ``is_charter is False`` —
confirmed system. An unmatched signal (``None``) is *not* excluded, on
purpose: the client asked to see these flights rather than have an
uncertain case silently disappear, and it's tagged نامشخص so that's visible
too rather than looking like a confirmed system or charter row.

Airline identity is resolved through :mod:`msbot.airlines`, the same table the
comparison uses to line up flights across sites. That matters: an earlier
local alias list here missed MySafar's "ایران ایر تور" entirely — the
spelling has a space, so neither "ایرتور" nor "ایران ایرتور" was a substring
of it and the code ``B9`` wasn't listed either, so every MySafar Iran Airtour
fare slipped into the comparison. One shared table means a carrier spelling
only has to be taught once.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from . import airlines as airlinesmod
from .models import FlightOffer

#: canonical keys (see msbot.airlines.AIRLINE_ALIASES) of the five carriers
#: whose *system* fares are set by the airline's circular.
DEFAULT_REGULATED_KEYS: List[str] = [
    "mahan",
    "saha",
    "caspian",
    "iran_airtour",
    "sepehran",
]

#: Persian labels for :func:`fare_type`'s three possible outputs.
FARE_TYPE_FA = {"charter": "چارتری", "system": "سیستمی", "unknown": "نامشخص"}


def fare_type(offers: Iterable[FlightOffer], prefer_source: str = "mysafar") -> str:
    """One flight's fare type — "charter"/"system"/"unknown" — for the report.

    Prefers whatever *our own* listing (``prefer_source``) reports, since
    that's the fare actually being sold and the one the exclusion rule above
    cares about. On real data for this route, MySafar itself reports most
    flights — across ordinary carriers too, not just the regulated five — as
    charter (228 of 283 offers on one sample run), and tktfly is a
    charter-only site by nature, so simply taking "any source says charter"
    across the whole group made nearly every row read چارتری regardless of
    what we ourselves sell it as. Preferring our own signal keeps the tag
    meaningful: "the fare we list for this flight is system/charter" rather
    than "at least one of six sites happened to say charter somewhere."

    Falls back to whatever any source reports only for a flight we don't
    sell ourselves — there, a single confirmed-charter offer still wins over
    a confirmed-system one, since the group may combine sources that
    genuinely disagree on the same physical flight's fare bucket.
    """
    ours = [o.is_charter for o in offers if o.source == prefer_source]
    pool = ours if any(s is not None for s in ours) else [o.is_charter for o in offers]
    if any(s is True for s in pool):
        return "charter"
    if any(s is False for s in pool):
        return "system"
    return "unknown"


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
    """True only for a *confirmed system* fare from one of ``keys``' airlines.

    ``is_charter is False`` is the site explicitly saying "published/GDS
    fare" — that's what the circular fixes. ``True`` (confirmed charter) and
    ``None`` (site gives no signal) both fall through to False here: neither
    is a system fare we know of, so neither is hidden.
    """
    if offer.is_charter is not False:
        return False
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
