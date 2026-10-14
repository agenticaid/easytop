"""Inbox attachments (Spec 14): pure helpers for the start payload of a file link, file names, the allowed
types and the one download URL the connector may fetch. No I/O here."""

from __future__ import annotations

import base64
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

MAX_FILE_BYTES = 20 * 1024 * 1024  # Telegram's bot limit is 50 MB; school documents are far smaller
MAX_NAME_CHARS = 100
PAYLOAD_PREFIX = "file_"
_HASH_CHARS = 12

DOWNLOAD_HOST = "webtopserver.smartschool.co.il"
DOWNLOAD_PATH = "/server/api/stream/GetFile"

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # the old Office formats (.doc .xls .ppt)
_ZIP = b"PK\x03\x04"  # the new Office formats (.docx .xlsx .pptx)
_SIGNATURES: dict[str, bytes] = {
    ".pdf": b"%PDF",
    ".docx": _ZIP,
    ".xlsx": _ZIP,
    ".pptx": _ZIP,
    ".doc": _OLE,
    ".xls": _OLE,
    ".ppt": _OLE,
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
}
ALLOWED_EXTENSIONS = frozenset({*_SIGNATURES, ".txt"})
_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
}
_UNSAFE_IN_NAME = re.compile(r'[:*?"<>|]')
_PAYLOAD_HASH = re.compile(rf"^[A-Za-z0-9_-]{{{_HASH_CHARS}}}$")


def message_hash(message_id: str) -> str:
    """12 url-safe characters standing for a Webtop message id (which is about 100 characters long, too long
    for a Telegram start parameter). Found again by hashing the ids of the parent's own inbox."""
    digest = hashlib.sha256(message_id.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()[:_HASH_CHARS]


def file_payload(message_id: str, index: int) -> str:
    """The start parameter of a file link: `file_<hash><index>` (index 0-9, 18 characters in all)."""
    if not 0 <= index <= 9:
        raise ValueError("a message has at most 10 files")
    return f"{PAYLOAD_PREFIX}{message_hash(message_id)}{index}"


def parse_file_start(text: str) -> tuple[str, int] | None:
    """`/start file_<hash><index>` -> (hash, index); anything else -> None."""
    parts = text.strip().split()
    if len(parts) != 2 or parts[0].lower() != "/start" or not parts[1].startswith(PAYLOAD_PREFIX):
        return None
    rest = parts[1].removeprefix(PAYLOAD_PREFIX)
    if len(rest) != _HASH_CHARS + 1 or not rest[-1].isdigit() or not rest[-1].isascii():
        return None
    digest = rest[:_HASH_CHARS]
    return (digest, int(rest[-1])) if _PAYLOAD_HASH.match(digest) else None


def _normal_ext(ext: str) -> str:
    ext = ext.strip().lower()
    return ext if ext.startswith(".") or not ext else f".{ext}"


def extension_allowed(ext: str) -> bool:
    return _normal_ext(ext) in ALLOWED_EXTENSIONS


def mime_for(ext: str) -> str:
    return _MIME.get(_normal_ext(ext), "application/octet-stream")


def content_matches(ext: str, head: bytes) -> bool:
    """True if the first bytes of the file are what its extension says (a renamed executable is refused)."""
    ext = _normal_ext(ext)
    if not head:
        return False
    if ext == ".txt":
        return b"\x00" not in head
    signature = _SIGNATURES.get(ext)
    return signature is not None and head.startswith(signature)


def safe_filename(name: str, ext: str) -> str:
    """A file name safe to hand to a chat app: no path parts, control characters or leading dots, the extension
    from the listing added when the name lacks it, Hebrew kept, at most 100 characters."""
    ext = _normal_ext(ext)
    cleaned = "".join(c for c in name if unicodedata.category(c) != "Cc")
    cleaned = re.split(r"[\\/]", cleaned)[-1]
    cleaned = _UNSAFE_IN_NAME.sub("_", cleaned).strip().lstrip(". ")
    if not cleaned:
        cleaned = "file"
    if ext and not cleaned.lower().endswith(ext):
        cleaned += ext
    if len(cleaned) > MAX_NAME_CHARS:
        stem = cleaned[: -len(ext)] if ext and cleaned.lower().endswith(ext) else cleaned
        cleaned = stem[: MAX_NAME_CHARS - len(ext)] + ext
    return cleaned


def is_download_url(url: str) -> bool:
    """The only URL the connector may fetch: Webtop's own file endpoint with a single `token` value, over https,
    with no user, port, fragment or extra parameter. A URL always comes out of a message the parent's own
    session just opened, but is checked anyway."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    query = parts.query
    return (
        parts.scheme == "https"
        and parts.hostname == DOWNLOAD_HOST
        and port is None
        and parts.username is None
        and parts.password is None
        and parts.path == DOWNLOAD_PATH
        and not parts.fragment
        and query.startswith("token=")
        and len(query) > len("token=")
        and "&" not in query
        and not any(c.isspace() or ord(c) < 32 for c in query)
    )


# The model cites the ref of a message that has files right after the item's source tag, for example
# "(message from Ms. Dana, 4.10) {{m2}}". Code removes the marker and puts the links at the end of the chat message.
_MARKER = re.compile(r"\{\{\s*(m\d+)\s*\}\}")
_MARKER_WITH_SPACE = re.compile(r"[ \t]*\{\{\s*m\d+\s*\}\}")


def refs_in(text: str) -> list[str]:
    """The refs cited in `text`, in order of first appearance."""
    return list(dict.fromkeys(_MARKER.findall(text)))


def strip_markers(text: str) -> str:
    """`text` without its markers (and the spaces right before them)."""
    return _MARKER_WITH_SPACE.sub("", text)
