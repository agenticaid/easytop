from datetime import datetime

from easytop import chat
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import ScriptedClient, response, text, tool_use

NOW = datetime(2026, 10, 5, 18, 42)


def make_agent(client, **kw):
    svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
    return Agent(client, svc, clock=lambda: NOW, **kw)


def lines(*items):
    it = iter(items)

    def read(_prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return read


async def test_chat_answers_shows_tools_tokens_and_quits():
    client = ScriptedClient(
        response(tool_use("t1", "list_kids"), in_tok=50, out_tok=5),
        response(text("Two kids."), in_tok=70, out_tok=9),
    )
    out = []
    await chat.run_chat(make_agent(client), lines("who are my kids?", "/quit"), out.append)
    joined = "\n".join(out)
    assert "Two kids." in joined
    assert "[tools: list_kids]" in joined
    assert "tokens in=120 out=14" in joined


async def test_chat_keeps_history_between_turns_and_skips_blank_lines():
    client = ScriptedClient(response(text("A1")), response(text("A2")))
    await chat.run_chat(make_agent(client), lines("q1", "", "  ", "q2"), lambda s: None)
    assert len(client.calls) == 2
    sent = client.calls[1]["messages"]
    assert [m["content"] for m in sent] == ["q1", "A1", "q2"]


async def test_chat_prints_cost_only_when_prices_given_and_totals_on_exit():
    client = ScriptedClient(response(text("ok"), in_tok=1_000_000, out_tok=0))
    out = []
    await chat.run_chat(
        make_agent(client, prices_per_mtok=(3.0, 15.0)), lines("hi", "/quit"), out.append
    )
    assert any("cost $3.0000" in o for o in out)
    assert any(o.startswith("session total:") and "1 question" in o for o in out)


def test_main_without_api_key_exits_2_and_says_how(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert chat.main([]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
