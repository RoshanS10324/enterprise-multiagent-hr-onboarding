"""Working-day arithmetic from an explicitly configured calendar. There is no built-in default calendar (BR-20)."""

from datetime import date, timedelta

from sub_agent.contracts import WorkCalendar


def is_working_day(d: date, cal: WorkCalendar) -> bool:
    return d.weekday() not in cal.weekend_days and d not in set(cal.holidays)


def subtract_working_days(d: date, n: int, cal: WorkCalendar) -> date:
    current, counted = d, 0
    while counted < n:
        current -= timedelta(days=1)
        if is_working_day(current, cal):
            counted += 1
    return current


def next_working_day(d: date, cal: WorkCalendar) -> date:
    current = d + timedelta(days=1)
    while not is_working_day(current, cal):
        current += timedelta(days=1)
    return current
