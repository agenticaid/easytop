import json
from datetime import datetime
from pathlib import Path

import pytest

from easytop.agent.guardrails import (
    MAX_ANSWER_CHARS,
    MAX_TOOL_OUTPUT_CHARS,
    ToolArgError,
    cap_answer,
    trim_tool_output,
    validate_args,
)
from easytop.agent.prompt import SYSTEM_PROMPT
from easytop.agent.tools import TOOLS, dispatch
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService

VAULT_SPEC = Path("/path/to/vault/Easytop/Spec/06 Guardrails.md")
NOW = datetime(2026, 10, 5, 18, 42)


def svc():
    return FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)


@pytest.mark.skipif(not VAULT_SPEC.exists(), reason="Obsidian vault not available")
def test_system_prompt_is_verbatim_from_spec():
    import re

    block = re.search(r"```text\n(.*?)\n```", VAULT_SPEC.read_text(encoding="utf-8"), re.S).group(1)
    assert SYSTEM_PROMPT == block


def test_system_prompt_has_all_sections():
    for s in ("GROUNDING", "ANSWER CATEGORIES", "STYLE", "PRIVACY", "EXAMPLES"):
        assert s in SYSTEM_PROMPT


def test_no_tool_takes_parent_or_session_argument():
    names = [t["name"] for t in TOOLS]
    assert names == [
        "list_kids",
        "get_tomorrow_brief",
        "get_schedule",
        "get_homework",
        "get_messages",
        "get_exams",
        "get_events",
        "set_reminder",
    ]
    for t in TOOLS:
        assert not {"parent", "parent_id", "session", "cookies", "user"} & set(
            t["input_schema"]["properties"]
        )


def test_validate_args():
    schema = {"properties": {"kid": {"type": "string"}}, "required": ["kid"]}
    assert validate_args(schema, {"kid": "a"}) == {"kid": "a"}
    for bad in ({}, {"kid": 1}, {"kid": "a", "x": 1}, "str"):
        with pytest.raises(ToolArgError):
            validate_args(schema, bad)


def test_trim_tool_output_bounds_size_and_stays_json():
    big = {"messages": [{"text": "x" * 5000} for _ in range(30)]}
    out = trim_tool_output(big)
    assert len(out) <= MAX_TOOL_OUTPUT_CHARS
    parsed = json.loads(out)
    assert "third parties" in parsed["_notice"]


def test_trim_small_output_untouched():
    out = json.loads(trim_tool_output({"a": 1}))
    assert out["a"] == 1


def test_cap_answer():
    assert cap_answer("short") == "short"
    long = "\n".join(f"line {i}" for i in range(1000))
    assert len(cap_answer(long)) < 3700 and "shortened" in cap_answer(long)
    assert MAX_ANSWER_CHARS == 3500  # about one Telegram message: three kids x three categories fit


async def test_dispatch_happy_and_errors():
    s = svc()
    assert (await dispatch(s, "list_kids", {}))["kids"][0]["name"] == "Noa"
    assert (await dispatch(s, "get_tomorrow_brief", {"kid": "Noa"}))["kids"][0]["ok"]
    assert (
        "unknown kid"
        in (await dispatch(s, "get_schedule", {"kid": "evil", "date": "2026-10-06"}))["error"]
    )
    assert "bad arguments" in (await dispatch(s, "get_schedule", {"kid": "Noa"}))["error"]
    assert (
        "bad value"
        in (await dispatch(s, "get_schedule", {"kid": "Noa", "date": "tomorrow"}))["error"]
    )
    assert "unknown tool" in (await dispatch(s, "send_message", {}))["error"]
    assert "bad arguments" in (await dispatch(s, "list_kids", {"parent_id": "other"}))["error"]


