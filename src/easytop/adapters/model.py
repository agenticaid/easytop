"""Neutral, channel-agnostic message model. Only adapters know about Telegram / WhatsApp."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from easytop.models import FileOffer

MAX_BUTTONS = 3  # WhatsApp allows 3 quick replies (or a 10-item list); Telegram is fine with less


@dataclass(frozen=True)
class Incoming:
    parent_id: str  # channel-qualified identity, e.g. "telegram:12345"
    text: str | None = None
    button: str | None = None  # id of a pressed Button


@dataclass(frozen=True)
class Button:
    id: str
    label: str

    def __post_init__(self) -> None:
        if len(self.id.encode()) > 64:
            raise ValueError("button id too long (Telegram callback_data limit is 64 bytes)")


@dataclass(frozen=True)
class LinkButton:
    """A button that opens a web page INSIDE the chat app (a Telegram Mini App; a URL button on WhatsApp).
    Used for connecting or reconnecting Webtop. The parent never leaves the chat to find a page."""

    label: str
    url: str

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("link button needs a label")
        if not self.url.startswith("https://") or len(self.url) <= len("https://"):
            raise ValueError("link button needs an https URL")
        if len(self.url) > 2000:
            raise ValueError("link button URL too long")


@dataclass(frozen=True)
class KidSection:
    name: str
    class_name: str
    lines: list[str]


@dataclass(frozen=True)
class FileRef:
    name: str
    url: str


@dataclass(frozen=True)
class Document:
    """A file to send into the chat (an attachment the parent asked for by tapping its link). Bytes in memory only."""

    name: str
    data: bytes
    mime: str


@dataclass(frozen=True)
class Reply:
    text: str = ""
    sections: list[KidSection] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    link: LinkButton | None = None
    files: list[FileRef] = field(default_factory=list)  # links (name + URL) shown as text links
    documents: list[Document] = field(default_factory=list)  # files sent into the chat
    # files of the messages the text cites with {{ref}} markers; the adapter lists them as links at the end of
    # the chat message that cites them (Spec 14)
    offers: dict[str, tuple[FileOffer, ...]] = field(default_factory=dict)
    footer: str = ""

    def __post_init__(self) -> None:
        if len(self.buttons) > MAX_BUTTONS:
            raise ValueError(f"at most {MAX_BUTTONS} buttons per reply")


def reply_from_brief(brief: dict[str, Any], buttons: list[Button] | None = None) -> Reply:
    """Deterministic per-kid reply from a family-service brief (no LLM). Used for digests and buttons."""
    from easytop.family.render import kid_lines

    return Reply(
        text=f"Tomorrow, {brief['weekday']} {brief['date']}",
        sections=[KidSection(k["name"], k["class"], kid_lines(k)) for k in brief["kids"]],
        buttons=buttons or [],
        footer=f"Source: Webtop, checked {brief['checked_at'][11:16]}",
    )
