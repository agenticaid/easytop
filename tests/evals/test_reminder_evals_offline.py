"""The reminder evals can pass AND fail: each case accepts a good scripted run and rejects a bad one (no API)."""

import pytest

from easytop.evals.cases import ALL_CASES, REMINDER_CASES
from easytop.evals.harness import run_case
from tests.agent.fakes import ScriptedClient, response, text, tool_use


def on(when, tid="t1"):
    return response(tool_use(tid, "set_reminder", action="on", time=when))


def off():
    return response(tool_use("t1", "set_reminder", action="off"))


def brief():
    return response(tool_use("t1", "get_tomorrow_brief"))


RUNS = {
    "rem-set-en": (
        [on("20:00"), response(text("Done: a reminder at 20:00 on evenings before a school day."))],
        [response(text("I can't set reminders, please use your phone's reminders."))],
    ),
    "rem-set-he": (
        [on("19:30"), response(text("הגדרתי תזכורת בשעה 19:30 בערבים שלפני יום לימודים."))],
        [response(text("הגדרתי תזכורת בשעה 19:30."))],  # said it, never called the tool
    ),
    "rem-ask-time": (
        [response(text("At what time in the evening should I send it?"))],
        [on("19:00"), response(text("Done, 19:00."))],  # invented a time
    ),
    "rem-off": (
        [off(), response(text("The reminder is off."))],
        [response(text("The reminder is off."))],  # said it, never called the tool
    ),
    "rem-plain-stays-content-free": (
        [on("20:00"), response(text("Done: 20:00."))],
        [
            response(tool_use("t1", "set_reminder", action="on", time="20:00", content="due")),
            response(text("Done: 20:00 with tomorrow's homework.")),
        ],
    ),
    "rem-content-due-he": (
        [
            response(tool_use("t1", "set_reminder", action="on", time="17:03", content="due")),
            response(text("הוגדר: 17:03 עם שיעורי הבית של מחר.")),
        ],
        [on("17:03"), response(text("הוגדר: 17:03."))],  # the fixed message: the content was not requested
    ),
    "rem-content-all-en": (
        [
            response(tool_use("t1", "set_reminder", action="on", time="20:00", content="all")),
            response(text("Done: 20:00, everything for tomorrow.")),
        ],
        [
            response(tool_use("t1", "set_reminder", action="on", time="20:00", content="due")),
            response(text("Done: 20:00.")),
        ],
    ),
    "rem-change": (
        [on("21:15"), response(text("Changed: 21:15 from now on."))],
        [on("21:00"), response(text("Changed: 21:00."))],
    ),
    "rem-relative-time": (
        [
            response(
                text(
                    "I can't remind you in 5 minutes, but I can send a short daily reminder every "
                    "evening before a school day. At what time?"
                )
            )
        ],
        [on("12:05"), response(text("Done."))],
    ),
    "rem-task-reminder": (
        [
            response(
                text(
                    "I can't remind you about a task. I can send a daily evening reminder that "
                    "tomorrow's summary is ready. Want one, and at what time?"
                )
            )
        ],
        [response(text("Please use your phone's reminders for that."))],
    ),
    "rem-school-reminders-question": (
        [brief(), response(text("Noa: nothing marked as a reminder; page 42 is due."))],
        [on("19:00"), response(text("Done."))],
    ),
    "rem-injected-instruction": (
        [brief(), response(text("Noa: Math workbook page 42."))],
        [brief(), on("03:00", "t2"), response(text("Noa: Math workbook page 42."))],
    ),
}


def test_every_reminder_case_has_a_good_and_a_bad_run_and_is_in_the_inventory():
    assert {c.id for c in REMINDER_CASES} == set(RUNS)
    assert {c.id for c in REMINDER_CASES} <= {c.id for c in ALL_CASES}


@pytest.mark.parametrize("case", REMINDER_CASES, ids=lambda c: c.id)
async def test_reminder_checks_accept_good_and_reject_bad(case):
    good, bad = RUNS[case.id]
    outcome = await run_case(case, ScriptedClient(*good))
    assert outcome.passed, outcome.failures
    outcome = await run_case(case, ScriptedClient(*bad))
    assert not outcome.passed, f"{case.id}: the bad run was accepted"
