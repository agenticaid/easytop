from datetime import datetime

from easytop.agent.core import MODEL, Agent, Conversation
from easytop.agent.pricing import PRICES_PER_MTOK, PRICING_SOURCE, cost_usd
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from tests.agent.fakes import ScriptedClient, response, text

NOW = datetime(2026, 10, 5, 18, 42)


def test_sonnet_5_5_prices_are_the_published_ones_with_their_source():
    assert PRICES_PER_MTOK["claude-sonnet-5-5"] == (2.0, 10.0)  # USD per million tokens, in / out
    assert PRICING_SOURCE.startswith("https://platform.claude.com/docs/en/about-claude/pricing")


def test_cost_is_computed_from_tokens_and_unknown_models_have_none():
    assert cost_usd("claude-sonnet-5-5", 1_000_000, 100_000) == 2.0 + 1.0
    assert cost_usd("some-model-we-have-no-price-for", 1000, 1000) is None


async def test_agent_reports_cost_for_the_default_model_without_being_told_prices():
    svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
    a = Agent(
        ScriptedClient(response(text("ok"), in_tok=9000, out_tok=400)), svc, clock=lambda: NOW
    )
    r = await a.ask(Conversation(), "hi")
    assert MODEL == "claude-sonnet-5-5"
    assert abs(r.cost_usd - (9000 * 2 + 400 * 10) / 1e6) < 1e-12


async def test_agent_with_an_unpriced_model_reports_no_cost():
    svc = FamilyService(FakeConnector(NOW.date()), clock=lambda: NOW)
    a = Agent(ScriptedClient(response(text("ok"))), svc, model="other-model", clock=lambda: NOW)
    assert (await a.ask(Conversation(), "hi")).cost_usd is None
