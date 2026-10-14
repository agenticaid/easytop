"""Tapping a file link (`/start file_<hash><n>`) sends the file without the model; answers carry their offers."""

import logging
from datetime import datetime

import pytest

from easytop.adapters.gateway import (
    FILE_ERROR,
    FILE_GONE,
    FILE_TOO_BIG,
    FILE_TYPE,
    WELCOME,
    Gateway,
)
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import FileUnavailable, SessionExpired, UpstreamError
from easytop.family.service import FamilyService
from easytop.files import file_payload
from tests.agent.fakes import ScriptedClient, response, text, tool_use

NOW = datetime(2026, 10, 5, 18, 42)
PARENT = "telegram:100"
MSG_ID = "1" + "0" * 99 + "7033"
FILE_MSG = {
    "id": MSG_ID,
    "kid_id": None,
    "sender": "Dana Levi",
    "subject": "Quiz sheet",
    "body": "Do the attached sheet before the next lesson.",
    "sent_days_ago": 2,
    "unread": False,
    "has_attachments": True,
    "files": [
        {"name": "zebra plan", "ext": ".pdf", "size_kb": 433, "content": "%PDF-1.4 bytes"},
        {"name": "run", "ext": ".exe", "size_kb": 1},
    ],
}
TAP = f"/start {file_payload(MSG_ID, 0)}"


def family():
    data = load_synthetic()
    data["messages"].append(FILE_MSG)
    conn = FakeConnector(NOW.date(), data)
    return conn, FamilyService(conn, clock=lambda: NOW)


def make(client=None, *, svc=None, file_limit=30, onboarding=None, allowed=(PARENT,)):
    conn, real = family()
    client = client or ScriptedClient()
    agent = Agent(client, svc or real, clock=lambda: NOW)
    gw = Gateway(
        Allowlist(set(allowed)),
        RateLimiter(30, clock=lambda: NOW),
        lambda pid: agent,
        onboarding=onboarding,
        clock=lambda: NOW,
        file_limiter=RateLimiter(file_limit, clock=lambda: NOW),
    )
    return gw, client, conn


async def test_a_tap_sends_the_file_and_never_calls_the_model():
    gw, client, _ = make()
    r = await gw.handle(Incoming(PARENT, text=TAP))
    (doc,) = r.documents
    assert (doc.name, doc.data, doc.mime) == (
        "zebra plan.pdf",
        b"%PDF-1.4 bytes",
        "application/pdf",
    )
    assert r.text == "" and client.calls == []


async def test_a_stranger_cannot_fetch_anything():
    gw, client, conn = make(allowed=())
    r = await gw.handle(Incoming(PARENT, text=TAP))
    assert not r.documents and ("get_message_file", None) not in conn.calls


async def test_a_message_that_is_gone_is_explained_in_both_languages():
    gw, _, _ = make()
    r = await gw.handle(Incoming(PARENT, text="/start " + file_payload("not here", 0)))
    assert r.text == FILE_GONE and not r.documents
    assert "הקובץ" in FILE_GONE and "file" in FILE_GONE


async def test_a_refused_type_is_explained():
    gw, _, _ = make()
    r = await gw.handle(Incoming(PARENT, text="/start " + file_payload(MSG_ID, 1)))
    assert r.text == FILE_TYPE and not r.documents


class Stub:
    def __init__(self, error):
        self.error = error

    async def get_file(self, h, i):
        raise self.error


@pytest.mark.parametrize(
    "error, expected",
    [
        (FileUnavailable("too_big"), FILE_TOO_BIG),
        (FileUnavailable("type_refused"), FILE_TYPE),
        (FileUnavailable("not_found"), FILE_GONE),
        (UpstreamError("down"), FILE_ERROR),
    ],
)
async def test_each_failure_has_its_own_plain_message(error, expected):
    gw, _, _ = make(svc=Stub(error))
    r = await gw.handle(Incoming(PARENT, text=TAP))
    assert r.text == expected and not r.documents


async def test_an_expired_session_goes_to_the_reconnect_notice():
    class Onboarding:
        async def pre_agent(self, msg):
            return None

        async def expired_reply(self, pid):
            from easytop.adapters.model import Reply

            return Reply(text="reconnect please")

    gw, _, _ = make(svc=Stub(SessionExpired("x")), onboarding=Onboarding())
    assert (await gw.handle(Incoming(PARENT, text=TAP))).text == "reconnect please"


async def test_taps_have_their_own_hourly_limit_separate_from_questions():
    gw, client, _ = make(file_limit=1)
    assert (await gw.handle(Incoming(PARENT, text=TAP))).documents
    r = await gw.handle(Incoming(PARENT, text=TAP))
    assert not r.documents and "limit" in r.text and client.calls == []


async def test_a_malformed_payload_is_an_ordinary_start():
    gw, _, _ = make()
    assert (await gw.handle(Incoming(PARENT, text="/start file_short1"))).text == WELCOME


async def test_an_answer_carries_the_offers_of_the_messages_it_cites():
    client = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief")),
        response(text("Do the sheet (message from Dana Levi, 3.10) {{m1}}")),
    )
    gw, _, _ = make(client)
    r = await gw.handle(Incoming(PARENT, text="homework?"))
    assert "{{m1}}" in r.text
    assert [o.name for o in r.offers["m1"]] == ["zebra plan", "run"]


async def test_nothing_about_the_file_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    gw, _, _ = make()
    await gw.handle(Incoming(PARENT, text=TAP))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "file sent" in logged
    for secret in ("zebra", "bytes", file_payload(MSG_ID, 0), "%PDF"):
        assert secret not in logged
