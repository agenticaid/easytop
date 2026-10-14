"""Privacy, end to end (Spec 07, usage analytics): recognisable secrets go through the real gateway, onboarding,
registry, invites, nudge settings, health check and the Telegram error path, with fakes only (scripted Claude
client, FakeConnector on a planted family). Not one of them may appear in ANY log record, formatted as text or as
JSON, nor in any analytics event. The Telegram id may appear in the ops lines but never in an analytics event."""

import json
import logging
from datetime import date, datetime
from types import SimpleNamespace as NS

from easytop.access.invites import InviteService
from easytop.adapters.gateway import WELCOME, Gateway
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.adapters.telegram import TelegramAdapter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.family.service import FamilyService
from easytop.health import SessionHealth
from easytop.logjson import JsonFormatter
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.flow import OnboardingFlow
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry
from easytop.onboarding.vault import SessionVault
from easytop.store.memory import InMemoryStore
from tests.agent.fakes import ScriptedClient, response, text, tool_use

TODAY = date(2026, 10, 4)  # a Sunday: the FakeConnector family has school tomorrow
NOW = datetime(2026, 10, 4, 18, 42)
ADMIN, GUEST = "telegram:7000000001", "telegram:7000000002"

KID_LATIN, KID_HEBREW = "Zilberstein", "צליליתה"
SECRETS = {
    "kid name": KID_LATIN,
    "kid name (Hebrew)": KID_HEBREW,
    "question": "SECRET-QUESTION-about-the-trip",
    "answer": "SECRET-ANSWER-bring-the-form",
    "homework": "SECRET-HOMEWORK-page-777",
    "teacher": "Ms. Secretteacher",
    "school": "Secret Valley School",
    "cookie": "SECRET-COOKIE-VALUE-9f8e7d",
    "phone": "0527654321",
    "id number": "039876543",
}


def planted_family() -> dict:
    data = load_synthetic()
    data["kids"][0]["name"] = f"{KID_LATIN} {KID_HEBREW}"
    data["kids"][0]["school"]["name"] = SECRETS["school"]
    for day in data["timetable"]["k1"].values():
        for lesson in day:
            lesson["teacher"] = SECRETS["teacher"]
    data["homework"][0]["description"] = SECRETS["homework"]
    data["messages"][0]["body"] = f"Call {SECRETS['phone']}, ID {SECRETS['id number']}"
    return data


class Everything(logging.Handler):
    """Every record from every logger, at every level."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


async def test_no_secret_reaches_any_log_line_or_analytics_event():
    root = logging.getLogger()
    sink, old_level = Everything(), root.level
    root.addHandler(sink)
    root.setLevel(logging.DEBUG)
    try:
        await run_everything()
    finally:
        root.removeHandler(sink)
        root.setLevel(old_level)

    as_text = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    lines = [as_text.format(r) for r in sink.records] + [
        JsonFormatter().format(r) for r in sink.records
    ]
    events = [r.analytics for r in sink.records if hasattr(r, "analytics")]
    assert len(events) >= 10, "the scenario must actually produce analytics events"
    blob = "\n".join(lines) + "\n" + json.dumps(events, ensure_ascii=False)
    for label, secret in SECRETS.items():
        assert secret not in blob, f"{label} leaked into the logs or analytics"
    # The Telegram ids may appear in the ops lines (debugging) but never in an analytics event.
    event_blob = json.dumps(events)
    for pid in (ADMIN, GUEST):
        assert pid.split(":")[1] not in event_blob
    seen = {e["event"] for e in events}
    assert {
        "question",
        "invite_created",
        "invite_redeemed",
        "connect_offered",
        "kids_confirmed",
        "nudge_setting",
        "session_check",
        "error",
    } <= seen


async def run_everything():
    store = InMemoryStore()
    vault = SessionVault(store, SessionCipher(new_key()))
    family = planted_family()

    def connector_factory(cookies, on_expired):
        return FakeConnector(TODAY, data=family)

    client = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief", kid=KID_LATIN, focus="equipment")),
        response(tool_use("t2", "get_messages", kid=KID_HEBREW)),
        response(text(f"{SECRETS['answer']} for {KID_LATIN} {KID_HEBREW} {SECRETS['phone']}")),
        response(text(SECRETS["answer"])),
    )

    def agent_factory(connector):
        return Agent(client, FamilyService(connector, clock=lambda: NOW), clock=lambda: NOW)

    registry = AgentRegistry(
        store, vault, connector_factory=connector_factory, agent_factory=agent_factory
    )
    flow = OnboardingFlow(
        store,
        vault,
        registry,
        ConnectLinks(),
        connector_factory=connector_factory,
        connect_url=lambda t: f"https://login.example/?t={t}",
        welcome=WELCOME,
    )
    allow = Allowlist({ADMIN})
    invites = InviteService(store, allow, link=lambda p: f"https://t.me/your_dev_bot?start={p}")
    gw = Gateway(
        allow,
        RateLimiter(30, clock=lambda: NOW),
        registry.agent_for,
        onboarding=flow,
        registry=registry,
        invites=invites,
    )

    async def notify(pid, reply):
        pass

    flow.notify = notify

    # an invite from the admin, redeemed by the guest; a stranger with a secret in the message
    invite_reply = await gw.handle(Incoming(ADMIN, text="invite"))
    payload = invite_reply.text.split("start=")[1].split()[0]
    await gw.handle(Incoming(GUEST, text=f"/start {payload}"))
    await gw.handle(Incoming("telegram:1", text=f"my phone is {SECRETS['phone']}"))

    # the guest connects (the login page hands over a cookie), confirms the kids, picks a reminder time
    await gw.handle(Incoming(GUEST, text="/start"))
    await flow.on_connected(GUEST, {"webToken": SECRETS["cookie"]})
    await gw.handle(Incoming(GUEST, text="כן"))
    await gw.handle(Incoming(GUEST, text="19:30"))

    # two real questions with secrets in them, answered through tool calls on the planted family
    await gw.handle(
        Incoming(GUEST, text=f"{SECRETS['question']} {KID_HEBREW} {SECRETS['id number']}")
    )
    await gw.handle(Incoming(GUEST, text=f"and {KID_LATIN}? {SECRETS['question']}"))

    # the hourly health check over the stored session, and a Telegram error whose text holds a secret
    async def no_wait(seconds):
        pass

    health = SessionHealth(store, registry)
    health._sleep = no_wait
    await health.tick()
    adapter = TelegramAdapter(gw)
    await adapter.on_error(
        None, NS(error=RuntimeError(f"token {SECRETS['cookie']} {SECRETS['phone']}"))
    )
