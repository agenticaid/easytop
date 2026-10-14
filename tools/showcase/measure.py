"""Time, tokens and cost of four fixed questions. Numbers only: the answers are never printed or stored, only
their length.

    ANTHROPIC_API_KEY=... uv run python -m tools.showcase.measure --session /absolute/path/cookies.json

Without --session it runs on the SYNTHETIC family (to check the tool). Each question starts a new conversation, in
this order, like separate parents; the first one may pay the prompt-cache write."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime

from easytop.agent.core import Agent, Conversation
from easytop.cli import SYNTHETIC, build_live_connector
from easytop.connector.base import Connector
from easytop.connector.fake import FakeConnector
from easytop.connector.session_file import load_cookies
from easytop.family.service import FamilyService

QUESTIONS: tuple[tuple[str, str], ...] = (
    ("full tomorrow answer (Hebrew)", "מה הילדים צריכים מחר?"),
    ("homework only (Hebrew)", "מה שיעורי הבית למחר?"),
    ("exams (English)", "Any exams coming up?"),
    ("equipment only (Hebrew)", "איזה ציוד צריך להביא מחר?"),
)


@dataclass
class Measurement:
    label: str
    seconds: float
    tool_calls: int
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    cost_usd: float | None
    answer_chars: int
    error: str | None


async def measure(agent: Agent, questions=QUESTIONS, clock=time.perf_counter) -> list[Measurement]:
    rows = []
    for label, question in questions:
        start = clock()
        r = await agent.ask(Conversation(), question)
        rows.append(
            Measurement(
                label,
                round(clock() - start, 1),
                len(r.tool_calls),
                r.input_tokens,
                r.cache_write_tokens,
                r.cache_read_tokens,
                r.output_tokens,
                r.cost_usd,
                len(r.text),
                r.error,
            )
        )
    return rows


def table(rows: list[Measurement]) -> str:
    lines = [
        "| Question | Time (s) | Tool calls | Tokens in / cache write / cache read / out | Cost (USD) | Answer chars |",
        "|---|---|---|---|---|---|",
    ]
    for m in rows:
        cost = "unknown" if m.cost_usd is None else f"{m.cost_usd:.3f}"
        tokens = f"{m.input_tokens} / {m.cache_write_tokens} / {m.cache_read_tokens} / {m.output_tokens}"
        lines.append(f"| {m.label} | {m.seconds:.1f} | {m.tool_calls} | {tokens} | {cost} | {m.answer_chars} |")
    total = sum(m.cost_usd or 0 for m in rows)
    errors = sum(1 for m in rows if m.error)
    lines.append(f"\nTotal {total:.3f} USD for {len(rows)} questions; errors: {errors}.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="measure", description="Time, tokens and cost of fixed questions")
    ap.add_argument("--session", help="cookie file of a real account (absolute path); default: synthetic family")
    args = ap.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("error: set ANTHROPIC_API_KEY (the questions call the paid Claude API)")
        return 2
    now = datetime.now()
    connector: Connector = (
        build_live_connector(load_cookies(args.session), now.date()) if args.session else FakeConnector(now.date())
    )

    async def run() -> list[Measurement]:
        import anthropic

        agent = Agent(anthropic.AsyncAnthropic(api_key=key), FamilyService(connector, clock=lambda: now))
        try:
            return await measure(agent)
        finally:
            close = getattr(connector, "aclose", None)
            if close:
                await close()

    rows = asyncio.run(run())
    print(f"Data: {'real account (read-only)' if args.session else SYNTHETIC}; measured {now:%Y-%m-%d %H:%M}")
    print(table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
