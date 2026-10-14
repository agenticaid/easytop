from types import SimpleNamespace as NS

from easytop.adapters.model import Button, FileRef, KidSection, Reply
from easytop.adapters.telegram import (
    MAX_LEN,
    TelegramAdapter,
    chat_id_of,
    incoming_from_update,
    render,
)


def test_render_sections_per_kid_with_escaping_and_buttons():
    reply = Reply(
        text="Tomorrow, Tuesday 2026-10-06",
        sections=[
            KidSection("Noa", "ג׳2", ["Math <p.42>", "⚠️ Art cancelled"]),
            KidSection("Ido", "ו׳1", ["Trip form & water"]),
        ],
        buttons=[Button("tomorrow", "📅 Tomorrow"), Button("messages", "📩 Messages")],
        footer="Source: Webtop, checked 18:42",
    )
    (m,) = render(reply)
    assert "<b>Noa</b> (ג׳2)" in m.text and "🧒" not in m.text  # no emoji next to a child's name
    assert "• Math &lt;p.42&gt;" in m.text and "Trip form &amp; water" in m.text
    assert "<i>Source: Webtop, checked 18:42</i>" in m.text
    assert m.buttons == [[("📅 Tomorrow", "tomorrow"), ("📩 Messages", "messages")]]
    assert m.text.index("Noa") < m.text.index("Ido")


def test_agent_free_text_is_escaped():
    (m,) = render(Reply(text="<b>hi</b> & <script>"))
    assert "<b>" not in m.text and "&lt;script&gt;" in m.text


def test_long_reply_split_under_limit_buttons_only_on_last():
    sections = [KidSection(f"Kid{i}", "x", ["line " * 200]) for i in range(10)]
    msgs = render(Reply(sections=sections, buttons=[Button("a", "A")]))
    assert len(msgs) > 1
    assert all(len(m.text) <= MAX_LEN for m in msgs)
    assert [bool(m.buttons) for m in msgs] == [False] * (len(msgs) - 1) + [True]


def test_single_oversized_block_hard_split():
    msgs = render(Reply(text="word " * 3000))
    assert len(msgs) >= 4 and all(len(m.text) <= MAX_LEN for m in msgs)


def test_files_rendered_as_links():
    (m,) = render(Reply(text="x", files=[FileRef("trip.pdf", "https://x.test/a?b=1&c=2")]))
    assert '<a href="https://x.test/a?b=1&amp;c=2">trip.pdf</a>' in m.text


def test_incoming_from_update():
    msg = NS(message=NS(text="hello", from_user=NS(id=42)), callback_query=None)
    inc = incoming_from_update(msg)
    assert (inc.parent_id, inc.text, inc.button) == ("telegram:42", "hello", None)
    cb = NS(message=None, callback_query=NS(data="tomorrow", from_user=NS(id=42)))
    inc = incoming_from_update(cb)
    assert (inc.parent_id, inc.text, inc.button) == ("telegram:42", None, "tomorrow")
    assert (
        incoming_from_update(NS(message=NS(text=None, from_user=NS(id=1)), callback_query=None))
        is None
    )
    assert chat_id_of("telegram:42") == 42


async def test_adapter_on_update_sends_rendered_reply():
    sent = []

    class Bot:
        async def send_chat_action(self, chat_id, action): ...

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            sent.append((chat_id, text, parse_mode, reply_markup))

    class GW:
        async def handle(self, incoming):
            return Reply(text=f"echo {incoming.text}", buttons=[Button("tomorrow", "T")])

    update = NS(message=NS(text="hi", from_user=NS(id=7)), callback_query=None)
    await TelegramAdapter(GW()).on_update(update, NS(bot=Bot()))
    chat_id, text, mode, markup = sent[0]
    assert (chat_id, text, mode) == (7, "echo hi", "HTML")
    assert markup.inline_keyboard[0][0].callback_data == "tomorrow"


async def test_callback_query_is_acknowledged():
    answered = []

    async def answer():
        answered.append(1)

    class Bot:
        async def send_chat_action(self, *a, **k): ...

        async def send_message(self, *a, **k): ...

    class GW:
        async def handle(self, incoming):
            return Reply(text="ok")

    update = NS(message=None, callback_query=NS(data="tomorrow", from_user=NS(id=7), answer=answer))
    await TelegramAdapter(GW()).on_update(update, NS(bot=Bot()))
    assert answered == [1]


def test_markdown_bold_from_the_model_becomes_html_bold_and_stays_escaped():
    (m,) = render(Reply(text="**🎒 Equipment** for <Noa> & Ido"))
    assert m.text == "<b>🎒 Equipment</b> for &lt;Noa&gt; &amp; Ido"


def test_unbalanced_stars_are_left_alone():
    (m,) = render(Reply(text="2 ** 3 and **open"))
    assert "<b>" not in m.text


async def test_typing_indicator_is_sent_before_the_slow_answer():
    order = []

    class Bot:
        async def send_chat_action(self, chat_id, action):
            order.append(("action", chat_id, action))

        async def send_message(self, chat_id, text, **kw):
            order.append(("message", chat_id))

    class GW:
        async def handle(self, incoming):
            order.append(("handle",))
            return Reply(text="ok")

    update = NS(message=NS(text="hi", from_user=NS(id=7)), callback_query=None)
    await TelegramAdapter(GW()).on_update(update, NS(bot=Bot()))
    assert order == [("action", 7, "typing"), ("handle",), ("message", 7)]


def test_bold_and_underlined_child_name_from_the_model_renders_as_html():
    (m,) = render(Reply(text="**__Noa__** (ג׳2)\n🎒 ציוד"))
    assert m.text.startswith("<b><u>Noa</u></b> (ג׳2)")


def test_underscores_inside_words_or_unbalanced_are_left_alone():
    (m,) = render(Reply(text="file_name_x and a__b and __open"))
    assert "<u>" not in m.text


def test_a_whole_bold_header_with_an_underlined_name_renders_with_valid_nesting():
    (m,) = render(Reply(text="**__Noa__ (ג׳2) – Tue 6.10**"))
    assert m.text == "<b><u>Noa</u> (ג׳2) – Tue 6.10</b>"
    (m,) = render(Reply(text="__**Noa**__ (ג׳2)"))
    assert m.text == "<u><b>Noa</b></u> (ג׳2)"


def test_a_link_button_renders_on_the_last_chunk_only():
    from easytop.adapters.model import LinkButton

    link = LinkButton("חיבור לוובטופ", "https://x.example/?t=abc")
    sections = [KidSection(f"K{i}", "א1", ["x" * 300] * 5) for i in range(8)]
    msgs = render(Reply(sections=sections, link=link))
    assert len(msgs) > 1
    assert [m.link for m in msgs] == [None] * (len(msgs) - 1) + [link]


async def test_a_link_button_is_sent_as_a_telegram_mini_app_button():
    from easytop.adapters.model import LinkButton

    sent = []

    class Bot:
        async def send_chat_action(self, *a, **k): ...

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            sent.append(reply_markup)

    class GW:
        async def handle(self, incoming):
            return Reply(text="connect", link=LinkButton("Connect", "https://x.example/?t=abc"))

    update = NS(message=NS(text="hi", from_user=NS(id=7)), callback_query=None)
    await TelegramAdapter(GW()).on_update(update, NS(bot=Bot()))
    button = sent[0].inline_keyboard[0][0]
    assert button.text == "Connect" and button.web_app.url == "https://x.example/?t=abc"
