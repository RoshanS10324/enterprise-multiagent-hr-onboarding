"""Deterministic UAE business-day arithmetic.

UAE private sector works Monday-Friday with a Saturday/Sunday weekend
(effective 2022). Public holidays are a static synthetic list for the
prototype; review C-7 flags that the live list needs Legal sign-off.

Everything here is pure: no clock reads, no I/O, no network.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from onboarding_task_management.errors import CalendarRangeError

if TYPE_CHECKING:  # avoids a circular import at runtime
    from onboarding_task_management.contracts import CalendarStatus

#: Monday=0 ... Saturday=5, Sunday=6
WEEKEND_WEEKDAYS = frozenset({5, 6})

#: Synthetic/approximate UAE public holidays. Lunar dates are placeholders and
#: are NOT authoritative — see review C-7 before any live use.
UAE_PUBLIC_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day
        date(2026, 3, 19),  # Eid al-Fitr (approx.)
        date(2026, 3, 20),
        date(2026, 3, 21),
        date(2026, 5, 26),  # Arafat Day (approx.)
        date(2026, 5, 27),  # Eid al-Adha (approx.)
        date(2026, 5, 28),
        date(2026, 6, 16),  # Islamic New Year (approx.)
        date(2026, 8, 25),  # Prophet's Birthday (approx.)
        date(2026, 12, 1),  # Commemoration Day
        date(2026, 12, 2),  # National Day
        date(2026, 12, 3),  # National Day
        date(2027, 1, 1),  # New Year's Day
        date(2027, 12, 1),  # Commemoration Day
        date(2027, 12, 2),  # National Day
        date(2027, 12, 3),  # National Day
    }
)


#: Inclusive bounds of the verified holiday list above. Outside this window the
#: list says nothing, so a date there cannot be scheduled correctly (finding L3).
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2027, 12, 31)


#: UC02-BR-06. The built-in list is NOT authoritative. Approved Legal/HR data
#: must be supplied explicitly; until then every due date derived from it is
#: provisional.
BUILTIN_HOLIDAYS_APPROVED = False
BUILTIN_HOLIDAY_SOURCE = (
    "synthetic placeholder - approximate lunar dates, not Legal/HR approved"
)


class WorkingCalendar(frozenset):
    """A client's working calendar: its holidays plus its own weekend.

    A ``frozenset`` of holiday dates, so every existing function that takes a
    ``holidays`` argument accepts it unchanged, carrying the weekday rule and
    coverage window alongside. Nothing here assumes the UAE: ``weekend_weekdays``
    and ``holidays`` are whatever the client's approved calendar says.
    ``coverage_start`` / ``coverage_end`` are optional; when given, any date
    outside the window is refused rather than scheduled against a calendar
    that says nothing about it.
    """

    def __new__(
        cls,
        holidays=(),
        *,
        name: str,
        weekend_weekdays,
        coverage_start: date | None = None,
        coverage_end: date | None = None,
        approved: bool = False,
        source: str = "client-supplied calendar",
    ) -> "WorkingCalendar":
        from onboarding_task_management.errors import CalendarConfigurationError

        if not str(name).strip():
            raise CalendarConfigurationError("calendar must be named")
        weekend = frozenset(weekend_weekdays)
        if not all(isinstance(d, int) and 0 <= d <= 6 for d in weekend):
            raise CalendarConfigurationError(
                "weekend weekdays must be integers 0 (Mon) to 6 (Sun)", name=name
            )
        if len(weekend) >= 7:
            raise CalendarConfigurationError(
                "a calendar with no working weekday cannot schedule anything", name=name
            )
        if (
            coverage_start is not None
            and coverage_end is not None
            and coverage_end < coverage_start
        ):
            raise CalendarConfigurationError(
                "calendar coverage ends before it starts", name=name
            )
        obj = super().__new__(cls, holidays)
        obj.name = str(name)
        obj.weekend_weekdays = weekend
        obj.coverage_start = coverage_start
        obj.coverage_end = coverage_end
        obj.approved = bool(approved)
        obj.source = source
        return obj

    # A plain frozenset subclass would lose the weekend rule the moment it was copied, pickled or combined
    # (``frozenset.__or__`` returns a bare ``frozenset``), and every consumer would then fall back to the
    # Saturday/Sunday default without a word. These make the rule travel with the dates instead.

    def _like(self, holidays) -> "WorkingCalendar":
        return WorkingCalendar(
            holidays,
            name=self.name,
            weekend_weekdays=self.weekend_weekdays,
            coverage_start=self.coverage_start,
            coverage_end=self.coverage_end,
            approved=self.approved,
            source=self.source,
        )

    def __reduce__(self):  # copy.copy / deepcopy / pickle
        return (
            _rebuild_calendar,
            (sorted(self), self.name, sorted(self.weekend_weekdays), self.coverage_start,
             self.coverage_end, self.approved, self.source),
        )

    def copy(self) -> "WorkingCalendar":
        return self._like(frozenset(self))

    def _combine(self, op, *others):
        result = op(frozenset(self), *others)
        return NotImplemented if result is NotImplemented else self._like(result)

    def __or__(self, other):
        return self._combine(frozenset.__or__, other)

    __ror__ = __or__

    def __and__(self, other):
        return self._combine(frozenset.__and__, other)

    __rand__ = __and__

    def __sub__(self, other):
        return self._combine(frozenset.__sub__, other)

    def __rsub__(self, other):
        result = frozenset.__rsub__(frozenset(self), other)
        return NotImplemented if result is NotImplemented else self._like(result)

    def __xor__(self, other):
        return self._combine(frozenset.__xor__, other)

    __rxor__ = __xor__

    def union(self, *others):
        return self._like(frozenset(self).union(*others))

    def intersection(self, *others):
        return self._like(frozenset(self).intersection(*others))

    def difference(self, *others):
        return self._like(frozenset(self).difference(*others))

    def symmetric_difference(self, other):
        return self._like(frozenset(self).symmetric_difference(other))


def _rebuild_calendar(holidays, name, weekend, coverage_start, coverage_end, approved, source):
    return WorkingCalendar(
        holidays, name=name, weekend_weekdays=weekend, coverage_start=coverage_start,
        coverage_end=coverage_end, approved=approved, source=source,
    )


def calendar_status(
    holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS,
    *,
    approved: bool | None = None,
    source: str | None = None,
    coverage_start: date | None = None,
    coverage_end: date | None = None,
) -> "CalendarStatus":
    """Describe the calendar in use, flagging provisional or missing data.

    Callers supplying their own approved holiday set pass ``approved=True``
    with a ``source``; the built-in set always reports ``approved=False``.
    """
    from onboarding_task_management.contracts import CalendarStatus

    if isinstance(holidays, WorkingCalendar):
        approved = holidays.approved if approved is None else approved
        source = source or holidays.source
        coverage_start = coverage_start or holidays.coverage_start
        coverage_end = coverage_end or holidays.coverage_end
    builtin = holidays is UAE_PUBLIC_HOLIDAYS
    if approved is None:
        approved = BUILTIN_HOLIDAYS_APPROVED if builtin else False
    if source is None:
        source = BUILTIN_HOLIDAY_SOURCE if builtin else "caller-supplied holiday set"
    start = coverage_start or (COVERAGE_START if builtin else min(holidays, default=COVERAGE_START))
    end = coverage_end or (COVERAGE_END if builtin else max(holidays, default=COVERAGE_END))

    warnings: list[str] = []
    if not approved:
        warnings.append(
            "holiday data is provisional - due dates are not authoritative "
            "until Legal/HR approved data is supplied"
        )
    if not holidays:
        warnings.append("no public holidays configured")
    return CalendarStatus(
        approved=approved,
        source=source,
        coverage_start=start,
        coverage_end=end,
        holiday_count=len(holidays),
        warnings=warnings,
    )


def is_covered(day: date) -> bool:
    """Whether ``day`` falls inside the verified holiday calendar."""
    return COVERAGE_START <= day <= COVERAGE_END


def assert_covered(day: date, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS) -> None:
    """Reject dates the holiday list does not cover.

    Only enforced for the built-in UAE list. A caller supplying their own
    holiday set owns its coverage, so validation is skipped for custom sets.
    """
    if isinstance(holidays, WorkingCalendar):
        lo, hi = holidays.coverage_start, holidays.coverage_end
        if (lo is not None and day < lo) or (hi is not None and day > hi):
            from onboarding_task_management.errors import CalendarRangeError as _Range

            raise _Range(
                f"date falls outside the {holidays.name} calendar coverage",
                day=day.isoformat(),
                coverage_start=lo.isoformat() if lo else "",
                coverage_end=hi.isoformat() if hi else "",
            )
        return
    if holidays is not UAE_PUBLIC_HOLIDAYS:
        return
    if not is_covered(day):
        raise CalendarRangeError(
            "date falls outside the verified UAE holiday calendar",
            day=day.isoformat(),
            coverage_start=COVERAGE_START.isoformat(),
            coverage_end=COVERAGE_END.isoformat(),
        )


def is_business_day(day: date, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS) -> bool:
    """True when ``day`` is a working day in the UAE private sector."""
    weekend = getattr(holidays, "weekend_weekdays", WEEKEND_WEEKDAYS)
    return day.weekday() not in weekend and day not in holidays


def next_business_day(
    day: date, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS
) -> date:
    """The first business day on or after ``day``."""
    assert_covered(day, holidays)
    cursor = day
    while not is_business_day(cursor, holidays):
        cursor += timedelta(days=1)
    assert_covered(cursor, holidays)
    return cursor


def previous_business_day(
    day: date, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS
) -> date:
    """The first business day on or before ``day``."""
    assert_covered(day, holidays)
    cursor = day
    while not is_business_day(cursor, holidays):
        cursor -= timedelta(days=1)
    assert_covered(cursor, holidays)
    return cursor


def add_business_days(
    start: date, offset: int, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS
) -> date:
    """Shift ``start`` by ``offset`` business days.

    ``offset`` may be negative (before ``start``). An ``offset`` of 0 snaps to
    the nearest business day on or after ``start``, so a due date never lands on
    a weekend or public holiday.
    """
    assert_covered(start, holidays)
    if offset == 0:
        return next_business_day(start, holidays)

    step = timedelta(days=1 if offset > 0 else -1)
    remaining = abs(offset)
    cursor = start
    while remaining > 0:
        cursor += step
        if is_business_day(cursor, holidays):
            remaining -= 1
    # The walk can leave the covered window even when the start date was inside
    # it, so the result is validated too.
    assert_covered(cursor, holidays)
    return cursor


def business_days_between(
    start: date, end: date, holidays: frozenset[date] = UAE_PUBLIC_HOLIDAYS
) -> int:
    """Count business days in ``(start, end]``.

    Negative when ``end`` precedes ``start``. Used for "days to joining" and for
    overdue measurement, so the sign convention matters: a result of ``-3`` means
    ``end`` is three business days in the past relative to ``start``.
    """
    assert_covered(start, holidays)
    assert_covered(end, holidays)
    if start == end:
        return 0
    sign = 1 if end > start else -1
    lo, hi = (start, end) if sign == 1 else (end, start)
    count = 0
    cursor = lo
    while cursor < hi:
        cursor += timedelta(days=1)
        if is_business_day(cursor, holidays):
            count += 1
    return count * sign
