from datetime import time

import pytest

from easytop.digest.nudge import ASK, DECLINED, NUDGE, OFF, OFFER, ON, parse_command, parse_time


@pytest.mark.parametrize(
    "text,expected",
    [
        ("19:00", time(19)),
        (" 7:30 ", time(7, 30)),
        ("19.00", time(19)),
        ("25:00", None),
        ("19:75", None),
        ("מחר", None),
        ("19", None),
    ],
)
def test_parse_time(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("תזכורת 20:00", time(20)),
        ("Reminder 20:00", time(20)),
        ("תזכורת כבוי", "off"),
        ("reminder off", "off"),
        ("Reminder OFF.", "off"),
        ("מה מחר?", None),
        ("תזכורת למחר מה צריך", None),
        ("reminder", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8pm", time(20)),
        ("8 PM", time(20)),
        ("8:30pm", time(20, 30)),
        ("12am", time(0)),
        ("12pm", time(12)),
        ("8 בערב", time(20)),
        ("13pm", None),
        ("0pm", None),
    ],
)
def test_parse_time_reads_am_pm_and_hebrew_evening(text, expected):
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        # a free-language request with a time sets it
        ("set a reminder for 20:00", time(20)),
        ("Set a reminder at 8pm", time(20)),
        ("please create a daily reminder for 19:30", time(19, 30)),
        ("remind me at 20:00", time(20)),
        ("remind me every evening at 19:30", time(19, 30)),
        ("reminder at 20:00", time(20)),
        ("change the reminder to 21:00", time(21)),
        ("תזכורת ב-20:00", time(20)),
        ("תזכורת ב־20:00", time(20)),
        ("תגדיר תזכורת ל־20:00", time(20)),
        ("תקבע לי תזכורת לשעה 19:30", time(19, 30)),
        ("תזכיר לי ב־20:00", time(20)),
        ("תזכיר לי כל ערב ב-19:00", time(19)),
        ("אני רוצה תזכורת ב-20:00", time(20)),
        # a request without a usable time asks for one
        ("create a reminder", "ask"),
        ("Create a reminder.", "ask"),
        ("set up a reminder please", "ask"),
        ("I want a reminder", "ask"),
        ("remind me", "ask"),
        ("set a reminder for 8", "ask"),  # a bare hour is ambiguous
        ("set a reminder for 25:00", "ask"),
        ("תוסיף תזכורת", "ask"),
        ("תגדיר לי תזכורת", "ask"),
        ("אני רוצה תזכורת", "ask"),
        ("תזכיר לי", "ask"),
        # turning it off
        ("turn off the reminder", "off"),
        ("stop the reminders", "off"),
        ("please cancel my reminder", "off"),
        ("no reminders", "off"),
        ("בטל תזכורת", "off"),
        ("תכבה את התזכורת", "off"),
        ("תפסיק את התזכורות", "off"),
        ("בלי תזכורת", "off"),
        # a question or a message that merely contains the word is for the agent
        ("any reminders from school?", None),
        ("what is the reminder the teacher sent?", None),
        ("remind me to bring the ruler", None),
        ("reminder for the trip tomorrow", None),
        ("יש תזכורת מהמורה?", None),
        ("תזכורת: מחר טיול", None),
        ("מה התזכורת שהמורה שלחה", None),
    ],
)
def test_parse_command_understands_free_language_requests(text, expected):
    assert parse_command(text) == expected


def test_the_nudge_carries_no_children_data_and_every_text_is_hebrew_first_with_english():
    for text in (OFFER, ASK, ON, OFF, DECLINED, NUDGE):
        assert any("֐" <= ch <= "׿" for ch in text.split("\n\n")[0])
        assert text.count("\n\n") >= 1  # Hebrew paragraph, then one English paragraph
    assert "{time}" in ON
    assert "{time}" not in ON.format(time="19:00")
