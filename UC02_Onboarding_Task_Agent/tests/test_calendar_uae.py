"""UAE business-day arithmetic."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from onboarding_task_management.calendar_uae import (
    UAE_PUBLIC_HOLIDAYS,
    add_business_days,
    business_days_between,
    is_business_day,
    next_business_day,
    previous_business_day,
)


class CalendarTests(unittest.TestCase):
    def test_weekend_is_not_a_business_day(self) -> None:
        saturday = date(2026, 11, 14)
        sunday = date(2026, 11, 15)
        self.assertEqual(saturday.weekday(), 5)
        self.assertEqual(sunday.weekday(), 6)
        self.assertFalse(is_business_day(saturday))
        self.assertFalse(is_business_day(sunday))

    def test_weekday_is_a_business_day(self) -> None:
        monday = date(2026, 11, 16)
        self.assertEqual(monday.weekday(), 0)
        self.assertTrue(is_business_day(monday))

    def test_public_holiday_is_not_a_business_day(self) -> None:
        national_day = date(2026, 12, 2)
        self.assertIn(national_day, UAE_PUBLIC_HOLIDAYS)
        self.assertFalse(is_business_day(national_day))

    def test_zero_offset_snaps_forward_to_a_business_day(self) -> None:
        saturday = date(2026, 11, 14)
        self.assertEqual(add_business_days(saturday, 0), date(2026, 11, 16))
        # Already a business day: unchanged.
        monday = date(2026, 11, 16)
        self.assertEqual(add_business_days(monday, 0), monday)

    def test_negative_offset_skips_weekends(self) -> None:
        # Ten business days before Monday 2026-11-16, no holidays in range.
        self.assertEqual(
            add_business_days(date(2026, 11, 16), -10), date(2026, 11, 2)
        )

    def test_positive_offset_skips_weekends(self) -> None:
        self.assertEqual(add_business_days(date(2026, 11, 16), 2), date(2026, 11, 18))

    def test_offset_skips_public_holidays(self) -> None:
        # 2026-12-01..03 are holidays, so one business day after Mon 2026-11-30
        # lands on Friday 2026-12-04.
        self.assertEqual(add_business_days(date(2026, 11, 30), 1), date(2026, 12, 4))

    def test_add_business_days_always_lands_on_a_business_day(self) -> None:
        start = date(2026, 11, 2)
        for offset in range(-30, 31):
            self.assertTrue(
                is_business_day(add_business_days(start, offset)),
                msg=f"offset {offset} landed on a non-business day",
            )

    def test_next_and_previous_business_day(self) -> None:
        saturday = date(2026, 11, 14)
        self.assertEqual(next_business_day(saturday), date(2026, 11, 16))
        self.assertEqual(previous_business_day(saturday), date(2026, 11, 13))

    def test_business_days_between_is_signed(self) -> None:
        earlier = date(2026, 11, 2)
        later = date(2026, 11, 16)
        self.assertEqual(business_days_between(earlier, later), 10)
        self.assertEqual(business_days_between(later, earlier), -10)
        self.assertEqual(business_days_between(earlier, earlier), 0)

    def test_business_days_between_inverts_add_business_days(self) -> None:
        start = date(2026, 11, 2)
        for offset in range(1, 25):
            target = add_business_days(start, offset)
            self.assertEqual(business_days_between(start, target), offset)

    def test_business_days_between_ignores_weekend_only_spans(self) -> None:
        friday = date(2026, 11, 13)
        sunday = date(2026, 11, 15)
        self.assertEqual(business_days_between(friday, sunday), 0)

    def test_holidays_are_configurable(self) -> None:
        custom = frozenset({date(2026, 11, 17)})
        self.assertFalse(is_business_day(date(2026, 11, 17), custom))
        self.assertEqual(
            add_business_days(date(2026, 11, 16), 1, custom), date(2026, 11, 18)
        )

    def test_span_covering_weekend_counts_only_weekdays(self) -> None:
        start = date(2026, 11, 13)  # Friday
        end = start + timedelta(days=3)  # Monday
        self.assertEqual(business_days_between(start, end), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
