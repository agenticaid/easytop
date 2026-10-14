from datetime import datetime

from easytop.agent.core import Agent, Conversation
from easytop.agent.pricing import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, cost_usd
from easytop.agent.tools import TOOLS
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import ScriptedClient, response, text

NOW = datetime(2026, 10, 5, 18, 42)
EPHEMERAL = {"type": "ephemeral"}


def agent(client):
    return Agent(
        client, FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW), clock=lambda: NOW
    )


async def test_only_the_stable_prefix_is_marked_cacheable():
    c = ScriptedClient(response(text("ok")))
    convo = Conversation()
    convo.tool_log = ["18:40 list_kids() -> ok"]
    await agent(c).ask(convo, "hi", context="Prefers short answers")
    call = c.calls[0]
    system = call["system"]
    assert system[0]["cache_control"] == EPHEMERAL  # the fixed system prompt
    assert all(
        "cache_control" not in b for b in system[1:]
    )  # date, context and tool log change per call
    assert call["tools"][-1]["cache_control"] == EPHEMERAL
    assert all("cache_control" not in t for t in call["tools"][:-1])


async def test_the_shared_tool_list_is_not_modified_by_marking_it_cacheable():
    c = ScriptedClient(response(text("ok")))
    await agent(c).ask(Conversation(), "hi")
    assert all("cache_control" not in t for t in TOOLS)


async def test_cache_writes_and_reads_are_counted_and_priced():
    c = ScriptedClient(
        response(text("ok"), in_tok=1000, out_tok=400, cache_write=3000, cache_read=6000)
    )
    r = await agent(c).ask(Conversation(), "hi")
    assert (r.input_tokens, r.cache_write_tokens, r.cache_read_tokens) == (1000, 3000, 6000)
    expected = (1000 * 2 + 3000 * 2.5 + 6000 * 0.2 + 400 * 10) / 1e6
    assert abs(r.cost_usd - expected) < 1e-12


async def test_usage_without_cache_fields_counts_zero():
    r = await agent(ScriptedClient(response(text("ok")))).ask(Conversation(), "hi")
    assert (r.cache_write_tokens, r.cache_read_tokens) == (0, 0)


def test_cache_multipliers_are_the_published_ones():
    assert (CACHE_WRITE_MULTIPLIER, CACHE_READ_MULTIPLIER) == (1.25, 0.1)  # 5-minute write, hit
    assert abs(cost_usd("claude-sonnet-5-5", 0, 0, cache_write=1_000_000) - 2.5) < 1e-9
    assert abs(cost_usd("claude-sonnet-5-5", 0, 0, cache_read=1_000_000) - 0.2) < 1e-9
