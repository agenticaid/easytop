"""Messages with files (Spec 14): the model gets a short `ref` and a count, code keeps the file details in a private
`_offers` entry for the reply, and a file is fetched by the hash of its message id."""

import json

import pytest

from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.errors import FileUnavailable, SessionExpired
from easytop.family.service import FamilyService
from easytop.files import file_payload, message_hash
from tests.family.test_service import NOW

LONG_ID = "1" + "0" * 99 + "7033"


def msg(
    mid, *, files=(), sender="Dana Levi", days_ago=2, body="See the attached page.", subject="Page"
):
    return {
        "id": mid,
        "kid_id": None,
        "sender": sender,
        "subject": subject,
        "body": body,
        "sent_days_ago": days_ago,
        "unread": False,
        "has_attachments": bool(files),
        "files": list(files),
    }


PDF = {"name": "zebra plan", "ext": ".pdf", "size_kb": 433, "content": "%PDF-1.7 fake"}
DOC = {"name": "דף עבודה", "ext": ".docx", "size_kb": 18}


def service(*extra):
    data = load_synthetic()
    data["messages"].extend(extra)
    conn = FakeConnector(NOW.date(), data)
    return conn, FamilyService(conn, clock=lambda: NOW)


async def test_a_message_with_files_has_a_ref_and_a_count_and_nothing_else_about_them():
    _, svc = service(msg(LONG_ID, files=[PDF, DOC]))
    out = await svc.get_tomorrow_brief()
    m = next(m for m in out["messages"] if m["id"] == LONG_ID)
    assert m["ref"].startswith("m") and m["files"] == 2
    seen = json.dumps({k: v for k, v in out.items() if k != "_offers"}, ensure_ascii=False)
    for secret in ("zebra plan", "דף עבודה", ".pdf", "433", file_payload(LONG_ID, 0)):
        assert secret not in seen


async def test_a_message_without_files_has_neither_ref_nor_count():
    _, svc = service(msg("plain1", files=[]))
    m = next(m for m in (await svc.get_tomorrow_brief())["messages"] if m["id"] == "plain1")
    assert "ref" not in m and "files" not in m


async def test_the_offers_describe_each_file_for_the_reply():
    _, svc = service(msg(LONG_ID, files=[PDF, DOC], days_ago=2, sender="Dana Levi"))
    out = await svc.get_tomorrow_brief()
    ref = next(m["ref"] for m in out["messages"] if m["id"] == LONG_ID)
    day = (NOW - __import__("datetime").timedelta(days=2)).date()
    assert out["_offers"][ref] == [
        {
            "name": "zebra plan",
            "ext": ".pdf",
            "size_kb": 433,
            "sender": "Dana Levi",
            "sent": f"{day.day}.{day.month}",
            "payload": file_payload(LONG_ID, 0),
        },
        {
            "name": "דף עבודה",
            "ext": ".docx",
            "size_kb": 18,
            "sender": "Dana Levi",
            "sent": f"{day.day}.{day.month}",
            "payload": file_payload(LONG_ID, 1),
        },
    ]


async def test_no_offers_entry_when_no_message_has_files():
    _, svc = service(msg("plain1"))
    assert "_offers" not in await svc.get_tomorrow_brief()


async def test_a_message_keeps_its_ref_across_calls_and_two_messages_differ():
    _, svc = service(
        msg("a1", files=[PDF]), msg("b1", files=[DOC], days_ago=3, body="A different note.")
    )
    first = {m["id"]: m["ref"] for m in (await svc.get_tomorrow_brief())["messages"] if "ref" in m}
    second = {m["id"]: m["ref"] for m in (await svc.get_messages())["messages"] if "ref" in m}
    assert first == second and len(set(first.values())) == 2


async def test_get_messages_has_refs_and_offers_too():
    _, svc = service(msg("a1", files=[PDF]))
    out = await svc.get_messages()
    ref = next(m["ref"] for m in out["messages"] if m["id"] == "a1")
    assert out["_offers"][ref][0]["payload"] == file_payload("a1", 0)


async def test_a_duplicate_message_is_offered_once():
    _, svc = service(msg("a1", files=[PDF]), msg("a2", files=[PDF], days_ago=3))
    out = await svc.get_tomorrow_brief()
    assert len(out["_offers"]) == 1  # identical sender, subject and text collapse to the newest


async def test_a_message_that_says_it_has_files_but_lists_none_gets_no_ref():
    data = msg("odd1")
    data["has_attachments"] = True  # e.g. an unread message that was not opened
    _, svc = service(data)
    m = next(m for m in (await svc.get_tomorrow_brief())["messages"] if m["id"] == "odd1")
    assert m["has_attachments"] is True and "ref" not in m


async def test_get_file_returns_the_file_found_by_the_hash():
    _, svc = service(msg(LONG_ID, files=[PDF]))
    f = await svc.get_file(message_hash(LONG_ID), 0)
    assert (f.name, f.data, f.mime) == ("zebra plan.pdf", b"%PDF-1.7 fake", "application/pdf")


async def test_get_file_for_an_unknown_message_is_not_found():
    _, svc = service(msg(LONG_ID, files=[PDF]))
    with pytest.raises(FileUnavailable) as e:
        await svc.get_file(message_hash("someone else's message"), 0)
    assert e.value.reason == "not_found"


async def test_get_file_surfaces_an_expired_session():
    conn = FakeConnector(NOW.date(), load_synthetic(), expired=True)
    with pytest.raises(SessionExpired):
        await FamilyService(conn, clock=lambda: NOW).get_file(message_hash("x"), 0)
