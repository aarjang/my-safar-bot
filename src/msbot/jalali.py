"""Gregorian <-> Jalali helpers.

Only tktfly needs Jalali in its URL, but several sites echo Jalali dates back in
labels, so keep the conversion in one place.
"""
from __future__ import annotations

from datetime import date, datetime

import jdatetime


def parse_iso(day: str) -> date:
    return datetime.strptime(day, "%Y-%m-%d").date()


def to_jalali_dash(day: str) -> str:
    """'2026-08-05' -> '1405-05-14' (tktfly's required format)."""
    g = parse_iso(day)
    j = jdatetime.date.fromgregorian(date=g)
    return "{:04d}-{:02d}-{:02d}".format(j.year, j.month, j.day)


def to_jalali_slash(day: str) -> str:
    """'2026-08-05' -> '1405/05/14' (display format)."""
    return to_jalali_dash(day).replace("-", "/")


def from_jalali(jalali: str) -> str:
    """'1405-05-14' or '1405/05/14' -> '2026-08-05'."""
    parts = jalali.replace("/", "-").split("-")
    j = jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
    return j.togregorian().isoformat()


WEEKDAY_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه", "شنبه", "یک‌شنبه"]


def weekday_fa(day: str) -> str:
    return WEEKDAY_FA[parse_iso(day).weekday()]
