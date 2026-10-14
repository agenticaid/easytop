import json
import logging
from datetime import date

import pytest

from easytop import bot
from easytop.adapters.security import Allowlist

TOKEN = "123456:SECRET-TOKEN-VALUE"


def session_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps(
            {"cookies": [{"name": "webToken", "value": "t", "domain": ".smartschool.co.il"}]}
        )
    )
    return p


def test_a_live_session_belongs_to_exactly_one_allowed_telegram_id(tmp_path):
    two = Allowlist({"telegram:1", "telegram:2"})
    with pytest.raises(bot.BotConfigError, match="exactly one"):
        bot.build_gateway(two, session=session_file(tmp_path), anthropic_client=object())
    with pytest.raises(bot.BotConfigError, match="exactly one"):
        bot.build_gateway(Allowlist(), session=session_file(tmp_path), anthropic_client=object())
    gw = bot.build_gateway(
        Allowlist({"telegram:1"}), session=session_file(tmp_path), anthropic_client=object()
    )
    assert gw is not None


def test_the_live_connector_asks_for_today_every_time_not_the_start_up_day(tmp_path):
    c = bot.build_connector(session_file(tmp_path))
    assert c._clock() == date.today()


def test_logging_never_prints_request_urls_that_hold_the_bot_token():
    logging.getLogger().setLevel(
        logging.INFO
    )  # as in a real run (pytest leaves the root at WARNING)
    bot.configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("telegram").getEffectiveLevel() >= logging.WARNING
    for name in (
        "httpx2",
        "anthropic",
        "httpcore",
    ):  # seen in the first live run: Anthropic calls log as httpx2
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_main_names_the_missing_settings_and_never_values(monkeypatch, capsys):
    for var in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_ALLOWED_IDS"):
        monkeypatch.delenv(var, raising=False)
    assert bot.main([]) == 2
    out = capsys.readouterr().out
    assert (
        "TELEGRAM_BOT_TOKEN" in out and "ANTHROPIC_API_KEY" in out and "TELEGRAM_ALLOWED_IDS" in out
    )


def test_main_refuses_a_bad_session_file_without_echoing_content(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "1")
    bad = tmp_path / "bad.json"
    bad.write_text("SECRETVALUE")
    assert bot.main(["--session", str(bad)]) == 2
    out = capsys.readouterr().out
    assert "SECRETVALUE" not in out and TOKEN not in out


def test_logging_can_write_to_a_file_so_a_death_leaves_a_record(tmp_path):
    log_file = tmp_path / "logs" / "bot.log"
    root = logging.getLogger()
    before = list(root.handlers)
    root.setLevel(logging.INFO)
    try:
        bot.configure_logging(log_file)
        logging.getLogger("easytop.test").info("bot started")
        for h in root.handlers:
            h.flush()
        assert "bot started" in log_file.read_text()
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


async def test_polling_errors_are_logged_by_type_only_never_content(caplog):
    from easytop.adapters.telegram import TelegramAdapter

    class Ctx:
        error = ConnectionError("https://api.telegram.org/bot123456:SECRET-TOKEN-VALUE/getUpdates")

    adapter = TelegramAdapter(gateway=object())
    with caplog.at_level(logging.WARNING):
        await adapter.on_error(None, Ctx())
    assert "ConnectionError" in caplog.text
    assert "SECRET-TOKEN-VALUE" not in caplog.text


def test_main_logs_a_fatal_exit_to_the_log_file_by_type_only(monkeypatch, tmp_path):
    from easytop.adapters.telegram import TelegramAdapter

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "1")
    monkeypatch.setenv("EASYTOP_LOG_FILE", str(tmp_path / "bot.log"))

    def boom(self, token, **kwargs):
        raise RuntimeError(f"url with {token}")

    monkeypatch.setattr(TelegramAdapter, "run_polling", boom)
    root = logging.getLogger()
    before = list(root.handlers)
    root.setLevel(logging.INFO)
    try:
        with pytest.raises(RuntimeError):
            bot.main([])
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "bot.log").read_text()
        assert "RuntimeError" in text and "SECRET-TOKEN-VALUE" not in text
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


