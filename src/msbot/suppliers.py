"""Map MySafar's opaque ``supplierId`` to the consolidator name the admin sees.

The client asked for the supplier shown on their own admin view of a flight
("سپید پرواز توسن" in the screenshot) to appear in the Excel export. The
public search API only returns the id — an ObjectId such as
``692c0c90b3108118fb8a6ead`` — and the endpoint that would name it,
``GET https://api.mysafar.com/v1/supplier``, answers **401 Unauthorized**.
(It exists; it just needs an admin session. Every other spelling we tried —
``/v1/suppliers``, ``/v1/supplier/<id>``, ``/v1/flight/supplier`` — is a 404,
so this is the one.)

So the names live in a small JSON file the operator supplies, rather than
being guessed or scraped from an authenticated endpoint we have no
credentials for:

    data/suppliers.json
    {"692c0c90b3108118fb8a6ead": "سپید پرواز توسن", ...}

Until that file exists the supplier column is simply blank — never the raw
id, which would be noise in a report meant for humans.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_PATH = "data/suppliers.json"

_cache: Optional[Dict[str, str]] = None
_cache_path: Optional[str] = None


def load(path: str = DEFAULT_PATH) -> Dict[str, str]:
    """id -> display name. Missing or malformed file yields an empty map: the
    supplier column is a nice-to-have, and a bad file must not take the whole
    report down with it."""
    global _cache, _cache_path
    if _cache is not None and _cache_path == path:
        return _cache

    mapping: Dict[str, str] = {}
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                mapping = {str(k): str(v) for k, v in data.items() if k and v}
            else:
                log.warning("%s should be a {id: name} object, got %s", path, type(data).__name__)
        except (ValueError, OSError) as exc:
            log.warning("could not read supplier names from %s: %s", path, exc)

    _cache, _cache_path = mapping, path
    return mapping


def name_for(supplier_id: Optional[str], path: str = DEFAULT_PATH) -> Optional[str]:
    if not supplier_id:
        return None
    return load(path).get(str(supplier_id))


def reset_cache() -> None:
    """Drop the memoized map — for tests, and so a freshly written
    suppliers.json takes effect without restarting the dashboard."""
    global _cache, _cache_path
    _cache, _cache_path = None, None
