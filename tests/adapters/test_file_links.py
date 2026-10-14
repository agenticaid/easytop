"""File links at the end of a chat message, and sending a file to the chat (Spec 14)."""

from types import SimpleNamespace as NS

from easytop.adapters.model import Document, Reply
from easytop.adapters.telegram import MAX_LEN, TelegramAdapter, render
from easytop.models import FileOffer

PAYLOAD = "file_AAAAAAAAAAAA0"


def offer(name="zebra plan", ext=".pdf", kb=433, sender="Dana Levi", sent="3.10", payload=PAYLOAD):
    return FileOffer(name, ext, kb, sender, sent, payload)


def link(payload=PAYLOAD, name="zebra plan.pdf", bot="easytop_bot"):
    return f'<a href="https://t.me/{bot}?start={payload}">{name}</a>'


def test_a_cited_message_gets_its_files_as_links_at_the_end_and_the_marker_is_gone():
    reply = Reply(
        text="Do the sheet (message from Dana Levi, 3.10) {{m1}}", offers={"m1": (offer(),)}
    )
    (m,) = render(reply, bot_username="easytop_bot")
    assert "{{" not in m.text
    assert m.text == (
        f"Do the sheet (message from Dana Levi, 3.10)\n\n📎 {link()} · 433 KB · Dana Levi, 3.10"
    )


def test_the_links_come_after_the_footer_the_gateway_appended():
    reply = Reply(
        text="Do the sheet {{m1}}\n\nMessages checked: the last 7 days.", offers={"m1": (offer(),)}
    )
    (m,) = render(reply, bot_username="easytop_bot")
    assert m.text.index("Messages checked") < m.text.index("📎")
    assert m.text.endswith("Dana Levi, 3.10")


def test_every_file_of_every_cited_message_has_its_own_line_in_order_of_citation():
    reply = Reply(
        text="a {{m2}} b {{m1}}",
        offers={
            "m1": (offer("one", payload="file_AAAAAAAAAAAA0"),),
            "m2": (
                offer("two", payload="file_BBBBBBBBBBBB0", sender="Rina"),
                offer("three", ext=".docx", payload="file_BBBBBBBBBBBB1", sender="Rina"),
            ),
        },
    )
    (m,) = render(reply, bot_username="bot")
    lines = [ln for ln in m.text.splitlines() if ln.startswith("📎")]
    assert [ln.split(">")[1].split("<")[0] for ln in lines] == ["two.pdf", "three.docx", "one.pdf"]


def test_an_unknown_ref_is_removed_without_a_line():
    (m,) = render(Reply(text="x {{m9}} y", offers={"m1": (offer(),)}), bot_username="bot")
    assert m.text == "x y" and "📎" not in m.text


def test_text_without_markers_gets_no_file_lines_even_when_files_exist():
    (m,) = render(Reply(text="plain answer", offers={"m1": (offer(),)}), bot_username="bot")
    assert m.text == "plain answer"


def test_only_the_chat_message_that_cites_a_file_message_gets_the_links():
    filler = "word " * 1500  # about 7,500 characters: three chat messages
    reply = Reply(text=f"first part\n\n{filler}\n\nlast part {{{{m1}}}}", offers={"m1": (offer(),)})
    msgs = render(reply, bot_username="bot")
    assert len(msgs) >= 2
    assert ["📎" in m.text for m in msgs] == [False] * (len(msgs) - 1) + [True]
    assert all(len(m.text) <= MAX_LEN for m in msgs)


def test_links_that_do_not_fit_become_their_own_message_right_after():
    body = "x" * (MAX_LEN - 20)  # one full chat message
    msgs = render(Reply(text=f"{body} {{{{m1}}}}", offers={"m1": (offer(),)}), bot_username="bot")
    assert len(msgs) == 2 and "📎" not in msgs[0].text and msgs[1].text.startswith("📎")
    assert all(len(m.text) <= MAX_LEN for m in msgs)


