"""The agent can set or stop the bot's own daily evening reminder (task 6.16). It is the only thing the agent can
change, it is not Webtop, and it carries a time and nothing else."""

from datetime import datetime, time

import pytest

from easytop.agent.core import Agent, Conversation
from easytop.agent.tools import TOOLS, dispatch
from easytop.connector.fake import FakeConnector
from easytop.digest.nudge import StoreReminders
from easytop.family.service import FamilyService
from easytop.store.base import DigestSetting
from easytop.store.memory import InMemoryStore
from tests.agent.fakes import ScriptedClient, response, text, tool_use

NOW = datetime(2026, 10, 5, 18, 42)


class Setter:
    """Records what the tool asked for (None = turned off)."""

    def __init__(self):
        self.calls: list[time | None] = []
        self.contents: list[str] = []

    async def set(self, when, content=None):
        self.calls.append(when)
        self.contents.append(content)
        return None if when is None else (content or "none")  # what the reminder now carries


def svc():
    return FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)


def test_the_tool_is_listed_and_takes_only_an_action_and_a_time():
    tool = next(t for t in TOOLS if t["name"] == "set_reminder")
    schema = tool["input_schema"]
    assert set(schema["properties"]) == {"action", "time", "content"}
    assert schema["properties"]["content"]["enum"] == ["none", "due", "all"]
    assert schema["properties"]["action"]["enum"] == ["on", "off"]
    assert schema["required"] == ["action"]
    assert "daily" in tool["description"] and "one-off" in tool["description"]
    assert "one short line" in tool["description"]  # Erez 2026-10-13: the confirmation is a single line
    assert "what it carries" in tool["description"]  # ...that also says what the reminder contains


async def test_on_sets_the_time_and_says_what_was_set():
    s = Setter()
    out = await dispatch(svc(), "set_reminder", {"action": "on", "time": "20:00"}, reminders=s)
    assert s.calls == [time(20)]
    assert "error" not in out and out["reminder"] == "on" and out["time"] == "20:00"
    assert "note" not in out  # a note invited the model to explain the reminder at length
    assert out["content"] == "none"  # the answer says what it carries: the fixed message


async def test_the_reminder_is_content_free_unless_the_parent_asked_for_content():
    s = Setter()
    await dispatch(svc(), "set_reminder", {"action": "on", "time": "20:00"}, reminders=s)
    await dispatch(
        svc(), "set_reminder", {"action": "on", "time": "17:03", "content": "due"}, reminders=s
    )
    await dispatch(
        svc(), "set_reminder", {"action": "on", "time": "17:03", "content": "all"}, reminders=s
    )
    assert s.contents == [None, "due", "all"]  # absent = leave the current choice as it is
    out = await dispatch(
        svc(), "set_reminder", {"action": "on", "time": "17:03", "content": "due"}, reminders=s
    )
    assert out["content"] == "due"


async def test_off_forgets_any_content_and_a_content_without_a_time_is_refused():
    s = Setter()
    out = await dispatch(svc(), "set_reminder", {"action": "off", "content": "due"}, reminders=s)
    assert s.calls == [None] and s.contents == [None] and out == {"reminder": "off"}
    refused = await dispatch(svc(), "set_reminder", {"action": "on", "content": "due"}, reminders=s)
    assert "error" in refused and s.calls == [None]


async def test_off_turns_it_off():
    s = Setter()
    out = await dispatch(svc(), "set_reminder", {"action": "off"}, reminders=s)
    assert s.calls == [None]
    assert "error" not in out and out["reminder"] == "off"


@pytest.mark.parametrize(
    "args",
    [
        {"action": "on"},  # no time: the model must ask the parent
        {"action": "on", "time": "25:00"},
        {"action": "on", "time": "8"},  # a bare hour could be morning or evening
        {"action": "on", "time": "tomorrow"},
        {"action": "pause"},
        {"action": "on", "time": "20:00", "content": "everything"},
        {"time": "20:00"},
        {"action": "on", "time": "20:00", "parent": "telegram:2"},  # no tool takes a parent
    ],
)
async def test_a_bad_request_changes_nothing_and_the_model_is_told_why(args):
    s = Setter()
    out = await dispatch(svc(), "set_reminder", args, reminders=s)
    assert "error" in out
    assert s.calls == []


async def test_without_a_reminder_setting_the_tool_reports_it_is_unavailable():
    out = await dispatch(svc(), "set_reminder", {"action": "on", "time": "20:00"})
    assert "error" in out and "unavailable" in out["error"]


async def test_the_agent_loop_runs_the_tool_for_the_parent_it_is_bound_to():
    s = Setter()
    client = ScriptedClient(
        response(tool_use("t1", "set_reminder", action="on", time="20:00")),
        response(text("Done, 20:00.")),
    )
    agent = Agent(client, svc(), clock=lambda: NOW, reminders=s)
    r = await agent.ask(Conversation(), "remind me every evening at 8")
    assert s.calls == [time(20)]
    assert r.tool_calls == ["set_reminder"] and r.text == "Done, 20:00."
    assert r.tool_uses == [{"name": "set_reminder", "input": {"action": "on", "time": "20:00"}}]


async def test_the_store_reminder_writes_the_parents_digest_setting():
    store = InMemoryStore()
    p = await store.get_or_create_parent("telegram", "1")
    reminders = StoreReminders(store, p.id, "telegram:1")
    await reminders.set(time(20, 30))
    assert await store.get_digest(p.id) == DigestSetting(True, time(20, 30))
    await reminders.set(time(17, 3), "due")
    assert await store.get_digest(p.id) == DigestSetting(True, time(17, 3), "due")
    kept = await reminders.set(time(21, 15))  # only the time changes: the content choice stays
    assert kept == "due"  # ...and the caller is told, so the parent can be too
    assert await store.get_digest(p.id) == DigestSetting(True, time(21, 15), "due")
    await reminders.set(time(21, 15), "none")  # asked to go back to the fixed message
    assert await store.get_digest(p.id) == DigestSetting(True, time(21, 15), "none")
    await reminders.set(None)
    assert await store.get_digest(p.id) == DigestSetting(False, None)
    await reminders.set(time(18, 0))  # a reminder switched back on starts content-free
    assert await store.get_digest(p.id) == DigestSetting(True, time(18, 0), "none")
