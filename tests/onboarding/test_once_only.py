"""A parent onboards only the first time: a restart changes nothing, and a reconnect after an expiry asks only
for the sign-in again, never for the kids to be confirmed again."""

from datetime import date

from easytop.adapters.gateway import WELCOME
from easytop.adapters.model import Incoming
from easytop.connector.fake import FakeConnector
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.flow import RECONNECTED, OnboardingFlow
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry, store_parent
from easytop.onboarding.vault import SessionVault
from easytop.store.file import FileStore

PID = "telegram:1"


class StubAgent:
    """Stands in for an Agent where only 'a session exists' matters."""

    def bind_reminders(self, reminders):
        pass


class Process:
    """One run of the bot over the same store file and key."""

    def __init__(self, path, key, store=None):
        self.store = store or FileStore(path)
        self.vault = SessionVault(self.store, SessionCipher(key))

        def make(cookies, hook):
            return FakeConnector(date(2026, 10, 4))

        self.registry = AgentRegistry(
            self.store, self.vault, connector_factory=make, agent_factory=lambda c: StubAgent()
        )
        self.flow = OnboardingFlow(
            self.store,
            self.vault,
            self.registry,
            ConnectLinks(),
            connector_factory=make,
            connect_url=lambda t: f"https://login.example/?t={t}",
            welcome=WELCOME,
        )
        self.pushed = []

        async def notify(pid, reply):
            self.pushed.append(reply)

        self.flow.notify = notify

    async def say(self, text, pid=PID):
        return await self.flow.pre_agent(Incoming(pid, text=text))


async def test_after_a_restart_a_connected_parent_goes_straight_to_the_agent(tmp_path):
    path, key = tmp_path / "store.json", new_key()
    first = Process(path, key)
    await first.flow.on_connected(PID, {"webToken": "tok"})
    await first.say("כן")  # first time only: the kids are confirmed

    for _ in range(3):  # restart the bot three times
        again = Process(path, key)
        assert await again.say("מה הילדים צריכים מחר?") is None  # no connect button, no questions
        assert await again.registry.agent_for(PID) is not None
        assert again.pushed == []


async def test_after_an_expiry_and_a_restart_only_the_sign_in_is_asked_again(tmp_path):
    path, key = tmp_path / "store.json", new_key()
    first = Process(path, key)
    await first.flow.on_connected(PID, {"webToken": "tok"})
    await first.say("כן")
    sp = await store_parent(first.store, PID)
    await first.registry._on_expired(PID, sp.id)  # Webtop ended the session

    again = Process(path, key)  # restarted before the parent came back
    reply = await again.say("מה מחר?")
    assert reply is not None and reply.link is not None  # the sign-in button, nothing else
    await again.flow.on_connected(PID, {"webToken": "fresh"})
    assert again.pushed[-1].text == RECONNECTED  # same kids: no confirmation question
    assert await again.say("מה מחר?") is None
