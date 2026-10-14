"""CLI chat with the agent (task 3.6). Without --session it uses the FAKE connector (synthetic data);
with --session FILE it reads the real Webtop account (read-only calls; opening a message marks it read).

Needs ANTHROPIC_API_KEY (paid API). Prices are never hardcoded: pass --price-in / --price-out (USD per
million tokens) to see cost per question.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from easytop.agent.core import Agent, Conversation
from easytop.cli import SYNTHETIC, build_live_connector
from easytop.connector.base import Connector
from easytop.connector.fake import FakeConnector
from easytop.connector.session_file import load_cookies
from easytop.errors import EasytopError
from easytop.family.service import FamilyService

QUIT = {"/quit", "/exit"}


async def run_chat(agent: Agent, read: Callable[[str], str], write: Callable[[str], Any]) -> None:
    convo, questions, tin, tout, cost = Conversation(), 0, 0, 0, 0.0
    priced = False
    while True:
        try:
            line = (await asyncio.to_thread(read, "> ")).strip()
        except EOFError:
            break
        if line.lower() in QUIT:
            break
        if not line:
            continue
        r = await agent.ask(convo, line)
        questions += 1
        tin, tout = tin + r.input_tokens, tout + r.output_tokens
        write(r.text)
        info = f"[tools: {', '.join(r.tool_calls) or 'none'}] tokens in={r.input_tokens} out={r.output_tokens}"
        if r.cost_usd is not None:
            priced, cost = True, cost + r.cost_usd
            info += f" cost ${r.cost_usd:.4f}"
        write(info)
    total = f"session total: {questions} question{'s' if questions != 1 else ''}, tokens in={tin} out={tout}"
    write(total + (f", cost ${cost:.4f}" if priced else ""))


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(prog="chat", description="Chat with the Easytop agent")
    ap.add_argument("--session", help="saved cookie file: use the real Webtop account (read-only)")
    ap.add_argument("--price-in", type=float, help="USD per million input tokens")
    ap.add_argument("--price-out", type=float, help="USD per million output tokens")
    args = ap.parse_args(argv)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("error: set ANTHROPIC_API_KEY (the chat calls the paid Claude API)")
        return 2
    now = now or datetime.now()
    connector: Connector
    if args.session:
        try:
            connector = build_live_connector(load_cookies(args.session), now.date())
        except EasytopError as e:
            print(f"error: {e}")
            return 2
        print("Data: real Webtop account (live, read-only)")
    else:
        connector = FakeConnector(now.date())
        print(f"Data: {SYNTHETIC}")
    prices = (
        (args.price_in, args.price_out)
        if args.price_in is not None and args.price_out is not None
        else None
    )

    async def run() -> None:
        import anthropic

        agent = Agent(
            anthropic.AsyncAnthropic(api_key=key),
            FamilyService(connector, clock=lambda: now),
            prices_per_mtok=prices,
        )
        try:
            print("Ask about the kids. /quit to leave.")
            await run_chat(agent, input, print)
        finally:  # close on the loop that opened the connections
            close = getattr(connector, "aclose", None)
            if close:
                await close()

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
