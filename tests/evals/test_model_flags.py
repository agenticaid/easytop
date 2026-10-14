"""The eval runner can score another model or effort level, so a cheaper model is judged by the same evals
(no API calls)."""

import pytest

from easytop.agent.core import Agent, Conversation
from easytop.agent.pricing import cost_usd
from easytop.evals.cases import ALL_CASES
from easytop.evals.harness import CaseOutcome, build_service, run_all, run_case, totals_line
from tests.agent.fakes import ScriptedClient, response, text


def _case(case_id="guard-scope"):
    return next(c for c in ALL_CASES if c.id == case_id)


@pytest.mark.asyncio
async def test_effort_is_sent_as_output_config_only_when_given():
    case = _case()
    with_effort = ScriptedClient(response(text("ok")))
    await Agent(with_effort, build_service(case), effort="low").ask(Conversation(), "hi")
    assert with_effort.calls[0]["output_config"] == {"effort": "low"}

    default = ScriptedClient(response(text("ok")))
    await Agent(default, build_service(case)).ask(Conversation(), "hi")
    assert "output_config" not in default.calls[0]


@pytest.mark.asyncio
async def test_run_case_uses_the_given_model_and_effort():
    client = ScriptedClient(response(text("ok")))
    await run_case(_case(), client, model="claude-haiku-5-5", effort="medium")
    assert client.calls[0]["model"] == "claude-haiku-5-5"
    assert client.calls[0]["output_config"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_run_all_passes_the_model_to_every_case():
    client = ScriptedClient(response(text("a")), response(text("b")))
    await run_all([_case(), _case()], client, concurrency=1, model="claude-haiku-5-5")
    assert [c["model"] for c in client.calls] == ["claude-haiku-5-5", "claude-haiku-5-5"]


def test_haiku_has_a_published_price_and_an_unknown_model_still_has_none():
    # $0.10 in / $0.50 out per million tokens, for prompts up to 100,000 tokens
    assert cost_usd("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert cost_usd("some-other-model", 1, 1) is None


def test_totals_line_names_and_prices_the_model_that_ran():
    o = [CaseOutcome("a", True, [], "", [], 1_000_000, 100_000)]
    assert (
        totals_line(o, model="claude-haiku-5-5")
        == "1/1 passed; tokens in=1000000 out=100000; cost about $0.15 (claude-haiku-5-5)"
    )