async def test_tomorrow_brief_tool_offers_a_focus_that_must_be_one_of_the_categories():
    brief = next(t for t in TOOLS if t["name"] == "get_tomorrow_brief")
    assert brief["input_schema"]["properties"]["focus"]["enum"] == [
        "homework",
        "equipment",
        "exams",
        "flow",
    ]
    ok = await dispatch(svc(), "get_tomorrow_brief", {"kid": "Noa", "focus": "homework"})
    assert ok["focus"] == "homework"
    ok = await dispatch(svc(), "get_tomorrow_brief", {"kid": "Noa", "focus": "exams"})
    assert ok["focus"] == "exams"
    bad = await dispatch(svc(), "get_tomorrow_brief", {"focus": "gossip"})
    assert "error" in bad


def _description(name: str) -> str:
    return " ".join(next(t for t in TOOLS if t["name"] == name)["description"].split())


def test_tool_descriptions_carry_the_data_rules_so_the_prompt_does_not_have_to():
    brief = _description("get_tomorrow_brief")
    assert "every inbox message of the last 7 days" in brief
    assert "homework due on that day and upcoming homework" in brief
    assert "exams" in brief
    messages = _description("get_messages")
    assert "last 7 days" in messages and "since" in messages
    homework = _description("get_homework")
    assert "written in lessons dated" in homework and "due" not in homework


def _flat() -> str:
    return " ".join(SYSTEM_PROMPT.split())  # the prompt wraps lines


def test_system_prompt_defines_the_four_categories_their_sources_and_the_focus_rule():
    block = SYSTEM_PROMPT.split("ANSWER CATEGORIES")[1].split("FORMAT")[0]
    for needle in (
        "Homework",
        "Equipment",
        "Exams",
        "Day flow",
        "homework_due",
        "homework_upcoming",
        "upcoming_exams",
        "focus",
        "verbatim",
    ):
        assert needle in block
    assert "not found" in SYSTEM_PROMPT and "inferred" in SYSTEM_PROMPT
    assert "לא נמצא: מבחנים." in _flat()  # the one category asked for, when empty, is "not found"
    assert block.index("Homework") < block.index("Equipment") < block.index("Exams")
    assert block.index("Exams") < block.index("Day flow")


def test_system_prompt_makes_the_whole_inbox_material_for_every_category():
    flat = _flat()
    assert "last 7 days of messages" in flat and "feed all four categories" in flat
    assert "file every actionable item under its category" in flat
    assert "even if it is not about tomorrow" in flat
    assert "Show a dated item unless its date has passed" in flat


def test_system_prompt_collapses_one_fact_from_several_sources_into_one_line():
    flat = _flat()
    assert "One fact from several sources is one line with its sources in brackets" in flat
    assert "different facts are never merged" in flat


def test_system_prompt_files_a_message_under_the_child_it_names_and_hides_other_classes():
    flat = _flat()
    assert "A message has no child" in flat
    assert "whose name or class it names" in flat
    assert "never show another class's message" in flat


def test_system_prompt_groups_homework_under_one_bold_line_per_due_date_earliest_first():
    flat = _flat()
    assert "a bold line per due date, earliest first" in flat
    assert '"Due tomorrow (Tue 6.10)"' in flat and '"Due Thu 8.10"' in flat
    assert '"• **Subject** — text" in lesson-hour order' in flat
    assert "what to bring or wear is Equipment, the topic is Day flow" in flat


def test_system_prompt_gives_every_child_a_header_with_the_date_and_every_category_a_bold_heading():
    flat = _flat()
    assert "Header: **__Name__** (class) — the date" in flat
    assert "each as its emoji and a bold heading, a blank line between them" in flat
    assert '"Not found: exams." / "לא נמצא: מבחנים."' in flat
    assert '"• hour 1 · **Subject** — item"' in flat
    assert '"• **Subject** · Thu 8.10 — topic"' in flat


def test_system_prompt_drops_an_undated_message_item_once_the_subjects_next_lesson_was_held():
    flat = _flat()
    assert "A message may carry lessons_held_since" in flat
    assert "the children and subjects it names whose lesson came after it" in flat
    assert "without a date is past: leave it out for those children" in flat