async def test_multi_gateway_gives_an_unconnected_parent_the_connect_button():
    from easytop.adapters.model import Incoming
    from easytop.onboarding.crypto import SessionCipher, new_key
    from easytop.store.memory import InMemoryStore

    gateway, flow = bot.build_multi_gateway(
        Allowlist({"telegram:1", "telegram:2"}),
        store=InMemoryStore(),
        cipher=SessionCipher(new_key()),
        anthropic_client=object(),
        connect_base_url=lambda: "https://login.example",
    )
    for pid in ("telegram:1", "telegram:2"):  # several parents are fine in multi mode
        r = await gateway.handle(Incoming(pid, text="hi"))
        assert r.link is not None and r.link.url.startswith("https://login.example/?t=")
    assert (await gateway.handle(Incoming("telegram:9", text="hi"))).link is None  # stranger


async def test_multi_gateway_without_a_login_page_says_it_is_unavailable():
    from easytop.adapters.model import Incoming
    from easytop.onboarding.crypto import SessionCipher, new_key
    from easytop.store.memory import InMemoryStore

    gateway, _ = bot.build_multi_gateway(
        Allowlist({"telegram:1"}),
        store=InMemoryStore(),
        cipher=SessionCipher(new_key()),
        anthropic_client=object(),
        connect_base_url=lambda: None,
    )
    r = await gateway.handle(Incoming("telegram:1", text="hi"))
    assert r.link is None and "not available" in r.text


def test_multi_mode_names_its_missing_settings_and_never_values(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "1,2")
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SESSION_ENCRYPTION_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert bot.main(["--multi"]) == 2
    out = capsys.readouterr().out
    assert "SESSION_ENCRYPTION_KEY" in out and "SUPABASE" in out and TOKEN not in out


def test_multi_with_supabase_accepts_the_dev_names_of_the_local_env_file(monkeypatch, capsys):
    from easytop.adapters.telegram import TelegramAdapter
    from easytop.onboarding.crypto import new_key

    _env(
        monkeypatch,
        SUPABASE_URL_DEV="https://dev.supabase.co",
        SUPABASE_SERVICE_KEY_DEV="dev-key",
        SESSION_ENCRYPTION_KEY=new_key().decode(),
    )
    monkeypatch.setattr(TelegramAdapter, "run_polling", lambda self, token, **kw: None)
    assert bot.main(["--multi", "--store", "supabase"]) == 0


def test_session_and_multi_cannot_be_combined(capsys):
    with pytest.raises(SystemExit):
        bot.main(["--multi", "--session", "x.json"])


async def test_the_telegram_adapter_can_push_a_reply_to_a_parent():
    from types import SimpleNamespace as NS

    from easytop.adapters.model import Reply
    from easytop.adapters.telegram import TelegramAdapter

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    adapter = TelegramAdapter(gateway=object())
    await adapter.notify("telegram:7", Reply(text="hello"))  # no bot yet: must not crash
    await adapter._remember_bot(NS(bot=Bot()))
    await adapter.notify("telegram:7", Reply(text="hello"))
    assert sent == [(7, "hello")]


def _env(monkeypatch, **extra):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_IDS", "1,2")
    for var in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "SUPABASE_URL_DEV",
        "SUPABASE_SERVICE_KEY_DEV",
        "SESSION_ENCRYPTION_KEY",
        "EASYTOP_CONNECT_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_multi_with_the_memory_store_needs_no_supabase_settings(monkeypatch, capsys):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    captured = {}

    def fake_run(self, token, *, on_start=None, on_stop=None):
        captured.update(on_start=on_start, on_stop=on_stop)

    monkeypatch.setattr(TelegramAdapter, "run_polling", fake_run)
    assert bot.main(["--multi", "--store", "memory"]) == 0
    out = capsys.readouterr().out
    assert "lost on restart" in out
    assert (
        captured["on_start"] is not None
    )  # no login server, but the nudge timer starts with the bot


def test_serve_login_starts_the_login_server_with_the_bot_and_stops_it_after(monkeypatch):
    from easytop.adapters.telegram import TelegramAdapter
    from easytop.login import runtime as runtime_module

    _env(monkeypatch)
    events = []

    class FakeRuntime:
        base_url = "https://fake.example"

        def __init__(self, links, on_connected, **kw):
            events.append(("built", kw["viewer_check"], kw["tunnel"] is not None))

        async def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(runtime_module, "LoginRuntime", FakeRuntime)
    monkeypatch.setattr(bot, "LoginRuntime", FakeRuntime)

    def fake_run(self, token, *, on_start=None, on_stop=None):
        import asyncio

        asyncio.run(on_start())
        asyncio.run(on_stop())

    monkeypatch.setattr(TelegramAdapter, "run_polling", fake_run)
    assert bot.main(["--multi", "--store", "memory", "--serve-login"]) == 0
    assert events[0][0] == "built" and events[0][2] is True  # a cloudflared tunnel by default
    assert events[1:] == ["start", "stop"]


