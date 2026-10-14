import json
from datetime import date

from easytop.family.calendar import SchoolCalendar, load_ministry_vacations


def test_regular_weeknight_goes_to_next_day():
    cal = SchoolCalendar()
    # Mon 2026-10-05 (no holiday: after Succos 2026-10-03 Sat) -> Tue
    d = cal.next_school_day(date(2026, 10, 5))
    assert d.day == date(2026, 10, 6)
    assert d.skipped == []


def test_thursday_skips_friday_saturday():
    cal = SchoolCalendar()
    d = cal.next_school_day(date(2026, 10, 8))  # Thu
    assert d.day == date(2026, 10, 11)  # Sun
    assert [s for _, s in d.skipped] == ["Friday", "Saturday"]


def test_sunday_to_thursday_are_school_days():
    cal = SchoolCalendar()
    assert cal.is_school_day(date(2026, 10, 11))  # Sun
    assert cal.is_school_day(date(2026, 10, 15))  # Thu
    assert not cal.is_school_day(date(2026, 10, 9))  # Fri
    assert not cal.is_school_day(date(2026, 10, 10))  # Sat


def test_holiday_skipped_with_reason():
    cal = SchoolCalendar()
    # Rosh Hashana 2026: Sat 12 + Sun 13 Sept. Thu 2026-09-10 -> next is Mon 14 Sept
    d = cal.next_school_day(date(2026, 9, 10))
    assert d.day == date(2026, 9, 14)
    reasons = dict(d.skipped)
    assert reasons[date(2026, 9, 13)] == "Rosh Hashana"


def test_yom_kippur_and_sukkot_week_off():
    cal = SchoolCalendar()
    assert not cal.is_school_day(date(2026, 9, 21))  # Yom Kippur (Mon)
    assert not cal.is_school_day(date(2026, 9, 29))  # Sukkot intermediate (Tue)


def test_ministry_vacation_ranges(tmp_path):
    f = tmp_path / "v.json"
    f.write_text(
        json.dumps(
            {
                "status": "UNVERIFIED",
                "vacations": [{"from": "2026-11-01", "to": "2026-11-03", "name": "Test break"}],
            }
        )
    )
    cal = SchoolCalendar(vacations=load_ministry_vacations(f))
    assert not cal.is_school_day(date(2026, 11, 2))
    d = cal.next_school_day(
        date(2026, 10, 29)
    )  # Thu -> skip Fri Sat, then Sun..Tue vacation -> Wed
    assert d.day == date(2026, 11, 4)


def test_packaged_vacations_file_is_unverified_and_empty():
    data = load_ministry_vacations()
    assert data == []


# ---- the school's own off days from Webtop (inactive weekdays + vacations) ----


def webtop_off(inactive=(5,), vacations=()):
    from easytop.models import DaysOff, Vacation

    return DaysOff(frozenset(inactive), tuple(Vacation(*v) for v in vacations))


def test_friday_is_a_school_day_when_webtop_says_only_saturday_is_inactive():
    cal = SchoolCalendar()
    off = webtop_off(inactive={5})
    assert cal.is_school_day(date(2026, 10, 9), off)  # Friday
    assert not cal.is_school_day(date(2026, 10, 10), off)  # Saturday
    d = cal.next_school_day(date(2026, 10, 8), off)  # Thursday evening -> Friday
    assert d.day == date(2026, 10, 9) and d.skipped == []


def test_webtop_vacation_range_is_skipped_with_its_name():
    cal = SchoolCalendar()
    off = webtop_off(vacations=[(date(2026, 10, 11), date(2026, 10, 13), "חופשה")])
    d = cal.next_school_day(date(2026, 10, 8), off)  # Fri 9 is school, so tomorrow is Fri
    assert d.day == date(2026, 10, 9)
    d = cal.next_school_day(date(2026, 10, 9), off)  # Fri evening: Sat off, Sun-Tue vacation
    assert d.day == date(2026, 10, 14)
    assert dict(d.skipped)[date(2026, 10, 12)] == "חופשה"


def test_without_webtop_data_the_default_fri_sat_week_still_applies():
    cal = SchoolCalendar()
    assert not cal.is_school_day(date(2026, 10, 9))  # Friday, no days_off given
