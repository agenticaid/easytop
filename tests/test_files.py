"""Pure helpers for inbox attachments (Spec 14): payloads, names, types, the one allowed download URL."""

import pytest

from easytop.files import (
    MAX_FILE_BYTES,
    content_matches,
    extension_allowed,
    file_payload,
    is_download_url,
    message_hash,
    mime_for,
    parse_file_start,
    safe_filename,
)

URL = "https://webtopserver.smartschool.co.il/server/api/stream/GetFile?token=" + "a" * 140
LONG_ID = "1" + "0" * 99 + "7111"


def test_message_hash_is_short_stable_and_url_safe():
    h = message_hash(LONG_ID)
    assert h == message_hash(LONG_ID) and len(h) == 12
    assert h != message_hash(LONG_ID + "1")
    assert all(c.isalnum() or c in "-_" for c in h)


def test_payload_fits_a_telegram_start_parameter_and_round_trips():
    p = file_payload(LONG_ID, 3)
    assert len(p) <= 64 and p.startswith("file_")
    assert parse_file_start(f"/start {p}") == (message_hash(LONG_ID), 3)


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/start inv_abcdef",
        "/start file_short1",  # wrong length
        "/start file_" + "A" * 12,  # no index
        "/start file_" + "A" * 12 + "x",  # index is not a digit
        "/start file_" + "A" * 12 + "1 extra",
        "hello file_" + "A" * 12 + "1",
        "",
    ],
)
def test_parse_file_start_rejects_anything_else(text):
    assert parse_file_start(text) is None


def test_parse_file_start_tolerates_case_of_the_command_and_spaces():
    assert parse_file_start("  /START   file_" + "A" * 12 + "0  ") == ("A" * 12, 0)


@pytest.mark.parametrize(
    "name, ext, expected",
    [
        ("worksheet", ".pdf", "worksheet.pdf"),
        ("worksheet.pdf", ".pdf", "worksheet.pdf"),
        ("Worksheet.PDF", ".pdf", "Worksheet.PDF"),
        ("דף עבודה", ".docx", "דף עבודה.docx"),
        ("../../etc/passwd", ".txt", "passwd.txt"),
        ("a\\b/c:d*e?f.pdf", ".pdf", "c_d_e_f.pdf"),
        ("..hidden", ".pdf", "hidden.pdf"),
        ("  ", ".pdf", "file.pdf"),
        ("bad\x00name\n", ".png", "badname.png"),
    ],
)
def test_safe_filename(name, ext, expected):
    assert safe_filename(name, ext) == expected


def test_safe_filename_is_bounded():
    out = safe_filename("x" * 500, ".pdf")
    assert out.endswith(".pdf") and len(out) <= 100


@pytest.mark.parametrize("ext", [".pdf", ".PDF", "pdf", ".docx", ".jpeg", ".png", ".txt", ".xlsx"])
def test_allowed_extensions(ext):
    assert extension_allowed(ext)


@pytest.mark.parametrize(
    "ext", [".exe", ".apk", ".js", ".html", ".zip", ".sh", "", ".pdf.exe", ".svg"]
)
def test_refused_extensions(ext):
    assert not extension_allowed(ext)


@pytest.mark.parametrize(
    "ext, head",
    [
        (".pdf", b"%PDF-1.7\n"),
        (".docx", b"PK\x03\x04rest"),
        (".xlsx", b"PK\x03\x04rest"),
        (".pptx", b"PK\x03\x04rest"),
        (".doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
        (".png", b"\x89PNG\r\n\x1a\nrest"),
        (".jpg", b"\xff\xd8\xff\xe0rest"),
        (".jpeg", b"\xff\xd8\xff\xe1rest"),
        (".txt", "שלום".encode()),
    ],
)
def test_content_matches_the_extension(ext, head):
    assert content_matches(ext, head)


@pytest.mark.parametrize(
    "ext, head",
    [
        (".pdf", b"MZ\x90\x00"),  # an executable named .pdf
        (".pdf", b"<html>"),
        (".docx", b"%PDF-1.4"),
        (".png", b"\xff\xd8\xff"),
        (".txt", b"abc\x00def"),
        (".pdf", b""),
    ],
)
def test_content_that_does_not_match_is_refused(ext, head):
    assert not content_matches(ext, head)


def test_mime_types():
    assert mime_for(".pdf") == "application/pdf"
    assert mime_for(".png") == "image/png"
    assert mime_for(".weird") == "application/octet-stream"


def test_the_cap_is_20_mb():
    assert MAX_FILE_BYTES == 20 * 1024 * 1024


def test_only_the_exact_getfile_url_is_a_download_url():
    assert is_download_url(URL)
    for bad in (
        URL.replace("https://", "http://"),
        URL.replace("webtopserver.smartschool.co.il", "evil.example"),
        URL.replace(
            "webtopserver.smartschool.co.il", "webtopserver.smartschool.co.il.evil.example"
        ),
        URL.replace("/stream/GetFile", "/stream/Other"),
        URL.replace("/server/api/stream/GetFile", "/server/api/stream/GetFile/../GetFile"),
        URL + "&x=1",
        URL.replace("token=", "t="),
        "https://webtopserver.smartschool.co.il/server/api/stream/GetFile?token=",
        "https://webtopserver.smartschool.co.il/server/api/stream/GetFile",
        "https://user@webtopserver.smartschool.co.il/server/api/stream/GetFile?token=abc",
        "https://webtopserver.smartschool.co.il:8443/server/api/stream/GetFile?token=abc",
        URL + "#frag",
        "",
        "not a url",
    ):
        assert not is_download_url(bad), bad


def test_markers_are_found_in_order_without_repeats():
    from easytop.files import refs_in

    assert refs_in("a {{m2}} b {{ m10 }} c {{m2}}") == ["m2", "m10"]
    assert refs_in("no markers {m2} {{x}} {{m}}") == []


def test_markers_are_stripped_with_the_space_before_them():
    from easytop.files import strip_markers

    assert strip_markers("source (Ms. Dana, 4.10) {{m2}}") == "source (Ms. Dana, 4.10)"
    assert strip_markers("a {{m2}}\nnext {{m3}} end") == "a\nnext end"
    assert strip_markers("{{m1}}start") == "start"
    assert strip_markers("keep {{x}} this") == "keep {{x}} this"