def test_names_are_escaped():
    reply = Reply(text="s {{m1}}", offers={"m1": (offer("a<b>&c", sender="<i>x</i>"),)})
    (m,) = render(reply, bot_username="bot")
    assert "&lt;b&gt;&amp;c.pdf" in m.text and "<i>x</i>" not in m.text


def test_sizes_read_naturally():
    def line(kb):
        (m,) = render(Reply(text="s {{m1}}", offers={"m1": (offer(kb=kb),)}), bot_username="bot")
        return m.text.splitlines()[-1]

    assert "· 433 KB ·" in line(433)
    assert "· 1.8 MB ·" in line(1800)
    assert "KB" not in line(0) and "MB" not in line(0)


def test_without_a_bot_name_the_file_is_listed_but_not_linked():
    (m,) = render(Reply(text="s {{m1}}", offers={"m1": (offer(),)}))
    assert "📎 zebra plan.pdf · 433 KB · Dana Levi, 3.10" in m.text and "<a " not in m.text


def test_a_reply_with_only_a_document_renders_no_text_message():
    assert render(Reply(documents=[Document("a.pdf", b"%PDF", "application/pdf")])) == []


# ---- sending ----


class Bot:
    def __init__(self, fail_delete=False):
        self.sent, self.docs, self.deleted, self.fail_delete = [], [], [], fail_delete

    async def send_chat_action(self, chat_id, action): ...

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))

    async def send_document(self, chat_id, document=None, filename=None, **kw):
        self.docs.append((chat_id, document, filename))

    async def delete_message(self, chat_id, message_id):
        if self.fail_delete:
            raise RuntimeError("cannot delete")
        self.deleted.append((chat_id, message_id))


async def test_send_posts_the_document_with_its_file_name_and_no_empty_text():
    bot = Bot()
    await TelegramAdapter(NS()).send(
        bot, 7, Reply(documents=[Document("a.pdf", b"%PDF", "application/pdf")])
    )
    assert bot.docs == [(7, b"%PDF", "a.pdf")] and bot.sent == []


async def test_send_uses_the_bot_name_for_links():
    bot = Bot()
    adapter = TelegramAdapter(NS())
    adapter.username = "easytop_bot"
    await adapter.send(bot, 7, Reply(text="s {{m1}}", offers={"m1": (offer(),)}))
    assert "https://t.me/easytop_bot?start=" + PAYLOAD in bot.sent[0][1]


class Gateway:
    def __init__(self, reply):
        self.reply = reply

    async def handle(self, incoming):
        return self.reply


def update(text, message_id=55):
    return NS(message=NS(text=text, from_user=NS(id=7), message_id=message_id), callback_query=None)


async def test_the_parents_file_tap_message_is_deleted_after_the_file_is_sent():
    bot = Bot()
    reply = Reply(documents=[Document("a.pdf", b"%PDF", "application/pdf")])
    await TelegramAdapter(Gateway(reply)).on_update(update(f"/start {PAYLOAD}"), NS(bot=bot))
    assert bot.docs and bot.deleted == [(7, 55)]


async def test_a_failure_to_delete_the_tap_message_is_ignored():
    bot = Bot(fail_delete=True)
    reply = Reply(documents=[Document("a.pdf", b"%PDF", "application/pdf")])
    await TelegramAdapter(Gateway(reply)).on_update(update(f"/start {PAYLOAD}"), NS(bot=bot))
    assert bot.docs


async def test_an_ordinary_message_is_never_deleted():
    bot = Bot()
    await TelegramAdapter(Gateway(Reply(text="hello"))).on_update(update("what now?"), NS(bot=bot))
    assert bot.deleted == []


async def test_the_tap_message_is_kept_when_no_file_came_back():
    bot = Bot()
    await TelegramAdapter(Gateway(Reply(text="gone"))).on_update(
        update(f"/start {PAYLOAD}"), NS(bot=bot)
    )
    assert bot.deleted == []
