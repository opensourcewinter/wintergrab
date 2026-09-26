"""Dates and times as written on web pages -> :class:`datetime.date` / :class:`datetime.datetime`.

Understands ISO 8601 (``2024-03-05``, ``2024-03-05T10:30:00Z``), HTTP/RFC 2822
dates (``Tue, 05 Mar 2024 10:30:00 GMT``), numeric dates (``05/03/2024``,
``5.3.24``), dates with month names in English, German, French, Spanish,
Italian, Portuguese and Dutch (``March 5th, 2024``, ``5. März 2024``,
``5 de marzo de 2024``), month-and-year (``March 2024``), relative dates
(``yesterday``, ``3 days ago``, ``vor 2 Stunden``, ``hace 3 días``) and Unix
timestamps.

Guesses are reported in ``notes``: ``"ambiguous-day-month"`` (``03/05/2024``
could be either; ``dayfirst`` decides, default ``False`` = month first),
``"two-digit-year"``, ``"relative-date"``, ``"day-missing"``, ``"timestamp"``,
``"no-timezone"``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .numbers import _note

__all__ = ["parse_date", "parse_datetime", "parse_duration"]

_MONTHS: dict[str, int] = {}
for _number, _names in enumerate(
    (
        "january jan januar janv janvier enero ene gennaio gen janeiro januari jänner jän",
        "february feb februar févr fevr février fevrier febrero febbraio fevereiro fev februari",
        "march mar märz maerz mars marzo março marco maart mrt",
        "april apr avril avr abril aprile",
        "may mai mayo maggio mag maio mei",
        "june jun juni juin junio giugno giu junho",
        "july jul juli juillet juil julio luglio lug julho",
        "august aug août aout agosto ago augustus",
        "september sep sept septembre septiembre settembre set setembro",
        "october oct oktober octobre octubre ottobre ott outubro out okt",
        "november nov novembre noviembre novembro",
        "december dec dez dezember décembre decembre diciembre dic dicembre dezembro",
    ),
    start=1,
):
    for _name in _names.split():
        _MONTHS.setdefault(_name, _number)

_MONTH_RE = "|".join(sorted((re.escape(m) for m in _MONTHS), key=len, reverse=True))
_WEEKDAYS = (
    r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|urday|sday)?|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|lundi|mardi|mercredi|jeudi|vendredi|samedi|"
    r"dimanche|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo"
)
_ORD = r"(?:st|nd|rd|th|er|e|º|ª|\.)?"
_TIME = (
    r"(?:(?:\s*(?:,|at|um|à|a las|alle|às|T)?\s*)"
    r"(?P<hour>\d{1,2})[:.h](?P<minute>\d{2})(?:[:.](?P<second>\d{2})(?:[.,](?P<fraction>\d{1,9}))?)?"
    r"\s*(?P<ampm>[ap]\.?\s?m\.?)?(?:\s*(?:uhr|h))?"
    r"\s*(?P<tz>Z|UTC|GMT|[+-]\d{2}:?\d{2}|[A-Z]{3,4}(?![a-z]))?)?"
)
_ISO = re.compile(
    r"(?P<year>\d{4})-?(?P<month>\d{2})-?(?P<day>\d{2})"
    r"(?:[T\s](?P<hour>\d{2}):?(?P<minute>\d{2})(?::?(?P<second>\d{2})(?:[.,](?P<fraction>\d{1,9}))?)?"
    r"\s*(?P<tz>Z|[+-]\d{2}(?::?\d{2})?)?)?$",
    re.I,
)
_TEXTUAL = [
    # March 5th, 2024 / Mar 5 2024 / Tuesday, March 5, 2024 10:30 AM
    re.compile(
        rf"^(?:(?:{_WEEKDAYS})\.?,?\s+)?(?P<mname>{_MONTH_RE})\.?\s+(?P<day>\d{{1,2}}){_ORD},?\s+(?P<year>\d{{2,4}})\b{_TIME}",
        re.I,
    ),
    # 5 March 2024 / 5th of March, 2024 / 5. März 2024 / 5 de marzo de 2024 / le 5 mars 2024
    re.compile(
        rf"^(?:le\s+|the\s+)?(?:(?:{_WEEKDAYS})\.?,?\s+)?(?P<day>\d{{1,2}}){_ORD}\s*(?:of\s+|de\s+)?(?P<mname>{_MONTH_RE})\.?,?\s+(?:de\s+)?(?P<year>\d{{2,4}})\b{_TIME}",
        re.I,
    ),
    # 2024 March 5 (and 2024年3月5日 below)
    re.compile(rf"^(?P<year>\d{{4}})\s+(?P<mname>{_MONTH_RE})\.?\s+(?P<day>\d{{1,2}}){_ORD}\b{_TIME}", re.I),
]
_MONTH_YEAR = re.compile(rf"^(?P<mname>{_MONTH_RE})\.?,?\s+(?P<year>\d{{4}})$", re.I)
_CJK = re.compile(r"^(?P<year>\d{4})\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日")
_NUMERIC = re.compile(r"^(?P<a>\d{1,4})(?P<sep>[/.\-])(?P<b>\d{1,2})(?P=sep)(?P<c>\d{1,4})\.?" + _TIME, re.I)
_RELATIVE_AGO = re.compile(
    r"^(?:(?P<n>\d+|an?|one|un|une|ein|eine|einem|einer)\s*(?P<unit>[a-zäéí]+)\s+(?:ago|her)"
    r"|vor\s+(?P<n2>\d+|einem|einer)\s+(?P<unit2>[a-zä]+)"
    r"|il y a\s+(?P<n3>\d+|un|une)\s+(?P<unit3>[a-zé]+)"
    r"|hace\s+(?P<n4>\d+|un|una)\s+(?P<unit4>[a-zí]+))$",
    re.I,
)
_UNITS = {
    "second": 1, "sec": 1, "s": 1, "sekunde": 1, "sekunden": 1, "seconde": 1, "segundo": 1,
    "minute": 60, "min": 60, "mins": 60, "minuten": 60, "minuto": 60,
    "hour": 3600, "hr": 3600, "h": 3600, "stunde": 3600, "stunden": 3600, "heure": 3600, "hora": 3600,
    "day": 86400, "d": 86400, "tag": 86400, "tagen": 86400, "jour": 86400, "día": 86400, "dia": 86400,
    "week": 604800, "wk": 604800, "woche": 604800, "wochen": 604800, "semaine": 604800, "semana": 604800,
    "month": 2629746, "monat": 2629746, "monaten": 2629746, "mois": 2629746, "mes": 2629746, "meses": 2629746,
    "year": 31556952, "yr": 31556952, "jahr": 31556952, "jahren": 31556952, "an": 31556952, "ans": 31556952,
    "año": 31556952, "años": 31556952, "ano": 31556952,
}  # fmt: skip
_WORDS_ONE = {"a", "an", "one", "un", "une", "una", "ein", "eine", "einem", "einer"}
_RELATIVE_DAYS = {
    "today": 0, "now": 0, "heute": 0, "aujourd'hui": 0, "hoy": 0, "oggi": 0, "hoje": 0, "vandaag": 0,
    "yesterday": -1, "gestern": -1, "hier": -1, "ayer": -1, "ieri": -1, "ontem": -1, "gisteren": -1,
    "tomorrow": 1, "morgen": 1, "demain": 1, "mañana": 1, "manana": 1, "domani": 1, "amanhã": 1,
}  # fmt: skip
# Unambiguous time-zone abbreviations (IST, CST in Asia, BST... are ambiguous and ignored).
_TZ_ABBR = {
    "UTC": 0, "GMT": 0, "Z": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8,
    "PDT": -7, "AKST": -9, "AKDT": -8, "HST": -10, "CET": 1, "CEST": 2, "EET": 2, "EEST": 3, "WET": 0, "WEST": 1,
    "JST": 9, "KST": 9, "AEST": 10, "AEDT": 11, "ACST": 9.5, "AWST": 8, "NZST": 12, "NZDT": 13, "MSK": 3,
}  # fmt: skip


def _year(text: str, notes: list[str] | None) -> int:
    year = int(text)
    if len(text) <= 2:
        _note(notes, "two-digit-year")
        year += 2000 if year < 70 else 1900
    return year


def _tzinfo(text: str | None) -> timezone | None:
    if not text:
        return None
    text = text.strip().upper()
    if text in _TZ_ABBR:
        return timezone(timedelta(hours=_TZ_ABBR[text]))
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", text)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
        if minutes > 18 * 60:
            return None
        return timezone(sign * timedelta(minutes=minutes))
    return None


def _clock(match: re.Match[str]) -> tuple[int, int, int, int] | None:
    """``(hour, minute, second, microsecond)`` from a match with the ``_TIME`` groups (``None`` if absent)."""
    hour_text = match.groupdict().get("hour")
    if hour_text is None:
        return None
    hour, minute = int(hour_text), int(match.group("minute"))
    second = int(match.group("second") or 0)
    fraction = match.groupdict().get("fraction")
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    ampm = (match.groupdict().get("ampm") or "").replace(".", "").replace(" ", "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59 or second > 60:
        return None
    return hour, minute, min(second, 59), micro


def _build(
    year: int, month: int, day: int, match: re.Match[str] | None, notes: list[str] | None
) -> date | datetime | None:
    try:
        day_value = date(year, month, day)
    except ValueError:
        return None
    if match is None:
        return day_value
    clock = _clock(match)
    if clock is None:
        return day_value
    tz = _tzinfo(match.groupdict().get("tz"))
    if match.groupdict().get("tz") and tz is None:
        _note(notes, "unknown-timezone")
    return datetime(year, month, day, *clock, tzinfo=tz)


def _parse(text: str, dayfirst: bool | None, now: datetime | None, notes: list[str] | None) -> date | datetime | None:
    text = " ".join(text.replace(_NBSP, " ").split()).strip(" ,.")
    if not text:
        return None
    value = _parse_exact(text, dayfirst, now, notes)
    if value is not None or len(text) > 200:
        return value
    # "Posted on 5 March 2024", "Updated: 2024-03-05": try from each later word.
    for match in _WORD_START.finditer(text, 1):
        value = _parse_exact(text[match.start() :], dayfirst, now, notes)
        if value is not None:
            _note(notes, "date-in-text")
            return value
    return None


_NBSP = chr(0xA0)
_WORD_START = re.compile(r"(?<!\w)(?=\w)")


def _parse_exact(
    text: str, dayfirst: bool | None, now: datetime | None, notes: list[str] | None
) -> date | datetime | None:
    low = text.lower()
    if low in _RELATIVE_DAYS:
        _note(notes, "relative-date")
        base = (now or datetime.now(timezone.utc)).date()
        return base + timedelta(days=_RELATIVE_DAYS[low])
    relative = _RELATIVE_AGO.match(low)
    if relative:
        amount = next(
            g for g in (relative.group("n"), relative.group("n2"), relative.group("n3"), relative.group("n4")) if g
        )
        unit = next(
            g
            for g in (relative.group("unit"), relative.group("unit2"), relative.group("unit3"), relative.group("unit4"))
            if g
        )
        seconds = _UNITS.get(unit) or _UNITS.get(unit.rstrip("s")) or _UNITS.get(unit.rstrip("en"))
        if seconds is not None:
            count = 1 if amount in _WORDS_ONE else int(amount)
            _note(notes, "relative-date")
            return (now or datetime.now(timezone.utc)) - timedelta(seconds=count * seconds)
    if text.isdigit() and len(text) in (10, 13):
        seconds_since_epoch = int(text) / (1000 if len(text) == 13 else 1)
        stamp = datetime.fromtimestamp(seconds_since_epoch, tz=timezone.utc)
        if 1990 <= stamp.year <= 2100:
            _note(notes, "timestamp")
            return stamp
    match = _ISO.match(text)
    if match:
        return _build(int(match.group("year")), int(match.group("month")), int(match.group("day")), match, notes)
    match = _CJK.match(text)
    if match:
        return _build(int(match.group("year")), int(match.group("month")), int(match.group("day")), None, notes)
    if re.match(r"^[A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}", text):
        try:
            return parsedate_to_datetime(text)  # RFC 2822 / HTTP dates
        except (TypeError, ValueError, IndexError):
            pass
    for pattern in _TEXTUAL:
        match = pattern.match(text)
        if match:
            month = _MONTHS.get(match.group("mname").lower().rstrip("."))
            if month is None:
                continue
            return _build(_year(match.group("year"), notes), month, int(match.group("day")), match, notes)
    match = _MONTH_YEAR.match(text)
    if match:
        month = _MONTHS.get(match.group("mname").lower().rstrip("."))
        if month:
            _note(notes, "day-missing")
            return _build(int(match.group("year")), month, 1, None, notes)
    match = _NUMERIC.match(text)
    if match:
        return _numeric(match, dayfirst, notes)
    return None


def _numeric(match: re.Match[str], dayfirst: bool | None, notes: list[str] | None) -> date | datetime | None:
    a, b, c = match.group("a"), match.group("b"), match.group("c")
    if len(a) == 4:  # 2024/03/05: year first is always year-month-day
        return _build(int(a), int(b), int(c), match, notes)
    if len(c) not in (2, 4):
        return None
    year = _year(c, notes)
    first, second = int(a), int(b)
    if match.group("sep") == "." and dayfirst is None:
        dayfirst = True  # 05.03.2024 is day-first wherever dots are used
    if first > 12 and second <= 12:
        day, month = first, second
    elif second > 12 and first <= 12:
        day, month = second, first
    elif first == second:
        day = month = first
    else:
        if dayfirst is None:
            _note(notes, "ambiguous-day-month")
        day, month = (first, second) if dayfirst else (second, first)
    return _build(year, month, day, match, notes)


def parse_date(
    text: str | date | None,
    *,
    dayfirst: bool | None = None,
    now: datetime | None = None,
    notes: list[str] | None = None,
) -> date | None:
    """The calendar date in ``text`` (the time of day, if any, is dropped). ``None`` if there is none.

    Args:
        dayfirst: Read ``03/05/2024`` as 3 May (``True``) or March 5 (``False``, the default when unknown).
        now: Reference time for relative dates (default: now, UTC).
    """
    if text is None:
        return None
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    value = _parse(str(text), dayfirst, now, notes)
    if isinstance(value, datetime):
        return value.date()
    return value


def parse_datetime(
    text: str | date | None,
    *,
    dayfirst: bool | None = None,
    tz: timezone | None = None,
    now: datetime | None = None,
    notes: list[str] | None = None,
) -> datetime | None:
    """The date and time in ``text``. A date alone becomes midnight.

    ``tz`` is assumed when the text gives no time zone (``"no-timezone"`` is
    noted when there is neither).
    """
    if text is None:
        return None
    if isinstance(text, datetime):
        value: date | datetime | None = text
    elif isinstance(text, date):
        value = datetime(text.year, text.month, text.day)
    else:
        value = _parse(str(text), dayfirst, now, notes)
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if value.tzinfo is None:
        if tz is not None:
            value = value.replace(tzinfo=tz)
        else:
            _note(notes, "no-timezone")
    return value


_DURATION_ISO = re.compile(
    r"^P(?:(?P<y>\d+(?:\.\d+)?)Y)?(?:(?P<mo>\d+(?:\.\d+)?)M)?(?:(?P<w>\d+(?:\.\d+)?)W)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<mi>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$",
    re.I,
)
_DURATION_PARTS = re.compile(r"(\d+(?:[.,]\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s|days?|d)\b", re.I)


def parse_duration(text: str | None) -> timedelta | None:
    """``"PT1H30M"``, ``"1h 30m"``, ``"90 minutes"``, ``"1:30:00"`` -> :class:`~datetime.timedelta`."""
    if not text:
        return None
    text = text.strip()
    match = _DURATION_ISO.match(text)
    if match and any(match.groupdict().values()):
        g = {k: float(v) for k, v in match.groupdict().items() if v}
        days = g.get("y", 0) * 365.2425 + g.get("mo", 0) * 30.436875 + g.get("w", 0) * 7 + g.get("d", 0)
        return timedelta(days=days, hours=g.get("h", 0), minutes=g.get("mi", 0), seconds=g.get("s", 0))
    clock = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", text)
    if clock:
        h, m, s = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
        return timedelta(hours=h, minutes=m, seconds=s) if clock.group(3) else timedelta(minutes=h, seconds=m)
    total = timedelta()
    found = False
    for number, unit in _DURATION_PARTS.findall(text):
        value = float(number.replace(",", "."))
        unit = unit.lower()
        found = True
        if unit.startswith("d"):
            total += timedelta(days=value)
        elif unit.startswith("h"):
            total += timedelta(hours=value)
        elif unit.startswith("m"):
            total += timedelta(minutes=value)
        else:
            total += timedelta(seconds=value)
    return total if found else None
