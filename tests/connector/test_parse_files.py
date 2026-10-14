"""The files listed in an opened message (messageBox/GetMessagesInboxData -> messageData.filesList)."""

import copy
import json
from pathlib import Path

from easytop.connector import parse

FIXTURES = Path(__file__).parent.parent / "fixtures" / "webtop"
URL = "https://webtopserver.smartschool.co.il/server/api/stream/GetFile?token=abc"


def resp(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["response"]


def message(**md) -> dict:
    r = copy.deepcopy(resp("messageBox__GetMessagesInboxData__02"))
    r["data"]["messageData"].update(md)
    return r


def entry(**kw) -> dict:
    return {
        "id": 0,
        "fileName": "worksheet",
        "fileKey": "k",
        "fileUrl": URL,
        "fileExtantion": ".pdf",
        "fileSize": {"size": 433.5, "sizeType": 2, "sizeName": "KB"},
    } | kw


def test_files_of_a_message_with_five_attachments_keep_their_position():
    body = parse.parse_message_body(resp("messageBox__GetMessagesInboxData__02"))
    assert [f.index for f in body.files] == [0, 1, 2, 3, 4]
    assert [f.ext for f in body.files] == [".pdf"] * 5
    assert body.files[0].name == "Synthetic text 98"
    assert (
        body.files[0].url == "Synthetic text 105"
    )  # kept as listed; checked only when downloading


def test_size_is_converted_to_bytes():
    r = message(
        filesList=[
            entry(fileSize={"size": 433.0439453125, "sizeName": "KB"}),
            entry(fileSize={"size": 1.7399559020996094, "sizeName": "MB"}),
            entry(fileSize={"size": 12, "sizeName": "B"}),
            entry(fileSize={"size": 1, "sizeName": "furlong"}),
        ]
    )
    sizes = [f.size_bytes for f in parse.parse_message_body(r).files]
    # 433.04 KiB, 1.74 MiB, 12 bytes; an unknown unit is 0 (unknown), never a guess
    assert sizes == [443437, 1824476, 12, 0]


def test_a_message_without_a_file_list_has_no_files():
    r = message()
    del r["data"]["messageData"]["filesList"]
    assert parse.parse_message_body(r).files == ()
    assert parse.parse_message_body(message(filesList=None)).files == ()
    assert parse.parse_message_body(message(filesList=[])).files == ()


def test_a_damaged_entry_is_skipped_and_the_others_keep_their_index():
    bad_no_url = entry()
    del bad_no_url["fileUrl"]
    r = message(filesList=[entry(), bad_no_url, "junk", entry(fileName="c", fileExtantion=None)])
    files = parse.parse_message_body(r).files
    assert [f.index for f in files] == [0, 3]
    assert files[1].ext == ""  # no extension listed


def test_at_most_ten_files_are_read():
    r = message(filesList=[entry() for _ in range(14)])
    assert [f.index for f in parse.parse_message_body(r).files] == list(range(10))


def test_the_message_text_is_still_parsed_when_the_file_list_is_odd():
    r = message(filesList="oops")
    body = parse.parse_message_body(r)
    assert body.files == () and body.has_files is True
