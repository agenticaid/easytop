"""Next-school-day logic.

School week: Sunday–Thursday (Fri/Sat off). Jewish holidays come from pyluach, never hardcoded.
Ministry of Education vacations come from a data file that is UNVERIFIED (empty until a human fills it).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from pyluach import dates

from easytop.models import DaysOff, Vacation

VACATIONS_FILE = Path(__file__).parent / "data" / "ministry_vacations.json"
# Festivals whose intermediate (working) days are school vacation in practice.
_VACATION_FESTIVALS = {"Succos", "Pesach"}
_WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_OFF_WEEKDAYS = {4, 5}  # Python weekday(): Fri=4, Sat=5


def load_ministry_vacations(path: Path = VACATIONS_FILE) -> list[Vacation]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [
        Vacation(
            date.fromisoformat(v["from"]), date.fromisoformat(v["to"]), v.get("name", "Vacation")
        )
        for v in raw.get("vacations", [])
    ]


@dataclass(frozen=True)
class SchoolDay:
    day: date
    skipped: list[tuple[date, str]] = field(default_factory=list)


class SchoolCalendar:
    def __init__(self, vacations: list[Vacation] | None = None):
        self.vacations = vacations if vacations is not None else load_ministry_vacations()

    def off_reason(self, day: date, days_off: DaysOff | None = None) -> str | None:
        """Why school is closed on `day`, or None if it is a school day.

        With `days_off` (the school's own data from Webtop) that data decides the weekly pattern and the
        vacations; real festival days (yom tov) still count as a safety net. Without it the default
        applies: Fri/Sat off, festivals, intermediate days of Succos/Pesach and the Ministry file."""
        g = dates.GregorianDate(day.year, day.month, day.day)
        yom_tov = g.festival(israel=True, include_working_days=False)
        if days_off is not None:
            if day.weekday() in days_off.inactive_weekdays:
                return _WEEKDAY_NAMES[day.weekday()]
            for v in days_off.vacations:
                if v.start <= day <= v.end:
                    return v.name
            return yom_tov or None
        if day.weekday() in _OFF_WEEKDAYS:
            return _WEEKDAY_NAMES[day.weekday()]
        if yom_tov:
            return yom_tov
        any_fest = g.festival(israel=True)
        if any_fest in _VACATION_FESTIVALS:
            return f"{any_fest} (intermediate days)"
        for v in self.vacations:
            if v.start <= day <= v.end:
                return v.name
        return None

    def is_school_day(self, day: date, days_off: DaysOff | None = None) -> bool:
        return self.off_reason(day, days_off) is None

    def next_school_day(
        self, today: date, days_off: DaysOff | None = None, limit: int = 60
    ) -> SchoolDay:
        skipped: list[tuple[date, str]] = []
        d = today
        for _ in range(limit):
            d += timedelta(days=1)
            reason = self.off_reason(d, days_off)
            if reason is None:
                return SchoolDay(d, skipped)
            skipped.append((d, reason))
        raise RuntimeError(f"no school day within {limit} days of {today}")
