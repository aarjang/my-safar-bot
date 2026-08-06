"""One stable identity per airline, across sites that each name it differently.

The comparison table has to line up the *same flight* on every site, and no
single field does that on its own:

* ``airline_code`` is missing entirely on mrbilit, and the sites that do send
  it don't always agree — MySafar calls Caspian ``CPN``, Alibaba calls it
  ``IV``.
* ``airline_name`` is Persian on MySafar/mrbilit and English on Alibaba, and
  even the Persian differs ("هواپیمایی آتا" vs "آتا", "ایران ایر تور" vs
  "ایران ایرتور").

So each carrier gets one canonical key plus every spelling and code seen in
the wild. :func:`canonical` resolves an offer to that key.

The one real trap here: "ایران ایر" is a prefix of "ایران ایر تور", so a naive
substring match files every Iran Airtour flight under Iran Air and silently
compares two different carriers' fares. Codes are therefore tried first, and
name matching prefers the longest alias that fits.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .models import FlightOffer

#: canonical key -> (IATA/site codes, name fragments in any script)
AIRLINE_ALIASES: Dict[str, Dict[str, List[str]]] = {
    "iranair": {"codes": ["ir"], "names": ["ایران ایر", "iranair", "iran air"]},
    "iran_airtour": {
        "codes": ["b9"],
        "names": ["ایران ایر تور", "ایران ایرتور", "ایرتور", "iran airtour", "airtour"],
    },
    "mahan": {"codes": ["w5"], "names": ["ماهان", "mahan"]},
    "caspian": {"codes": ["iv", "cpn"], "names": ["کاسپین", "caspian"]},
    "ata": {"codes": ["i3"], "names": ["آتا", "ata"]},
    "meraj": {"codes": ["j1"], "names": ["معراج", "meraj"]},
    "qeshm": {"codes": ["qb"], "names": ["قشم", "qeshm"]},
    "soroush": {"codes": ["shr"], "names": ["سروش", "soroush"]},
    "taban": {"codes": ["hh"], "names": ["تابان", "taban"]},
    "varesh": {"codes": ["vr"], "names": ["وارش", "varesh"]},
    "saha": {"codes": ["irz"], "names": ["ساها", "saha"]},
    "sepehran": {"codes": ["sp"], "names": ["سپهران", "sepehran"]},
    "zagros": {"codes": ["zv", "iz"], "names": ["زاگرس", "zagros"]},
    "kish": {"codes": ["y9"], "names": ["کیش ایر", "کیش", "kish"]},
    "karun": {"codes": ["kr", "eq"], "names": ["کارون", "karun"]},
    "pouya": {"codes": ["pya"], "names": ["پویا", "pouya"]},
    "sepahan": {"codes": ["ihc"], "names": ["سپاهان", "sepahan"]},
    "chabahar": {"codes": ["ib"], "names": ["چابهار", "chabahar"]},
    "yazd": {"codes": ["yzr"], "names": ["یزد", "yazd"]},
    "fly_persia": {"codes": ["fp"], "names": ["فلای پرشیا", "flypersia", "fly persia"]},
    "turkish": {"codes": ["tk"], "names": ["ترکیش", "turkish"]},
    "pegasus": {"codes": ["pc"], "names": ["پگاسوس", "pegasus"]},
    "qatar": {"codes": ["qr"], "names": ["قطر", "qatar"]},
    "flydubai": {"codes": ["fz"], "names": ["فلای دبی", "flydubai", "fly dubai"]},
    "emirates": {"codes": ["ek"], "names": ["امارات", "emirates"]},
}

_ARABIC_YE_KE = str.maketrans({"ي": "ی", "ك": "ک", "‌": " "})


def _norm(s: Optional[str]) -> str:
    """Lowercase, collapse whitespace, and fold the Arabic ye/kaf and ZWNJ that
    make the same Persian name compare unequal between two sites."""
    s = (s or "").translate(_ARABIC_YE_KE)
    return re.sub(r"\s+", " ", s.strip().lower())


#: name aliases, longest first — so "ایران ایر تور" is tested before the
#: "ایران ایر" that is a prefix of it.
_NAME_INDEX: List[tuple] = sorted(
    ((_norm(alias), key) for key, spec in AIRLINE_ALIASES.items() for alias in spec["names"]),
    key=lambda pair: len(pair[0]),
    reverse=True,
)
_CODE_INDEX: Dict[str, str] = {
    _norm(code): key for key, spec in AIRLINE_ALIASES.items() for code in spec["codes"]
}


def canonical_from(code: Optional[str], name: Optional[str]) -> str:
    """Resolve a raw (code, name) pair to a canonical key. Codes win, because
    a name can be a prefix of another carrier's — see the module docstring."""
    c = _norm(code)
    if c and c in _CODE_INDEX:
        return _CODE_INDEX[c]

    n = _norm(name)
    if n:
        for alias, key in _NAME_INDEX:
            if alias and alias in n:
                return key
    return n or "unknown"


def canonical(offer: FlightOffer) -> str:
    """The airline's stable key, e.g. ``"soroush"``.

    Falls back to the normalized airline name when the carrier isn't in the
    table — unknown airlines still group with themselves per site, they just
    can't be matched across sites until an alias is added here. Returning the
    name (rather than one shared ``"unknown"``) keeps two different unlisted
    carriers from being compared against each other.
    """
    return canonical_from(offer.airline_code, offer.airline_name)


def display_name(offers: List[FlightOffer]) -> Optional[str]:
    """Pick the friendliest label for a flight the client will read: a Persian
    name if any site gave one, otherwise whatever exists."""
    persian = [o.airline_name for o in offers if o.airline_name and re.search(r"[؀-ۿ]", o.airline_name)]
    if persian:
        return max(persian, key=len)  # "هواپیمایی آتا" reads better than "آتا"
    named = [o.airline_name for o in offers if o.airline_name]
    return named[0] if named else None


_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def departure_hhmm(value: Optional[str]) -> Optional[str]:
    """Normalize the three departure formats in play to ``"HH:MM"``:
    ``"2026-08-06 11:30"`` (MySafar), ``"2026-08-06T11:30:00"`` (Alibaba) and
    a bare ``"11:30"`` (mrbilit). This is the only field every source
    populates, so it carries most of the flight-matching weight.
    """
    m = _TIME_RE.search(str(value or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return "%02d:%02d" % (hour, minute)
