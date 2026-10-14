from datetime import datetime

import anthropic
import httpx

from easytop.agent.core import EMPTY_MESSAGE, MODEL, RETRY_MESSAGE, Agent, Conversation
from easytop.agent.prompt import SYSTEM_PROMPT
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import (
    ScriptedClient,
    redacted_thinking,
    response,
    text,
    thinking,
    tool_use,
)

NOW = datetime(2026, 10, 5, 18, 42)


def agent(client, **kw):
    svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
    return Agent(client, svc, clock=lambda: NOW, **kw)


def api_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def tool_result_of(call, idx=-1):
    return call["messages"][idx]["content"][0]


async def test_model_id_and_direct_answer():
    c = ScriptedClient(response(text("Hello"), in_tok=100, out_tok=20))
    r = await agent(c, prices_per_mtok=(3.0, 15.0)).ask(Conversation(), "hi")
    assert r.text == "Hello" and r.tool_calls == []
    assert c.calls[0]["model"] == MODEL == "claude-sonnet-5-5"
    assert r.input_tokens == 100 and r.output_tokens == 20
    assert abs(r.cost_usd - (100 * 3 + 20 * 15) / 1e6) < 1e-12


async def test_no_price_means_no_cost_invented():
    r = await agent(ScriptedClient(response(text("x"))), model="unpriced-model").ask(
        Conversation(), "hi"
    )
    assert r.cost_usd is None


async def test_system_prompt_verbatim_first_then_date_and_context():
    c = ScriptedClient(response(text("ok")))
    await agent(c).ask(Conversation(), "hi", context="Prefers short answers")
    system = c.calls[0]["system"]
    assert system[0]["text"] == SYSTEM_PROMPT
    assert "2026-10-05" in system[1]["text"]
    assert "Prefers short answers" in system[2]["text"]


async def test_empty_context_adds_no_block():
    c = ScriptedClient(response(text("ok")))
    await agent(c).ask(Conversation(), "hi")
    assert len(c.calls[0]["system"]) == 2


async def test_tool_loop_runs_tool_and_feeds_result():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief", kid="Noa")),
        response(text("Noa: math page 42")),
    )
    r = await agent(c).ask(Conversation(), "What does Noa need?")
    assert r.tool_calls == ["get_tomorrow_brief"] and r.text == "Noa: math page 42"
    tr = tool_result_of(c.calls[1])
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    assert "Workbook page 42" in tr["content"] and not tr["is_error"]


async def test_tool_error_is_reported_as_error_result():
    c = ScriptedClient(
        response(tool_use("t1", "get_schedule", kid="someone-else", date="2026-10-06")),
        response(text("I can't see that child.")),
    )
    await agent(c).ask(Conversation(), "x")
    assert tool_result_of(c.calls[1])["is_error"] is True


async def test_max_eight_tool_calls_then_forced_answer():
    script = [response(tool_use(f"t{i}", "list_kids")) for i in range(8)]
    script.append(response(tool_use("t9", "list_kids")))  # model insists on a 9th
    script.append(response(text("final")))
    c = ScriptedClient(*script)
    r = await agent(c).ask(Conversation(), "loop")
    assert len(r.tool_calls) == 8 and r.hit_tool_limit is True and r.text == "final"
    assert c.calls[8].get("tool_choice") == {"type": "none"}
    assert "limit" in tool_result_of(c.calls[9])["content"]


async def test_multiple_tool_uses_in_one_turn_respect_budget():
    blocks = [tool_use(f"t{i}", "list_kids") for i in range(10)]
    c = ScriptedClient(response(*blocks), response(text("done")))
    r = await agent(c).ask(Conversation(), "x")
    assert len(r.tool_calls) == 8 and r.hit_tool_limit


async def test_retry_once_then_success():
    c = ScriptedClient(api_error(), response(text("ok")))
    r = await agent(c).ask(Conversation(), "x")
    assert r.text == "ok" and len(c.calls) == 2


async def test_api_error_twice_gives_friendly_message_and_keeps_history():
    c = ScriptedClient(api_error(), api_error())
    conv = Conversation()
    r = await agent(c).ask(conv, "x")
    assert r.text == RETRY_MESSAGE and r.error == "APIConnectionError"
    assert conv.messages == []


async def test_conversation_carries_followups():
    conv = Conversation()
    c = ScriptedClient(response(text("A1")), response(text("A2")))
    a = agent(c)
    await a.ask(conv, "Q1")
    await a.ask(conv, "Q2")
    second = c.calls[1]["messages"]
    assert [m["content"] for m in second] == ["Q1", "A1", "Q2"]


async def test_history_bounded():
    conv = Conversation()
    a = agent(ScriptedClient(*[response(text(f"A{i}")) for i in range(15)]))
    for i in range(15):
        await a.ask(conv, f"Q{i}")
    assert len(conv.messages) == 20


async def test_long_answer_capped_and_empty_answer_replaced():
    c = ScriptedClient(response(text("\n".join(["line"] * 2000))), response(text("")))
    a = agent(c)
    assert "shortened" in (await a.ask(Conversation(), "x")).text
    assert (await a.ask(Conversation(), "y")).text == EMPTY_MESSAGE


