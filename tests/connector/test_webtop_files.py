"""Attachments through the real connector against a fake Webtop (Spec 14): the file list of a message, and the
download of one file found again by the hash of its message id."""

import copy
from datetime import datetime

import httpx
import pytest

from easytop.connector.webtop import WebtopConnector
from easytop.errors import EndpointNotAllowed, FileUnavailable, SessionExpired
from easytop.files import message_hash
from tests.connector.test_webtop import TODAY, FakeWebtop, fx

GOOD_URL = "https://webtopserver.smartschool.co.il/server/api/stream/GetFile?token="
PDF = b"%PDF-1.7 fake worksheet"


def listing(name="worksheet", ext=".pdf", size=433.04, unit="KB", token="tok0", url=None):
    return {
        "id": 0,
        "fileName": name,
        "fileKey": "k",
        "fileUrl": url or GOOD_URL + token,
        "fileExtantion": ext,
        "fileSize": {"size": size, "sizeType": 2, "sizeName": unit},
    }


class FilesServer(FakeWebtop):
    def __init__(self, files=None, served=None):
        super().__init__()
        self.listing = files if files is not None else [listing()]
        self.served = served if served is not None else {"tok0": (200, PDF)}
        self.downloads: list[tuple[str, str]] = []  # (token in the URL, webToken cookie)
        inbox = copy.deepcopy(fx("messageBox__GetMessagesInbox__02")["response"])
        rows = inbox["data"]
        self.file_id = next(r["messageId"] for r in rows if r["filesWereAttached"])
        self.plain_id = next(r["messageId"] for r in rows if not r["filesWereAttached"])
        self.routes["messageBox/GetMessagesInbox"] = lambda b, t: inbox
        self.routes["messageBox/GetMessagesInboxData"] = self._open

    def _open(self, body, token):
        r = copy.deepcopy(fx("messageBox__GetMessagesInboxData__02")["response"])
        r["data"]["messageData"]["filesList"] = self.listing
        return r

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stream/GetFile"):
            cookies = dict(p.split("=", 1) for p in request.headers["cookie"].split("; "))
            tok = request.url.params["token"]
            self.downloads.append((tok, cookies.get("webToken", "")))
            status, data = self.served.get(tok, (404, b""))
            return httpx.Response(status, content=data)
        return super().handler(request)


def conn(server, **kw) -> WebtopConnector:
    return WebtopConnector(
        {"uniqueId": "u", "webToken": "login-token"},
        transport=httpx.MockTransport(server.handler),
        clock=lambda: TODAY,
        **kw,
    )


async def test_messages_list_their_files_without_the_address():
    server = FilesServer(
        files=[listing(name="a", size=433.04), listing(name="b", ext=".docx", size=2, unit="MB")]
    )
    (m, *_) = [m for m in await conn(server).get_messages(None, None, False) if m.has_attachments]
    assert [(f.index, f.name, f.ext, f.size_kb) for f in m.files] == [
        (0, "a", ".pdf", 433),
        (1, "b", ".docx", 2048),
    ]
    assert "tok0" not in repr(m) and "stream" not in repr(m)


async def test_a_file_is_found_by_the_hash_of_its_message_id_and_downloaded():
    server = FilesServer()
    f = await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert (f.name, f.data, f.mime) == ("worksheet.pdf", PDF, "application/pdf")
    # fetched with the PARENT context's token, never the login token
    assert server.downloads == [("tok0", f"token-{server.parent_ctx}")]
    assert server.endpoints()[-2:] == [
        "messageBox/GetMessagesInbox",
        "messageBox/GetMessagesInboxData",
    ]


@pytest.mark.parametrize("h, index", [("A" * 12, 0), (message_hash("not in the inbox"), 0)])
async def test_a_hash_that_matches_no_message_is_not_found(h, index):
    server = FilesServer()
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(h, index)
    assert e.value.reason == "not_found" and server.downloads == []


async def test_a_message_without_files_is_not_found_even_if_its_hash_matches():
    server = FilesServer()
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.plain_id), 0)
    assert e.value.reason == "not_found"
    assert "messageBox/GetMessagesInboxData" not in server.endpoints()


async def test_an_index_the_message_does_not_have_is_not_found():
    server = FilesServer()
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.file_id), 3)
    assert e.value.reason == "not_found" and server.downloads == []


async def test_a_refused_type_is_never_downloaded():
    server = FilesServer(files=[listing(name="run", ext=".exe")], served={"tok0": (200, b"MZ")})
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert e.value.reason == "type_refused" and server.downloads == []


async def test_the_extension_is_taken_from_the_name_when_the_listing_has_none():
    server = FilesServer(files=[listing(name="plan.pdf", ext="")])
    f = await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert f.name == "plan.pdf"


async def test_bytes_that_are_not_what_the_extension_says_are_refused():
    server = FilesServer(served={"tok0": (200, b"MZ\x90\x00 an executable named pdf")})
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert e.value.reason == "type_refused"


async def test_a_listed_size_over_the_cap_is_refused_before_downloading():
    server = FilesServer(files=[listing(size=21, unit="MB")])
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert e.value.reason == "too_big" and server.downloads == []


async def test_a_download_over_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr("easytop.connector.webtop.MAX_FILE_BYTES", 10)
    server = FilesServer(files=[listing(size=1, unit="B")])
    with pytest.raises(FileUnavailable) as e:
        await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert e.value.reason == "too_big"


async def test_an_address_outside_webtop_is_never_fetched():
    server = FilesServer(
        files=[listing(url="https://evil.example/server/api/stream/GetFile?token=x")]
    )
    with pytest.raises(EndpointNotAllowed):
        await conn(server).get_message_file(message_hash(server.file_id), 0)
    assert server.downloads == []


async def test_a_refused_session_during_the_download_is_a_session_expired():
    fired = []
    server = FilesServer(served={"tok0": (401, b"")})
    c = conn(server, on_expired=lambda: fired.append(1))
    with pytest.raises(SessionExpired):
        await c.get_message_file(message_hash(server.file_id), 0)
    assert fired


async def test_the_clock_is_not_needed_to_find_a_file():
    # the lookup reads the newest inbox page, whatever the date
    server = FilesServer()
    c = WebtopConnector(
        {"uniqueId": "u", "webToken": "login-token"},
        transport=httpx.MockTransport(server.handler),
        clock=lambda: datetime(2030, 1, 1).date(),
    )
    assert (await c.get_message_file(message_hash(server.file_id), 0)).data == PDF
