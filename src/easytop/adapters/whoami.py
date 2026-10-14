"""Find your numeric Telegram ID for the allowlist: `uv run --env-file .env python -m easytop.adapters.whoami`.

Message your own bot first (press Start, send "hi"). This reads the bot's pending updates and prints each
human sender's ID and first name, never message text and never the token. It does not consume the updates.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


def ids_from_updates(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Distinct human senders of messages, in first-seen order: {id, first_name}."""
    seen: dict[int, dict[str, Any]] = {}
    for u in updates:
        sender = (u.get("message") or {}).get("from") or {}
        if sender.get("is_bot") is False and isinstance(sender.get("id"), int):
            seen.setdefault(
                sender["id"], {"id": sender["id"], "first_name": sender.get("first_name", "")}
            )
    return list(seen.values())


def main(*, client: httpx.Client | None = None) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("error: set TELEGRAM_BOT_TOKEN in .env (run with: uv run --env-file .env ...)")
        return 2
    try:
        r = (client or httpx.Client(timeout=15)).get(
            f"https://api.telegram.org/bot{token}/getUpdates"
        )
        data = r.json()
    except (
        httpx.HTTPError,
        ValueError,
    ) as e:  # the message of an httpx error holds the URL, so never print it
        print(f"error: could not reach Telegram ({type(e).__name__})")
        return 2
    if not data.get("ok"):
        print(f"error: Telegram refused the request (HTTP {r.status_code}); is the token right?")
        return 2
    found = ids_from_updates(data.get("result", []))
    if not found:
        print(
            "No messages yet. Open your bot in Telegram, press Start, send 'hi', then run this again."
        )
        return 1
    for f in found:
        print(f"Telegram user id: {f['id']}  (first name: {f['first_name']})")
    print("Put the id in .env as TELEGRAM_ALLOWED_IDS=<id>  (several ids: comma separated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