def test_serve_login_with_a_fixed_public_address_uses_no_tunnel(monkeypatch):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch, EASYTOP_CONNECT_URL="https://login.example")
    built = {}

    class FakeRuntime:
        base_url = "https://login.example"

        def __init__(self, links, on_connected, **kw):
            built.update(kw)

        async def start(self): ...

        async def stop(self): ...

    monkeypatch.setattr(bot, "LoginRuntime", FakeRuntime)
    monkeypatch.setattr(TelegramAdapter, "run_polling", lambda self, token, **kw: None)
    assert bot.main(["--multi", "--store", "memory", "--serve-login"]) == 0
    assert built["tunnel"] is None and built["public_url"] == "https://login.example"


def test_the_viewer_check_accepts_only_the_parent_the_link_was_made_for():
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    init = urlencode(
        {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}
    )
    ok = bot.make_viewer_check(TOKEN)
    assert ok("telegram:42", init) is True
    assert ok("telegram:43", init) is False  # a forwarded link opened by someone else
    assert ok("telegram:42", "") is False


def test_serve_login_needs_multi_mode(capsys):
    with pytest.raises(SystemExit):
        bot.main(["--serve-login"])


def _run_with_fake_runtime(monkeypatch, argv):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    built = {}

    class FakeRuntime:
        base_url = "https://fake.example"

        def __init__(self, links, on_connected, **kw):
            built.update(kw)

        async def start(self): ...

        async def stop(self): ...

    monkeypatch.setattr(bot, "LoginRuntime", FakeRuntime)
    monkeypatch.setattr(TelegramAdapter, "run_polling", lambda self, token, **kw: None)
    assert bot.main(argv) == 0
    return built


def test_long_sessions_are_the_default_so_a_parent_signs_in_only_once(monkeypatch, tmp_path):
    base = ["--multi", "--store", "file", "--store-file", str(tmp_path / "s.json"), "--serve-login"]
    assert _run_with_fake_runtime(monkeypatch, base)["remember"] is True
    # the old dev flag is still accepted (it is now the default) and there is an explicit way out
    assert _run_with_fake_runtime(monkeypatch, [*base, "--login-remember"])["remember"] is True
    assert _run_with_fake_runtime(monkeypatch, [*base, "--login-no-remember"])["remember"] is False


def test_the_file_store_keeps_parents_across_restarts_with_its_own_dev_key(
    monkeypatch, tmp_path, capsys
):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    monkeypatch.setattr(TelegramAdapter, "run_polling", lambda self, token, **kw: None)
    store_file = tmp_path / "data" / "easytop.json"
    assert bot.main(["--multi", "--store", "file", "--store-file", str(store_file)]) == 0
    key_file = store_file.with_suffix(".key")
    assert key_file.exists() and oct(key_file.stat().st_mode & 0o777) == "0o600"
    first_key = key_file.read_text()
    assert bot.main(["--multi", "--store", "file", "--store-file", str(store_file)]) == 0
    assert (
        key_file.read_text() == first_key
    )  # the same key next time, so stored sessions can be read again
    assert "dev key" in capsys.readouterr().out


