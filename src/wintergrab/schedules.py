"""When jobs run: cron expressions, intervals, times of day, and one-off times.

::

    >>> schedule = parse_schedule("every 2 hours")
    >>> schedule.next(after=datetime(2026, 9, 26, 9, 30), last=datetime(2026, 9, 26, 8, 0))
    datetime.datetime(2026, 9, 26, 10, 0)
    >>> parse_schedule("daily at 06:00").next(after=datetime(2026, 9, 26, 9, 30))
    datetime.datetime(2026, 9, 27, 6, 0)

What a schedule can say:

* a cron expression, five fields (minute, hour, day of the month, month, day of the week):
  ``"*/15 * * * *"``, ``"0 6 * * mon-fri"``, ``"30 2 1 * *"``; lists, ranges, steps and names
  (``jan``, ``mon``) as in cron; when both days are given, either matches (as in cron);
* an interval: ``"every 30 minutes"``, ``"every 2 hours"``, ``"every day"``, ``"every 15m"``, ``"hourly"``,
  ``"daily"``, ``"weekly"``: counted from the last run (the first run is right away);
* a time of day: ``"daily at 06:00"``, ``"at 18:30"``, ``"weekly on monday at 06:00"``,
  ``"every monday at 7:15"``;
* one time: ``"once at 2026-10-01 06:00"`` (ISO 8601; runs once, then never).

Times are in the machine's local time zone, or in ``timezone`` (``"Europe/Berlin"``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .errors import ConfigurationError

__all__ = ["Cron", "Interval", "Once", "Schedule", "parse_duration", "parse_schedule"]

_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}  # noqa: SIM905
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}  # noqa: SIM905
_DAY_NAMES = {name: i for i, name in enumerate(
    ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"))}  # fmt: skip
_UNITS = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hour": 3600, "d": 86400,
          "day": 86400, "w": 604800, "week": 604800}  # fmt: skip


class Schedule:
    """When a job runs (see the module docs)."""

    text: str  # what it was made from

    def next(self, after: datetime, last: datetime | None = None) -> datetime | None:
        """When the job is due next, from ``after`` on (``after`` itself: due now); ``last``: when it
        last ran. ``None`` when it never is again."""
        raise NotImplementedError

    def __str__(self) -> str:
        return self.text


def _field(text: str, low: int, high: int, names: dict[str, int] | None = None) -> frozenset[int]:
    values: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"bad step {step_text!r}")
            step = int(step_text)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = _value(a, names), _value(b, names)
        else:
            start = _value(part, names)
            end = high if step > 1 else start
        if not low <= start <= high or not low <= end <= high or start > end:
            raise ValueError(f"{part!r} is out of {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def _value(text: str, names: dict[str, int] | None) -> int:
    if text.isdigit():
        return int(text)
    if names and text[:3] in names:
        return names[text[:3]]
    raise ValueError(f"{text!r} is not a number")


@dataclass
class Cron(Schedule):
    """A cron expression: minute, hour, day of the month, month, day of the week."""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool
    zone: tzinfo | None = None

    @classmethod
    def parse(cls, text: str, zone: tzinfo | None = None) -> Cron:
        parts = text.split()
        if len(parts) != 5:
            raise ConfigurationError(f"a cron expression has 5 fields (minute hour day month weekday): {text!r}")
        try:
            weekdays = frozenset(d % 7 for d in _field(parts[4], 0, 7, _DAYS))  # 0 and 7: Sunday
            return cls(
                text=text,
                minutes=_field(parts[0], 0, 59),
                hours=_field(parts[1], 0, 23),
                days=_field(parts[2], 1, 31),
                months=_field(parts[3], 1, 12, _MONTHS),
                weekdays=weekdays,
                any_day=parts[2] == "*",
                any_weekday=parts[4] == "*",
                zone=zone,
            )
        except ValueError as exc:
            raise ConfigurationError(f"cron expression {text!r}: {exc}") from exc

    def _day_matches(self, moment: datetime) -> bool:
        day = moment.day in self.days
        weekday = (moment.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return weekday
        if self.any_weekday:
            return day
        return day or weekday  # both given: either (as in cron)

    def next(self, after: datetime, last: datetime | None = None) -> datetime | None:
        moment = _local(after, self.zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = moment + timedelta(days=366 * 5)
        while moment < limit:
            if moment.month not in self.months:
                moment = (moment.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_matches(moment):
                moment = moment.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if moment.hour not in self.hours:
                moment = moment.replace(minute=0) + timedelta(hours=1)
                continue
            if moment.minute not in self.minutes:
                moment += timedelta(minutes=1)
                continue
            return _back(moment, after)
        return None  # "30 2 31 2 *": never


@dataclass
class Interval(Schedule):
    """Every so many seconds, counted from the last run (the first run is right away)."""

    text: str
    seconds: float

    def next(self, after: datetime, last: datetime | None = None) -> datetime | None:
        if last is None:
            return after
        due = last + timedelta(seconds=self.seconds)
        return due if due > after else after


@dataclass
class Once(Schedule):
    """One time."""

    text: str
    at: datetime

    def next(self, after: datetime, last: datetime | None = None) -> datetime | None:
        if last is not None:
            return None  # done
        at = _back(self.at, after)
        return at if at > after else after  # its time went by while nothing ran: now


def _local(moment: datetime, zone: tzinfo | None) -> datetime:
    """``moment`` in ``zone`` (naive when both are), for calendar arithmetic."""
    if zone is None:
        return moment
    return (moment if moment.tzinfo else moment.astimezone()).astimezone(zone)


def _back(moment: datetime, like: datetime) -> datetime:
    """``moment``, aware or naive as ``like`` is."""
    if like.tzinfo is None and moment.tzinfo is not None:
        return moment.astimezone().replace(tzinfo=None)
    if like.tzinfo is not None and moment.tzinfo is None:
        return moment.astimezone(like.tzinfo)
    return moment


_EVERY = re.compile(r"^every\s+(\d+(?:\.\d+)?)\s*([a-z]+?)s?$")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([a-z]+?)s?$")
_EVERY_ONE = re.compile(r"^every\s+(second|minute|hour|day|week)$")
_AT = re.compile(r"^(?:(daily|every\s+day)\s+)?at\s+(\d{1,2}):(\d{2})$")
_WEEKLY = re.compile(r"^(?:weekly\s+on|every)\s+([a-z]+)\s+at\s+(\d{1,2}):(\d{2})$")
_ONCE = re.compile(r"^once\s+at\s+(.+)$")


def parse_schedule(text: str | int | float, *, timezone: str | tzinfo | None = None) -> Schedule:
    """A :class:`Schedule` from its text (see the module docs), or a number of seconds between runs."""
    zone = _zone(timezone)
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        if text <= 0:
            raise ConfigurationError(f"an interval must be positive, not {text}")
        return Interval(f"every {text:g} seconds", float(text))
    if not isinstance(text, str) or not text.strip():
        raise ConfigurationError(f"a schedule is a text (a cron expression, 'every 2 hours'...), not {text!r}")
    clean = " ".join(text.lower().split())
    words = {"hourly": 3600, "daily": 86400, "weekly": 604800}
    if clean in words:
        return Interval(text, words[clean])
    match = _EVERY_ONE.match(clean)
    if match:
        return Interval(text, _UNITS[match.group(1)])
    match = _EVERY.match(clean)
    if match and match.group(2) in _UNITS:
        seconds = float(match.group(1)) * _UNITS[match.group(2)]
        if seconds <= 0:
            raise ConfigurationError(f"schedule {text!r}: an interval must be positive")
        return Interval(text, seconds)
    match = _AT.match(clean)
    if match:
        return _at(text, int(match.group(2)), int(match.group(3)), "*", zone)
    match = _WEEKLY.match(clean)
    if match and match.group(1) in _DAY_NAMES:
        return _at(text, int(match.group(2)), int(match.group(3)), str(_DAY_NAMES[match.group(1)]), zone)
    match = _ONCE.match(clean)
    if match:
        try:
            at = datetime.fromisoformat(text.strip()[len("once at") :].strip())
        except ValueError as exc:
            raise ConfigurationError(f"schedule {text!r}: {exc} (use ISO 8601: 2026-10-01 06:00)") from exc
        if at.tzinfo is None and zone is not None:
            at = at.replace(tzinfo=zone)
        return Once(text, at)
    if len(clean.split()) == 5:
        return Cron.parse(clean, zone)
    raise ConfigurationError(
        f"cannot read the schedule {text!r}: use a cron expression ('0 */2 * * *'), 'every 2 hours', "
        "'daily at 06:00', 'weekly on monday at 06:00' or 'once at 2026-10-01 06:00'"
    )


def parse_duration(text: str | int | float) -> timedelta:
    """A duration: ``"30 minutes"``, ``"2h"``, ``"1 day"``, or a number of seconds."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        seconds = float(text)
    else:
        match = _DURATION.match(" ".join(str(text).lower().split()))
        if not match or match.group(2) not in _UNITS:
            raise ConfigurationError(f"cannot read the duration {text!r} (like '30 minutes', '2 hours', '1 day')")
        seconds = float(match.group(1)) * _UNITS[match.group(2)]
    if seconds <= 0:
        raise ConfigurationError(f"a duration must be positive, not {text!r}")
    return timedelta(seconds=seconds)


def _at(text: str, hour: int, minute: int, weekday: str, zone: tzinfo | None) -> Cron:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigurationError(f"schedule {text!r}: {hour}:{minute:02d} is not a time of day")
    cron = Cron.parse(f"{minute} {hour} * * {weekday}", zone)
    cron.text = text
    return cron


def _zone(timezone: Any) -> tzinfo | None:
    if timezone is None or isinstance(timezone, tzinfo):
        return timezone
    from zoneinfo import ZoneInfo, available_timezones

    try:
        return ZoneInfo(str(timezone))
    except Exception as exc:  # ZoneInfoNotFoundError, a bad key
        if not available_timezones():  # no time zone database at all (Windows without tzdata)
            raise ConfigurationError(
                f"time zone {timezone!r}: this Python has no time zone database (pip install tzdata)"
            ) from exc
        raise ConfigurationError(f"unknown time zone {timezone!r}") from exc
