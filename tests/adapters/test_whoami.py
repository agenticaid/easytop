import httpx

from easytop.adapters import whoami

TOKEN = "123456:SECRET-TOKEN-VALUE"


def update(uid, text="hi", first="Erez"):
    return {
        "update_id": uid,
        "message": {"from": {"id": uid * 10, "is_bot": False, "first_name": first}, "text": text},
    }


def test_ids_come_from_message_senders_deduplicated_without_message_text():
    found = whoami.ids_from_updates([update(1), update(1, "again"), update(2, first="Dana")])
    assert found == [{"id": 10, "first_name": "Erez"}, {"id": 20, "first_name": "Dana"}]
    assert "again" not in str(found)


def test_updates_without_a_human_sender_are_skipped():
    updates = [
        {"update_id": 1, "edited_channel_post": {"text": "x"}},
        {"update_id": 2, "message": {"from": {"id": 5, "is_bot": True, "first_name": "B"}}},
    ]
    assert whoami.ids_from_updates(updates) == []


def test_main_prints_the_ids_and_never_the_token(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": [update(1)]})

    code = whoami.main(client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = capsys.readouterr().out
    assert code == 0 and "10" in out and "Erez" in out
    assert TOKEN not in out and f"bot{TOKEN}/getUpdates" in seen["url"]


def test_main_without_token_or_messages_says_what_to_do(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert whoami.main() == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True, "result": []})
        )
    )
    assert whoami.main(client=client) == 1
    assert "Start" in capsys.readouterr().out


def test_main_http_error_does_not_leak_the_token(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)

    def boom(request):
        raise httpx.ConnectError("failed for " + str(request.url))

    assert whoami.main(client=httpx.Client(transport=httpx.MockTransport(boom))) == 2
    assert TOKEN not in capsys.readouterr().out
