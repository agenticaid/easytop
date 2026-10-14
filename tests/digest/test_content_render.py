"""The content reminder is written by code, never by the model (Erez 2026-10-13): short Hebrew lines per child."""

from easytop.agent.guardrails import MAX_ANSWER_CHARS
from easytop.digest.content import EMPTY_ALL, EMPTY_DUE, render_reminder

BRIEF = {
    "date": "2026-10-14",
    "weekday": "Wednesday",
    "kids": [
        {
            "id": "k1",
            "name": "ענבר",
            "class": "א׳2",
            "ok": True,
            "homework_due": [{"subject": "עברית", "description": "עמ׳ 44"}],
            "upcoming_exams": [
                {"subject": "אנענברת", "date": "2026-10-14", "topic": "יחידה 1"},
                {"subject": "חשבון", "date": "2026-10-19"},  # a later exam: only in the full level
            ],
            "homework_upcoming": [
                {"subject": "מדעים", "description": "סיכום פרק 3", "due": "2026-10-15"}
            ],
            "lessons": [
                {"period": 1, "subject": "אנענברת", "note": "ספר ומחברת"},
                {"period": 2, "subject": "אמנות"},  # no note: not listed
                {"day_opener": True, "subject": "פתיחה", "note": "להביא סוודר"},
            ],
            "changes": [{"period": 3, "subject": "מוסיקה", "status": "cancelled", "note": "מורה חולה"}],
            "calendar": [{"kind": "calendar", "date": "2026-10-14", "title": "טיול", "detail": "מים"}],
        },
        {  # nothing due tomorrow: left out of the short level
            "id": "k2",
            "name": "טליה",
            "class": "א׳1",
            "ok": True,
            "homework_due": [],
            "upcoming_exams": [],
            "homework_upcoming": [],
            "lessons": [],
            "changes": [],
        },
        {"id": "k3", "name": "אפרת", "class": "ה׳1", "ok": False, "error": "upstream"},
    ],
}


def test_the_none_level_has_no_text():
    assert render_reminder(BRIEF, "none") is None


def test_due_lists_only_what_is_due_tomorrow_per_child():
    text = render_reminder(BRIEF, "due")
    assert text.splitlines()[0] == "🌙 מחר, יום ד׳ 14.10"
    assert "**__ענבר__** (א׳2)" in text
    assert "📚 עברית — עמ׳ 44" in text
    assert "📝 מבחן אנענברת — יחידה 1" in text
    for left_out in ("חשבון", "מדעים", "ספר ומחברת", "מוסיקה", "טיול", "טליה"):
        assert left_out not in text
    assert "⚠️ **__אפרת__**: הוובטופ לא ענה" in text  # a school that did not answer is named, never hidden


def test_all_adds_the_rest_of_the_day_written_from_the_data():
    text = render_reminder(BRIEF, "all")
    assert "📚 עברית — עמ׳ 44" in text
    assert "📚 מדעים — סיכום פרק 3 (להגשה ה׳ 15.10)" in text
    assert "📝 מבחן חשבון ב־19.10" in text
    assert "🎒 שעה 1 · אנענברת — ספר ומחברת" in text
    assert "🎒 פתיחת היום — להביא סוודר" in text
    assert "⚠️ מוסיקה בוטל — מורה חולה" in text
    assert "🗓️ טיול — מים" in text
    assert "אמנות" not in text  # a lesson with no note says nothing


def test_nothing_due_says_not_found_in_one_line():
    empty = {"date": "2026-10-14", "weekday": "Wednesday", "kids": [BRIEF["kids"][1]]}
    assert render_reminder(empty, "due") == f"🌙 מחר, יום ד׳ 14.10\n\n{EMPTY_DUE}"
    assert render_reminder(empty, "all") == f"🌙 מחר, יום ד׳ 14.10\n\n{EMPTY_ALL}"
    assert "לא נמצא" in EMPTY_DUE and "לא נמצא" in EMPTY_ALL  # not found, never "there is nothing"


def test_a_teachers_note_with_line_breaks_stays_on_one_line():
    kid = {
        **BRIEF["kids"][0],
        "homework_due": [{"subject": "עברית", "description": "עמ׳ 44\n\nלהביא   מחברת"}],
        "lessons": [{"period": 1, "subject": "אנענברת", "note": "הבנת הנקרא\n\nציוד: מחברת\n"}],
    }
    text = render_reminder({**BRIEF, "kids": [kid]}, "all")
    assert "📚 עברית — עמ׳ 44 להביא מחברת" in text
    assert "🎒 שעה 1 · אנענברת — הבנת הנקרא ציוד: מחברת" in text


def test_a_kid_whose_next_school_day_differs_carries_their_own_date():
    brief = {**BRIEF, "kids": [{**BRIEF["kids"][0], "date": "2026-10-15", "weekday": "Thursday"}]}
    assert "**__ענבר__** (א׳2) — יום ה׳ 15.10" in render_reminder(brief, "due")


def test_a_very_long_day_is_cut_to_one_telegram_message():
    big = {
        **BRIEF,
        "kids": [{**BRIEF["kids"][0], "homework_due": [{"subject": f"מקצוע{i}", "description": "ת" * 200} for i in range(60)]}],
    }
    assert len(render_reminder(big, "due")) <= MAX_ANSWER_CHARS
