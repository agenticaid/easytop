"""Live evals: real Claude calls (paid). Skipped unless ANTHROPIC_API_KEY is set.

Run: ANTHROPIC_API_KEY=... uv run pytest tests/evals/test_live_evals.py -v
"""

import os

import pytest

from easytop.evals.cases import ALL_CASES
from easytop.evals.harness import SKIP_MESSAGE, live_client, run_case

pytestmark = pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason=SKIP_MESSAGE)


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda c: c.id)
async def test_live_case(case):
    outcome = await run_case(case, live_client())
    assert outcome.passed, f"{outcome.failures}\nANSWER:\n{outcome.answer}"
