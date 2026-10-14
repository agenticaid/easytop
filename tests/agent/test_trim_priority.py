"""When a tool result is too big, what is cut first matters: the teacher's homework text stays verbatim,
the inbox text and the lesson notes shrink before it (Spec 13)."""

import json

from easytop.agent.guardrails import MAX_TOOL_OUTPUT_CHARS, trim_tool_output

HW = "Math: workbook page 42, exercises 1-9, and prepare the poster. " * 3


def brief(messages=0, msg_chars=1500, notes=0, note_chars=300):
    return {
        "kids": [
            {
                "name": "Noa",
                "lessons": [{"subject": f"S{i}", "note": "n" * note_chars} for i in range(notes)],
                "homework_due": [{"subject": "Math", "description": HW}],
                "homework_upcoming": [{"subject": "Art", "description": HW, "due": "2026-10-08"}],
            }
        ],
        "messages": [{"id": f"m{i}", "text": "t" * msg_chars} for i in range(messages)],
    }


def test_the_cap_covers_a_big_family_brief():
    assert MAX_TOOL_OUTPUT_CHARS >= 16000


def test_a_brief_under_the_cap_is_untouched():
    parsed = json.loads(trim_tool_output(brief(messages=5, msg_chars=600)))
    assert all(len(m["text"]) == 600 for m in parsed["messages"])


def test_messages_shrink_before_anything_else_and_homework_stays_verbatim():
    parsed = json.loads(trim_tool_output(brief(messages=30, msg_chars=1500)))
    kid = parsed["kids"][0]
    assert kid["homework_due"][0]["description"] == HW
    assert kid["homework_upcoming"][0]["description"] == HW
    assert len(parsed["messages"]) == 30  # every message is still there
    assert all(0 < len(m["text"]) <= 601 for m in parsed["messages"])


def test_lesson_notes_shrink_before_homework_when_the_inbox_is_not_the_problem():
    parsed = json.loads(trim_tool_output(brief(messages=2, notes=60, note_chars=400)))
    kid = parsed["kids"][0]
    assert kid["homework_due"][0]["description"] == HW
    assert kid["homework_upcoming"][0]["description"] == HW
    assert all(len(x["note"]) < 400 for x in kid["lessons"])


def test_the_result_is_always_valid_json_within_the_cap():
    for payload in (
        brief(messages=30, msg_chars=5000, notes=80, note_chars=2000),
        {"messages": [{"text": "x" * 100} for _ in range(5000)]},
    ):
        out = trim_tool_output(payload)
        assert len(out) <= MAX_TOOL_OUTPUT_CHARS
        assert "third parties" in json.loads(out)["_notice"]


def test_when_even_the_trimmed_brief_is_too_big_the_homework_is_still_kept():
    parsed = json.loads(trim_tool_output(brief(messages=30, msg_chars=5000, notes=400, note_chars=900)))
    assert parsed.get("truncated") is True
    assert parsed["kids"][0]["homework_due"][0]["description"] == HW