def test_system_prompt_lists_past_homework_when_the_parent_asks_for_a_class_over_a_period():
    flat = _flat()
    assert "Past homework is left out" in flat
    assert 'Asked for the homework of a class over a period ("all Math homework in the last 3 weeks")' in flat
    assert "use get_homework for that period and get_messages since its start" in flat
    assert "list everything, past ones too" in flat


def test_the_brief_description_names_lessons_held_since_so_the_model_knows_what_it_is():
    description = _description("get_tomorrow_brief")
    assert "lessons_held_since" in description and "a lesson of the subject came after" in description
    assert "held_lessons" not in description


def test_system_prompt_puts_an_item_that_belongs_to_no_child_in_one_general_block():
    flat = _flat()
    assert "An item that belongs to no child (a general message) goes once, never per child" in flat
    assert "a last block headed **__General__** / **__כללי__**, same layout" in flat


def test_system_prompt_names_a_source_only_for_items_that_come_from_a_message():
    flat = _flat()
    assert "A source in brackets only for an item from a message, with the message's date" in flat
    assert '"(message 4.10)"' in flat  # no sender: as short as possible (Erez 2026-10-13)
    assert '"(homework tab + message 4.10)"' in flat
    assert "(הודעה 4.10)" in SYSTEM_PROMPT  # the examples show the date too


def test_system_prompt_forbids_guessing_why_data_is_missing_and_naming_internals():
    flat = _flat()
    assert "never guess why data is missing" in flat
    assert 'Never show tool or field names: say "Webtop" and plain words' in flat


def test_system_prompt_keeps_the_sports_clothes_inference_marked_as_inferred():
    flat = _flat()
    assert "Physical Education" in flat and "sports clothes" in flat and "as your inference" in flat


def test_system_prompt_asks_for_short_items_and_readable_dates():
    flat = _flat()
    assert "One short line per item" in flat
    assert "day.month" in flat and "2026-10-06" in flat


def test_system_prompt_orders_items_by_lesson_hour_and_names_double_lessons():
    flat = _flat()
    assert "lesson-hour order" in flat
    assert "double lesson" in flat and "hours 2-3" in flat


def test_system_prompt_drops_the_filler_lines_for_lessons_without_equipment_and_empty_categories():
    flat = _flat()
    assert "Lessons with no equipment are not listed" in flat
    assert "No equipment mentioned" not in flat and "לא צוין ציוד" not in SYSTEM_PROMPT
    assert "left out without a word, unless it is the one category asked for" in flat


def test_system_prompt_gives_a_short_default_answer_and_one_coming_up_line():
    flat = _flat()
    assert "DEFAULT ANSWER" in flat and "only what is due or needed on the child's next" in flat
    assert "end the whole answer with ONE line" in flat and '"Coming up: Noa' in flat
    assert "בהמשך:" in SYSTEM_PROMPT  # the Hebrew example shows the line
    assert "A named category, a later day, \"everything\" or \"what is coming\" gets full detail" in flat


def test_system_prompt_points_to_the_record_of_tool_calls_as_the_truth_about_what_was_checked():
    flat = _flat()
    assert "system record" in flat and "answer from it only" in flat
    assert "never claim a check in it did not run" in flat


def test_system_prompt_spells_webtop_in_hebrew_as_vovtop():
    assert "וובטופ" in SYSTEM_PROMPT


def test_system_prompt_forbids_trailing_commentary_about_its_own_process():
    flat = _flat()
    assert "No other commentary about your process or earlier answers" in flat
    assert "end when the information ends" in flat


def test_system_prompt_keeps_the_answer_in_the_parents_language_and_names_untouched():
    flat = _flat()
    assert "Reply in the language of the parent's last message, every word" in flat
    assert "Hebrew in tool data never changes it" in flat
    assert "never transliterated" in flat
    assert "never announce the language" in flat


def test_system_prompt_routes_lesson_events_to_equipment_and_homework_and_hides_praise():
    flat = _flat()
    assert "recent_lesson_events" in flat and "אי הבאת ציוד" in flat
    assert "no praise or behavior unless asked" in flat