async def test_thinking_blocks_are_sent_back_unchanged_with_the_tool_results():
    c = ScriptedClient(
        response(
            thinking("I should look at the kids first", "sig-abc"),
            redacted_thinking("opaque"),
            text("Let me check."),
            tool_use("t1", "list_kids"),
        ),
        response(thinking("now answer"), text("Two kids.")),
    )
    r = await agent(c).ask(Conversation(), "who are my kids?")
    assert r.text == "Two kids."  # thinking text never leaks into the answer
    echoed = c.calls[1]["messages"][1]["content"]
    assert echoed[0] == {
        "type": "thinking",
        "thinking": "I should look at the kids first",
        "signature": "sig-abc",
    }
    assert echoed[1] == {"type": "redacted_thinking", "data": "opaque"}
    assert [b["type"] for b in echoed] == ["thinking", "redacted_thinking", "text", "tool_use"]


async def test_unknown_block_types_are_passed_through_not_crashed_on():
    from types import SimpleNamespace as NS

    class Block(NS):
        def model_dump(self, **kw):
            return {"type": self.type, "payload": self.payload}

    odd = Block(type="future_block", payload="x")
    c = ScriptedClient(
        response(odd, tool_use("t1", "list_kids")),
        response(text("done")),
    )
    r = await agent(c).ask(Conversation(), "hi")
    assert r.text == "done"
    assert c.calls[1]["messages"][1]["content"][0] == {"type": "future_block", "payload": "x"}


async def test_request_leaves_room_for_thinking_plus_the_answer():
    c = ScriptedClient(response(text("hi")))
    await agent(c).ask(Conversation(), "hi")
    # a full three-kid Hebrew answer plus the thinking reached the old 4,096 cap (2026-10-13): leave real headroom
    assert c.calls[0]["max_tokens"] >= 8192


async def test_answer_cut_off_by_the_token_limit_is_named_not_called_empty():
    from easytop.agent.core import TRUNCATED_MESSAGE

    c = ScriptedClient(response(thinking("long reasoning..."), stop="max_tokens"))
    convo = Conversation()
    r = await agent(c).ask(convo, "tell me everything")
    assert r.text == TRUNCATED_MESSAGE and r.truncated is True
    assert convo.messages == []  # a failed turn is not remembered


async def test_partial_text_cut_off_by_the_limit_is_kept_and_flagged():
    c = ScriptedClient(response(text("Noa: Math p.42, and"), stop="max_tokens"))
    r = await agent(c).ask(Conversation(), "hi")
    assert r.text.startswith("Noa: Math p.42") and r.truncated is True


# ---- the model must be able to see which tools it already called in this conversation ----


def _record(call):
    texts = [b["text"] for b in call["system"]]
    return next((t for t in texts if t.startswith("TOOL CALLS ALREADY MADE")), None)


async def test_follow_up_sees_a_record_of_the_tools_called_in_earlier_turns():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief", kid="Noa", focus="equipment")),
        response(text("Noa: a ruler")),
        response(text("Yes, I checked Webtop.")),
    )
    convo = Conversation()
    a = agent(c)
    await a.ask(convo, "equipment for Noa?")
    await a.ask(convo, "did you actually check?")
    assert _record(c.calls[0]) is None and _record(c.calls[1]) is None  # nothing earlier yet
    rec = _record(c.calls[2])
    assert "18:42 get_tomorrow_brief(kid=Noa, focus=equipment) -> ok" in rec
    assert "system record" in rec.lower()


async def test_a_failed_tool_call_is_recorded_as_an_error_without_its_content():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief", kid="Nobody")),
        response(text("I couldn't find that child.")),
        response(text("ok")),
    )
    convo = Conversation()
    a = agent(c)
    await a.ask(convo, "equipment for Nobody?")
    await a.ask(convo, "and now?")
    assert "get_tomorrow_brief(kid=Nobody) -> error" in _record(c.calls[2])


async def test_the_record_keeps_only_the_last_twenty_calls():
    convo = Conversation()
    convo.tool_log = [f"18:00 list_kids() -> ok #{i}" for i in range(30)]
    c = ScriptedClient(
        response(tool_use("t1", "list_kids")), response(text("a")), response(text("b"))
    )
    a = agent(c)
    await a.ask(convo, "q1")
    assert len(convo.tool_log) == 20 and convo.tool_log[-1].startswith("18:42 list_kids()")


def test_the_agent_exposes_its_family_service_read_only():
    from easytop.agent.core import Agent

    family = object()
    agent = Agent(object(), family)
    assert agent.family is family


def test_the_fixed_error_texts_are_hebrew_first_with_english_like_every_other_fixed_text():
    from easytop.agent.core import EMPTY_MESSAGE, RETRY_MESSAGE, TRUNCATED_MESSAGE

    for message in (RETRY_MESSAGE, EMPTY_MESSAGE, TRUNCATED_MESSAGE):
        hebrew, english = message.split("\n\n")
        assert any("֐" <= ch <= "׿" for ch in hebrew)
        assert english.isascii()
