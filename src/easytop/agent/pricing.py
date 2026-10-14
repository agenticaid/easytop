"""Token prices, in USD per million tokens (input, output), from Anthropic's published pricing page.

Looked up 2026-10-01: Claude Sonnet 5.5 = $2 / MTok input, $10 / MTok output (cache hits $0.20, 5-minute
cache writes $2.50). Looked up 2026-10-14: Claude Haiku 5.5 = $0.10 / MTok input, $0.50 / MTok output for
prompts up to 100,000 tokens (a longer prompt pays more; this agent's prompts are about 20,000 tokens). A model
that is not listed has NO price: cost is then unknown, never guessed.
Re-check the page when the model changes.
"""

from __future__ import annotations

PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),  # prompts up to 100,000 tokens
}


# Prompt caching, relative to the base input price (same page): a 5-minute cache write costs 1.25x, a hit 0.1x.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_write: int = 0,
    cache_read: int = 0,
) -> float | None:
    """`input_tokens` are the uncached ones (the API reports cache writes and reads separately)."""
    prices = PRICES_PER_MTOK.get(model)
    if prices is None:
        return None
    price_in, price_out = prices
    return (
        input_tokens * price_in
        + cache_write * price_in * CACHE_WRITE_MULTIPLIER
        + cache_read * price_in * CACHE_READ_MULTIPLIER
        + output_tokens * price_out
    ) / 1e6
