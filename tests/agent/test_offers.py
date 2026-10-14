"""The file details of a tool result (`_offers`) go to the reply, never to the model (Spec 14)."""

import json
from datetime import datetime

from easytop.agent.core import Agent, Conversation
from easytop.agent.guardrails import trim_tool_output
from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.family.service import FamilyService
from easytop.files import file_payload
from easytop.models import FileOffer
from tests.agent.fakes import ScriptedClient, response, text, tool_use

NOW = datetime(2026, 10, 5, 18, 42)
FILE_MSG = {
    "id": "f1",
    "kid_id": None,
    "sender": "Dana Levi",
    "subject": "Quiz sheet",
    "body": "Do the attached sheet before the next lesson.",
    "sent_days_ago": 2,
    "unread": False,
    "has_attachments": True,
    "files": [{"name": "zebra plan", "ext": ".pdf", "size_kb": 433, "content": "%PDF-1.4 x"}],
}


def agent(client, *messages):
    data = load_synthetic()
    data["messages"].extend(messages)
    svc = FamilyService(FakeConnector(NOW.date(), data), clock=lambda: NOW)
    return Agent(client, svc, clock=lambda: NOW)


def test_trim_never_passes_the_private_offers_on():
    out = json.loads(
        trim_tool_output({"messages": [], "_offers": {"m1": [{"name": "secret.pdf"}]}})
    )
    assert "_offers" not in out and "secret.pdf" not in json.dumps(out)


async def test_offers_reach_the_result_and_not_the_model():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief")),
        response(text("Do the sheet (message from Dana Levi, 3.10) {{m1}}")),
    )
    r = await agent(c, FILE_MSG).ask(Conversation(), "homework?")
    assert r.offers == {
        "m1": (FileOffer("zebra plan", ".pdf", 433, "Dana Levi", "3.10", file_payload("f1", 0)),)
    }
    seen_by_model = c.calls[1]["messages"][-1]["content"][0]["content"]
    for hidden in ("_offers", "zebra plan", file_payload("f1", 0), "433"):
        assert hidden not in seen_by_model
    assert '"ref": "m1"' in seen_by_model and '"files": 1' in seen_by_model


async def test_the_answer_keeps_its_markers_for_rendering_but_the_history_does_not():
    conv = Conversation()
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief")),
        response(text("Do the sheet (message from Dana Levi, 3.10) {{m1}}")),
    )
    r = await agent(c, FILE_MSG).ask(conv, "homework?")
    assert "{{m1}}" in r.text
    assert "{{" not in conv.messages[-1]["content"]
    assert conv.messages[-1]["content"] == "Do the sheet (message from Dana Levi, 3.10)"


async def test_offers_of_several_tool_calls_are_merged():
    c = ScriptedClient(
        response(tool_use("t1", "get_tomorrow_brief")),
        response(tool_use("t2", "get_messages")),
        response(text("ok")),
    )
    r = await agent(c, FILE_MSG).ask(Conversation(), "anything from the teacher?")
    assert list(r.offers) == ["m1"] and len(r.offers["m1"]) == 1


async def test_no_files_no_offers():
    c = ScriptedClient(response(tool_use("t1", "get_tomorrow_brief")), response(text("ok")))
    r = await agent(c).ask(Conversation(), "homework?")
    assert r.offers == {}
