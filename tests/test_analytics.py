import json
import logging
from datetime import UTC, datetime

import pytest

from easytop import analytics
from easytop.analytics import SCHEMA, AnalyticsSchemaError, Emitter, labels_for, language_of
from easytop.logjson import JsonFormatter

NOW = datetime(2026, 10, 11, 18, 0, tzinfo=UTC)


def emitter(**kw):
    return Emitter(env="test", key=b"k" * 32, clock=lambda: NOW, strict=True, **kw)


def captured(caplog):
    return [r.analytics for r in caplog.records if hasattr(r, "analytics")]


def test_an_event_carries_the_common_fields_and_a_pseudonym_not_the_telegram_id(caplog):
    caplog.set_level(logging.INFO, logger="easytop.analytics")
    emitter().emit("invite_redeemed", parent="telegram:7000000001")
    (event,) = captured(caplog)
    assert event["event"] == "invite_redeemed" and event["env"] == "test" and event["schema"] == 1
    assert event["ts"] == "2026-10-11T18:00:00.000Z" and event["synthetic"] is False
    assert event["parent_ref"].startswith("p_") and len(event["parent_ref"]) == 18
    assert "7000000001" not in json.dumps(event)


def test_the_pseudonym_is_stable_for_one_key_and_differs_between_environments():
    a, b = emitter(), Emitter(env="test", key=b"other-key", strict=True)
    assert a.ref("telegram:1") == a.ref("telegram:1") != a.ref("telegram:2")
    assert a.ref("telegram:1") != b.ref("telegram:1")


def test_without_a_key_refs_are_random_per_process_but_still_pseudonymous():
    e = Emitter(env="local", key=None, strict=True)
    assert e.ref("telegram:1").startswith("p_") and "telegram" not in e.ref("telegram:1")


def test_unknown_event_is_refused():
    with pytest.raises(AnalyticsSchemaError):
        emitter().emit("question_text_dump", parent="telegram:1")


def test_unknown_field_is_refused():
    with pytest.raises(AnalyticsSchemaError):
        emitter().emit("question", parent="telegram:1", outcome="answered", question="מה מחר?")


def test_free_text_in_an_enum_field_is_refused():
    with pytest.raises(AnalyticsSchemaError):
        emitter().emit("question", parent="telegram:1", outcome="Noa needs a ruler")


def test_wrong_types_are_refused():
    e = emitter()
    with pytest.raises(AnalyticsSchemaError):
        e.emit("question", parent="telegram:1", outcome="answered", answer_ms="fast")
    with pytest.raises(AnalyticsSchemaError):
        e.emit("question", parent="telegram:1", outcome="answered", follow_up=1)
    with pytest.raises(AnalyticsSchemaError):
        e.emit("question", parent="telegram:1", outcome="answered", tools=["get_secret"])


def test_error_types_must_look_like_a_class_name():
    e = emitter()
    e.emit("error", where="agent", error_type="APIConnectionError")
    with pytest.raises(AnalyticsSchemaError):
        e.emit("error", where="agent", error_type="connection refused by 10.0.0.1")


def test_required_fields_are_required():
    with pytest.raises(AnalyticsSchemaError):
        emitter().emit("question", parent="telegram:1")


def test_outside_tests_a_bad_event_is_dropped_with_a_warning_and_never_raises(caplog):
    caplog.set_level(logging.INFO)
    e = Emitter(env="prod", key=b"k", strict=False)
    e.emit("question", parent="telegram:1", outcome="Noa needs a ruler")
    assert captured(caplog) == []
    assert "analytics event refused event=question" in caplog.text and "Noa" not in caplog.text


def test_every_field_name_has_one_type_across_all_events():
    seen: dict[str, object] = {}
    for fields in SCHEMA.values():
        for name, kind in fields.items():
            family = analytics.type_family(kind)
            assert seen.setdefault(name, family) == family, name


def test_json_log_line_carries_the_event_at_the_top_level():
    e = emitter()
    record = e.record("session_check", parent="telegram:1", status="ok", age_h=2.5)
    data = json.loads(JsonFormatter().format(record))
    assert data["event"] == "session_check" and data["status"] == "ok" and data["age_h"] == 2.5
    assert data["severity"] == "INFO" and data["logger"] == "easytop.analytics"


def test_synthetic_emitter_marks_every_event():
    e = Emitter(env="dev", key=b"k", strict=True, synthetic=True)
    assert (
        e.record("invite_created", parent="telegram:1", ttl_hours=24).analytics["synthetic"] is True
    )


# ---- labels by code ----


def use(name, **inp):
    return {"name": name, "input": inp}


@pytest.mark.parametrize(
    "uses, expected",
    [
        ([], ("none", "none", "none")),
        ([use("list_kids")], ("kids", "none", "none")),
        ([use("get_tomorrow_brief")], ("tomorrow", "all", "all")),
        ([use("get_tomorrow_brief", focus="equipment")], ("tomorrow", "equipment", "all")),
        ([use("get_tomorrow_brief", kid="Noa", focus="homework")], ("tomorrow", "homework", "one")),
        ([use("get_tomorrow_brief", kid="Noa", focus="exams")], ("tomorrow", "exams", "one")),
        ([use("list_kids"), use("get_exams", kid="Ido")], ("exams", "none", "one")),
        (
            [use("get_schedule", kid="Noa"), use("get_schedule", kid="Ido")],
            ("schedule", "none", "several"),
        ),
        ([use("get_messages"), use("get_events", kid="Noa")], ("mixed", "none", "all")),
        ([use("get_homework", kid="k1")], ("homework", "none", "one")),
        (
            [use("get_tomorrow_brief", focus="flow"), use("get_tomorrow_brief", focus="homework")],
            ("tomorrow", "all", "all"),
        ),
        ([use("teleport")], ("unknown", "none", "none")),
        # the reminder setting is its own category and is no question about any child
        ([use("set_reminder", action="on", time="20:00")], ("reminder", "none", "none")),
        ([use("list_kids"), use("set_reminder", action="off")], ("reminder", "none", "none")),
        (
            [use("get_tomorrow_brief", kid="Noa"), use("set_reminder", action="on", time="20:00")],
            ("mixed", "none", "one"),
        ),
    ],
)
def test_category_focus_and_kids_scope_come_from_the_tool_calls(uses, expected):
    assert labels_for(uses) == expected


@pytest.mark.parametrize(
    "text, lang",
    [
        ("מה הילדים צריכים מחר?", "he"),
        ("What does Noa need tomorrow?", "en"),
        ("Что нужно завтра?", "ru"),
        ("ماذا يحتاج الأطفال غدا؟", "ar"),
        ("מה Noa צריכה מחר?", "he"),
        ("12:00 ?", "other"),
    ],
)
def test_language_is_read_from_the_letters(text, lang):
    assert language_of(text) == lang
