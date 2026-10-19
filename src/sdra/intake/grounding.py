"""Are a claim's numbers really in its quoted evidence?

The extraction model copies quotes verbatim (checked in verify.py) but states quantities and
dates separately, so a misread "200" or "20 December" would otherwise pass. Here the claimed
quantity must appear as a number in the quotes, and the claimed date must be one way of
reading a date written in them. English and Arabic month names, ISO dates, day-first
numeric dates and Arabic-Indic digits are understood. Relative dates ("in two weeks") are not
grounded, and a numeric date that reads two ways (03/12) is reported as ambiguous: both go
to a person and a status request rather than being guessed.
"""

from __future__ import annotations

import re
from datetime import date

_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_MONTHS = {
    # English
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # Arabic (Egyptian/Gulf forms and Levantine forms)
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "إبريل": 4, "مايو": 5, "يونيو": 6, "يونيه": 6,
    "يوليو": 7, "يوليه": 7, "أغسطس": 8, "اغسطس": 8, "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نوفمبر": 11,
    "ديسمبر": 12, "كانون الثاني": 1, "شباط": 2, "آذار": 3, "نيسان": 4, "أيار": 5, "حزيران": 6, "تموز": 7,
    "آب": 8, "أيلول": 9, "تشرين الأول": 10, "تشرين الثاني": 11, "كانون الأول": 12,
}
_MONTH = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\.?\s+(\d{{1,2}}){_ORD}\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b")
_NUMBER = re.compile(r"\d{1,3}(?:[,٬]\d{3})+|\d+")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_DIGITS))


def numbers_in(text: str) -> set[int]:
    text = _ISO.sub(" ", _normalise(text))
    return {int(re.sub(r"[,٬]", "", n)) for n in _NUMBER.findall(text)}


def _make(year: int | None, month: int, day: int, around: date) -> date | None:
    """A date; without a year, its first occurrence from a month before `around`."""
    try:
        if year is not None:
            return date(year + 2000 if year < 100 else year, month, day)
        for y in (around.year - 1, around.year, around.year + 1):
            d = date(y, month, day)
            if (d - around).days >= -31:
                return d
    except ValueError:
        return None
    return None


def dates_in(text: str, around: date) -> tuple[set[date], set[date]]:
    """(dates written unambiguously, dates that are one of two readings of a numeric date)."""
    text = _normalise(text)
    sure: set[date] = set()
    maybe: set[date] = set()
    for y, m, d in _ISO.findall(text):
        if (x := _make(int(y), int(m), int(d), around)) is not None:
            sure.add(x)
    for d, m, y in _DAY_MONTH.findall(text):
        if (x := _make(int(y) if y else None, _MONTHS[m.casefold()], int(d), around)) is not None:
            sure.add(x)
    for m, d, y in _MONTH_DAY.findall(text):
        if (x := _make(int(y) if y else None, _MONTHS[m.casefold()], int(d), around)) is not None:
            sure.add(x)
    for a, b, y in _NUMERIC.findall(_ISO.sub(" ", text)):
        year = int(y) if y else None
        readings = {x for x in (_make(year, int(b), int(a), around), _make(year, int(a), int(b), around)) if x}
        (sure if len(readings) == 1 else maybe).update(readings)
    return sure, maybe - sure


def check(quotes: list[str], qty: int | None, when: date | None, around: date) -> list[str]:
    """Problems that keep a claim unverified (empty when its values are grounded)."""
    text = " ".join(quotes)
    out = []
    if qty is not None and qty not in numbers_in(text):
        out.append(f"quantity {qty} not in the quoted text")
    if when is not None:
        sure, maybe = dates_in(text, around)
        if when in maybe:
            out.append(f"date {when.isoformat()} is one reading of an ambiguous date")
        elif when not in sure:
            out.append(f"date {when.isoformat()} not in the quoted text")
    return out


def present(quotes: list[str], qty: int | None, when: date | None, around: date) -> bool:
    """The values appear in the quotes at all (a date may be one of two readings)."""
    text = " ".join(quotes)
    if qty is not None and qty not in numbers_in(text):
        return False
    if when is not None:
        sure, maybe = dates_in(text, around)
        return when in sure | maybe
    return True