def test_system_prompt_spells_webtop_with_prefixes_and_puts_no_emoji_next_to_child_names():
    assert (
        "בוובטופ" in SYSTEM_PROMPT and "בווטופ" in SYSTEM_PROMPT
    )  # the right form and the wrong one named
    flat = _flat()
    assert "none next to a name" in flat and "**__Name__** (class)" in flat
    assert "🧒" not in flat and "👧" not in flat


def test_system_prompt_treats_the_day_opener_as_an_opener_not_hour_zero():
    flat = _flat()
    assert "day_opener" in flat and "after_day_opener" in flat
    assert 'never "hour 0"' in flat and '"usually"' in flat and "shortened" in flat


def test_system_prompt_has_one_hebrew_example_per_response_type_and_says_they_are_format_only():
    examples = SYSTEM_PROMPT.split("EXAMPLES")[1]
    assert "format only" in examples and "every fact is invented" in examples
    assert "reply in the parent's language" in examples
    full, one_category, from_message, failure = (
        "מה הילדים צריכים מחר?",  # a full tomorrow answer with all four categories
        '"ציוד בלבד לאורי"',  # one category only
        "Message from the maths teacher",  # items taken from a message, one line for tab + message
        "⚠️ הוובטופ של בית הספר לא ענה",  # one school failed
    )
    for marker in (full, one_category, from_message, failure):
        assert marker in examples
    for icon in ("📚", "🎒"):  # the full example shows the categories that have something tomorrow...
        assert icon in examples.split(one_category)[0]
    assert "לא נמצא" not in examples.split(one_category)[0]  # ...and says nothing about empty ones
    assert "בהמשך: טליה" in examples.split(one_category)[0]  # later items: one closing line
    assert "מבחן חשבון ד׳ 22.10" in examples  # an exam that is not tomorrow is only in that line
    assert "אני רק קורא מידע" in examples  # a write request is refused and sent to Webtop
    assert "**__טליה__** (ד׳1) — מחר, יום ג׳ 6.10" in examples  # header: name, class and date
    assert "**להגשה מחר (ג׳ 6.10)**" in examples  # homework grouped by due date
    assert "• שעה 1 · **חשבון** — סרגל" in examples  # equipment: hour, bold subject, item


def test_the_prompt_ends_with_the_language_reminder_so_hebrew_examples_do_not_leak_into_other_answers():
    """Decisions 2026-10-01 and 2026-10-13: a prompt full of Hebrew once made English questions get Hebrew
    answers, and it did again in a live eval until the reminder moved to the very end."""
    tail = _flat()[-300:]
    assert "language of the parent's last message" in tail
    assert "an English question gets English headings (Homework, Equipment, Exams, Day flow)" in tail


EXAMPLE_ONLY_FACTS = ("טליה", "אורי", "עמ׳ 17", "page 17", "מחוגה", "compass", "משקפי מגן", "22.10")


def test_no_fact_that_only_the_prompt_examples_contain_is_in_the_data_or_the_evals():
    """An answer that repeats an example fact is a copy, not an answer: the data and the eval cases must not
    contain any, or a copy could not be told from the truth."""
    from pathlib import Path

    from easytop.connector.fake import DATA_FILE
    from easytop.evals import cases

    haystack = DATA_FILE.read_text(encoding="utf-8") + Path(cases.__file__).read_text(encoding="utf-8")
    for fact in EXAMPLE_ONLY_FACTS:
        assert fact in SYSTEM_PROMPT and fact not in haystack, fact


def test_system_prompt_tells_the_model_to_cite_the_ref_of_a_message_with_files_and_nothing_more():
    flat = " ".join(SYSTEM_PROMPT.split())
    assert "ref" in flat and "double braces" in flat and "{{m" in flat
    assert "links are added for you" in flat
    # the example shows the marker once per item taken from the message that has files
    assert SYSTEM_PROMPT.count("{{m3}}") >= 2