def test_the_login_server_listens_on_localhost_and_8765_unless_told_otherwise(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("PORT", raising=False)
    base = ["--multi", "--store", "file", "--store-file", str(tmp_path / "s.json"), "--serve-login"]
    built = _run_with_fake_runtime(monkeypatch, base)
    assert built["host"] == "127.0.0.1" and built["port"] == 8765


def test_in_a_container_the_login_server_takes_its_port_from_the_environment_and_host_from_a_flag(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("PORT", "9000")
    base = ["--multi", "--store", "file", "--store-file", str(tmp_path / "s.json"), "--serve-login"]
    built = _run_with_fake_runtime(monkeypatch, [*base, "--login-host", "0.0.0.0"])
    assert built["host"] == "0.0.0.0" and built["port"] == 9000


async def test_the_lifecycle_starts_the_login_page_and_the_nudge_timer_and_stops_both():
    import asyncio

    events: list[str] = []

    class Runtime:
        async def start(self):
            events.append("login-start")

        async def stop(self):
            events.append("login-stop")

    class Scheduler:
        async def run(self):
            events.append("timer-run")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("timer-stopped")

    on_start, on_stop = bot.make_lifecycle(Runtime(), Scheduler())
    await on_start()
    await asyncio.sleep(0)
    assert events == ["login-start", "timer-run"]
    await on_stop()
    assert events[-2:] == ["timer-stopped", "login-stop"] or events[-2:] == [
        "login-stop",
        "timer-stopped",
    ]
    assert set(events) == {"login-start", "timer-run", "timer-stopped", "login-stop"}


def test_the_lifecycle_is_empty_when_there_is_nothing_to_run():
    assert bot.make_lifecycle(None, None) == (None, None)


def test_multi_mode_runs_the_nudge_timer_even_without_the_login_page(monkeypatch, tmp_path):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    seen = {}

    class FakeScheduler:
        def __init__(self, store, registry, notify, **kw):
            seen["built"] = True
            seen["expired_reply"] = kw["expired_reply"]

        async def run(self): ...

    def fake_polling(self, token, **kw):
        seen["on_start"] = kw.get("on_start")

    monkeypatch.setattr(bot, "NudgeScheduler", FakeScheduler)
    monkeypatch.setattr(TelegramAdapter, "run_polling", fake_polling)
    argv = ["--multi", "--store", "file", "--store-file", str(tmp_path / "s.json")]
    assert bot.main(argv) == 0
    assert seen["built"] and seen["on_start"] is not None


def test_single_session_mode_has_no_nudge_timer(monkeypatch, tmp_path):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    seen = {}
    monkeypatch.setattr(bot, "NudgeScheduler", lambda *a, **k: seen.setdefault("built", True))
    monkeypatch.setattr(TelegramAdapter, "run_polling", lambda self, token, **kw: seen.update(kw))
    assert bot.main([]) == 0  # the synthetic demo family
    assert "built" not in seen and seen["on_start"] is None


async def test_the_adapter_builds_invite_links_from_the_bots_username():
    from types import SimpleNamespace as NS

    from easytop.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter(gateway=None)
    assert adapter.invite_link("inv_x") is None  # username not known before start-up
    await adapter._remember_bot(NS(bot=NS(username="my_bot")))
    assert adapter.invite_link("inv_x") == "https://t.me/my_bot?start=inv_x"


async def test_the_lifecycle_loads_the_stored_allowlist_before_anything_else():
    events: list[str] = []

    async def preload():
        events.append("allowlist-loaded")

    class Scheduler:
        async def run(self):
            events.append("timer-run")

    on_start, _ = bot.make_lifecycle(None, Scheduler(), preload=preload)
    await on_start()
    assert events[0] == "allowlist-loaded"


def test_multi_mode_gives_the_gateway_an_invite_service_and_loads_it_at_start(
    monkeypatch, tmp_path
):
    from easytop.adapters.telegram import TelegramAdapter

    _env(monkeypatch)
    seen = {}

    def fake_polling(self, token, **kw):
        seen["on_start"] = kw["on_start"]

    monkeypatch.setattr(TelegramAdapter, "run_polling", fake_polling)
    monkeypatch.setattr(bot, "NudgeScheduler", lambda *a, **k: None)
    built = {}
    real = bot.build_multi_gateway

    def spy(*a, **kw):
        gateway, flow = real(*a, **kw)
        built["invites"] = gateway.invites
        return gateway, flow

    monkeypatch.setattr(bot, "build_multi_gateway", spy)
    assert bot.main(["--multi", "--store", "file", "--store-file", str(tmp_path / "s.json")]) == 0
    assert built["invites"] is not None and seen["on_start"] is not None


async def test_the_lifecycle_also_runs_the_session_health_check_and_stops_it():
    import asyncio

    events: list[str] = []

    class Health:
        async def run(self):
            events.append("health-run")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("health-stopped")

    on_start, on_stop = bot.make_lifecycle(None, None, health=Health())
    await on_start()
    await asyncio.sleep(0)
    assert events == ["health-run"]
    await on_stop()
    assert events == ["health-run", "health-stopped"]
