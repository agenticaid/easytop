"""Once-only onboarding against the real dev database (EASYTOP_LIVE_STORE=1, SUPABASE_URL_DEV / SUPABASE_SERVICE_KEY_DEV /
SESSION_ENCRYPTION_KEY set): a bot restart is a new SupabaseStore and a new set of objects over the same database.
Uses a throwaway parent id and removes it afterwards. Never point this at the production project."""

import os
import uuid

import pytest

from easytop.onboarding.flow import RECONNECTED
from easytop.onboarding.registry import store_parent
from easytop.store.supabase import SupabaseStore
from tests.onboarding.test_once_only import Process

pytestmark = pytest.mark.skipif(
    os.environ.get("EASYTOP_LIVE_STORE") != "1",
    reason="live store test: set EASYTOP_LIVE_STORE=1 with the Supabase and SESSION_ENCRYPTION_KEY env vars",
)


@pytest.fixture
async def run():
    """Start one 'bot process' over the dev database; every one is closed (and the parent removed) at the end."""
    key = os.environ["SESSION_ENCRYPTION_KEY"]
    pid = f"telegram:livetest-{uuid.uuid4()}"
    stores: list[SupabaseStore] = []

    def start() -> Process:
        s = SupabaseStore.from_env()
        stores.append(s)
        return Process(None, key, store=s)

    yield pid, start
    cleanup = SupabaseStore.from_env()
    try:
        parent = await cleanup.find_parent(*pid.split(":", 1))
        if parent is not None:
            await cleanup.delete_parent(parent.id)
    finally:
        await cleanup.aclose()
        for s in stores:
            await s.aclose()


async def test_restart_keeps_a_connected_parent_and_onboarding_runs_once(run):
    pid, start = run
    first = start()
    await first.flow.on_connected(pid, {"webToken": "tok"})
    await first.say("כן", pid)  # first time only: the kids are confirmed

    for _ in range(3):  # restart the bot three times
        again = start()
        assert await again.say("מה הילדים צריכים מחר?", pid) is None  # no connect button, no questions
        assert await again.registry.agent_for(pid) is not None
        assert again.pushed == []


async def test_expiry_then_restart_asks_only_for_the_sign_in(run):
    pid, start = run
    first = start()
    await first.flow.on_connected(pid, {"webToken": "tok"})
    await first.say("כן", pid)
    sp = await store_parent(first.store, pid)
    await first.registry._on_expired(pid, sp.id)

    again = start()
    reply = await again.say("מה מחר?", pid)
    assert reply is not None and reply.link is not None
    await again.flow.on_connected(pid, {"webToken": "fresh"})
    assert again.pushed[-1].text == RECONNECTED
    assert await again.say("מה מחר?", pid) is None
