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
from typing import Any, Dict, List, Optional

from .models import FlightOffer

#: canonical key -> {"fa": the label shown in reports, "codes": IATA/site
#: codes, "names": name fragments in any script}
AIRLINE_ALIASES: Dict[str, Dict[str, Any]] = {
    "iranair": {"fa": "ایران ایر", "codes": ["ir"], "names": ["ایران ایر", "iranair", "iran air"]},
    "iran_airtour": {"fa": "ایران ایرتور", 
        "codes": ["b9"],
        "names": ["ایران ایر تور", "ایران ایرتور", "ایرتور", "iran airtour", "airtour"],
    },
    "mahan": {"fa": "ماهان", "codes": ["w5"], "names": ["ماهان", "mahan"]},
    "caspian": {"fa": "کاسپین", "codes": ["iv", "cpn"], "names": ["کاسپین", "caspian"]},
    "ata": {"fa": "آتا", "codes": ["i3"], "names": ["آتا", "ata"]},
    "meraj": {"fa": "معراج", "codes": ["j1"], "names": ["معراج", "meraj"]},
    "qeshm": {"fa": "قشم ایر", "codes": ["qb"], "names": ["قشم", "qeshm"]},
    "soroush": {"fa": "سروش ایر", "codes": ["shr"], "names": ["سروش", "soroush"]},
    "taban": {"fa": "تابان", "codes": ["hh"], "names": ["تابان", "taban"]},
    "varesh": {"fa": "وارش", "codes": ["vr"], "names": ["وارش", "varesh"]},
    "saha": {"fa": "ساها", "codes": ["irz", "sa"], "names": ["ساها", "saha"]},
    "sepehran": {"fa": "سپهران", "codes": ["sp", "is"], "names": ["سپهران", "sepehran"]},
    "zagros": {"fa": "زاگرس", "codes": ["zv", "iz"], "names": ["زاگرس", "zagros"]},
    "ava": {"fa": "آوا ایر", "codes": ["ax", "axv"], "names": ["آوا", "ava"]},
    # Fly Kish and Kish Air are different carriers, and "کیش" matches both —
    # the longest-alias-first rule below is what keeps them apart, so the
    # Fly Kish spellings must stay longer than the bare "کیش".
    "fly_kish": {"fa": "فلای کیش", "codes": ["fk", "tkn"], "names": ["فلای کیش", "fly kish", "flykish"]},
    "kish": {"fa": "کیش ایر", "codes": ["y9"], "names": ["کیش ایر", "kish air", "کیش", "kish"]},
    "karun": {"fa": "کارون", "codes": ["kr", "eq"], "names": ["کارون", "karun"]},
    "pouya": {"fa": "پویا", "codes": ["pya"], "names": ["پویا", "pouya"]},
    "sepahan": {"fa": "سپاهان", "codes": ["ihc"], "names": ["سپاهان", "sepahan"]},
    "chabahar": {"fa": "چابهار", "codes": ["ib"], "names": ["چابهار", "chabahar"]},
    "yazd": {"fa": "یزد", "codes": ["yzr"], "names": ["یزد", "yazd"]},
    "fly_persia": {"fa": "فلای پرشیا", "codes": ["fp"], "names": ["فلای پرشیا", "flypersia", "fly persia"]},
    "turkish": {"fa": "ترکیش", "codes": ["tk"], "names": ["ترکیش", "turkish"]},
    "pegasus": {"fa": "پگاسوس", "codes": ["pc"], "names": ["پگاسوس", "pegasus"]},
    "qatar": {"fa": "قطر", "codes": ["qr"], "names": ["قطر", "qatar"]},
    "flydubai": {"fa": "فلای دبی", "codes": ["fz"], "names": ["فلای دبی", "flydubai", "fly dubai"]},
    "emirates": {"fa": "امارات", "codes": ["ek"], "names": ["امارات", "emirates"]},
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
    """The label for a flight, in the client's report.

    Prefers this table's own Persian name so a carrier reads identically on
    every row. Taking it from the offers instead made the label depend on
    which sites happened to cover that flight — the same airline appeared as
    both "معراج" and "هواپیمایی معراج" down one column.

    Falls back to a site's own name for carriers not in the table.
    """
    for o in offers:
        fa = AIRLINE_ALIASES.get(canonical(o), {}).get("fa")
        if fa:
            return fa
    persian = [o.airline_name for o in offers if o.airline_name and re.search(r"[؀-ۿ]", o.airline_name)]
    if persian:
        return max(persian, key=len)
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
