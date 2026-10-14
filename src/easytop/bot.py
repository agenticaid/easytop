"""Run the Telegram bot on this Mac (polling): `uv run --env-file .env python -m easytop.bot [--session FILE]`.

Needs TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY and TELEGRAM_ALLOWED_IDS in the environment. Without --session
it answers from the FAKE synthetic family. With --session FILE it reads the real Webtop account (read-only
calls) of exactly ONE allowed Telegram id: a saved Webtop session belongs to one parent, so allowing a
second id would show that person this family's data.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from easytop import analytics
from easytop.access.invites import InviteService
from easytop.adapters.gateway import WELCOME, Gateway
from easytop.adapters.security import Allowlist, DailyCap, RateLimiter
from easytop.adapters.telegram import TelegramAdapter, chat_id_of
from easytop.agent.core import Agent
from easytop.connector.base import Connector
from easytop.connector.fake import FakeConnector
from easytop.connector.session_file import load_cookies
from easytop.connector.webtop import WebtopConnector
from easytop.digest.nudge import NudgeScheduler
from easytop.errors import EasytopError
from easytop.family.service import FamilyService
from easytop.health import SessionHealth
from easytop.login.runtime import LoginRuntime
from easytop.login.telegram_initdata import verify_init_data
from easytop.login.tunnel import CloudflaredTunnel
from easytop.logjson import JsonFormatter
from easytop.onboarding.crypto import SessionCipher
from easytop.onboarding.flow import OnboardingFlow
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry
from easytop.onboarding.vault import SessionVault
from easytop.store.base import Store
from easytop.store.supabase import supabase_setting

REQUIRED_ENV = ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "TELEGRAM_ALLOWED_IDS")


class BotConfigError(EasytopError):
    pass


DAILY_CAP_VAR = "EASYTOP_DAILY_QUESTION_CAP"


def daily_cap_from_env() -> DailyCap | None:
    """Questions per parent per Israel-time day (Erez 2026-10-13). Unset, blank or 0: no cap (dev, the Mac bot).
    Anything that is not a whole number stops the bot rather than quietly running without the cap."""
    raw = os.environ.get(DAILY_CAP_VAR, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise BotConfigError(f"{DAILY_CAP_VAR} must be a whole number (0 = no cap), got {raw!r}")
    return DailyCap(int(raw)) if int(raw) > 0 else None


_installed_handlers: list[logging.Handler] = []


def json_logs_requested() -> bool:
    return os.environ.get("EASYTOP_LOG_FORMAT") == "json"


def configure_logging(log_file: Path | str | None = None, *, json_lines: bool = False) -> None:
    """Content-free INFO logs from our code. The HTTP libraries log request URLs, and a Telegram URL holds
    the bot token, so they stay at WARNING. With `log_file` the same lines also go to a rotating file, so a
    bot that dies unattended leaves a record. `json_lines` writes one JSON object per line (Cloud Logging)."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
        )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for old in _installed_handlers:  # calling it twice must not double every line
        root.removeHandler(old)
        old.close()
    _installed_handlers[:] = handlers
    formatter = (
        JsonFormatter() if json_lines else logging.Formatter("%(asctime)s %(name)s %(message)s")
    )
    for h in handlers:
        h.setFormatter(formatter)
        root.addHandler(h)
    for noisy in ("httpx", "httpx2", "httpcore", "telegram", "hpack", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def configure_analytics() -> analytics.Emitter:
    """Analytics events carry the environment and a pseudonym keyed by the session key (stable across restarts,
    different per environment). Without that key the pseudonyms are random for this process."""
    return analytics.configure(
        env=os.environ.get("EASYTOP_ENV", "local"), key=os.environ.get("SESSION_ENCRYPTION_KEY")
    )


def build_connector(session: Path | str) -> WebtopConnector:
    """The real connector; `today` is asked every time because the bot runs for days."""
    return WebtopConnector(load_cookies(session), clock=date.today)


def build_gateway(
    allowlist: Allowlist,
    *,
    session: Path | str | None,
    anthropic_client,
    limiter: RateLimiter | None = None,
) -> Gateway:
    connector: Connector
    if session is not None:
        if len(allowlist.ids) != 1:
            raise BotConfigError(
                "a live Webtop session needs exactly one id in TELEGRAM_ALLOWED_IDS "
                f"(found {len(allowlist.ids)})"
            )
        connector = build_connector(session)
    else:
        connector = FakeConnector(date.today())
    agent = Agent(anthropic_client, FamilyService(connector, clock=datetime.now))
    return Gateway(allowlist, limiter or RateLimiter(), lambda parent_id: agent)


def build_multi_gateway(
    allowlist: Allowlist,
    *,
    store: Store,
    cipher: SessionCipher,
    anthropic_client,
    connect_base_url: Callable[[], str | None],
    invite_link: Callable[[str], str | None] = lambda payload: None,
    limiter: RateLimiter | None = None,
    daily_cap: DailyCap | None = None,
) -> tuple[Gateway, OnboardingFlow]:
    """Several parents, each with their own stored (encrypted) Webtop session and their own onboarding.
    `connect_base_url` is the public address of the login page, or None while there is none."""
    vault = SessionVault(store, cipher)

    def connector_factory(cookies, on_expired):
        return WebtopConnector(cookies, clock=date.today, on_expired=on_expired)

    registry = AgentRegistry(
        store,
        vault,
        connector_factory=connector_factory,
        agent_factory=lambda connector: Agent(
            anthropic_client, FamilyService(connector, clock=datetime.now)
        ),
    )
    flow = OnboardingFlow(
        store,
        vault,
        registry,
        ConnectLinks(),
        connector_factory=connector_factory,
        connect_url=lambda token: (
            f"{base.rstrip('/')}/?t={token}" if (base := connect_base_url()) else None
        ),
        welcome=WELCOME,
    )
    gateway = Gateway(
        allowlist,
        limiter or RateLimiter(),
        registry.agent_for,
        onboarding=flow,
        registry=registry,
        invites=InviteService(store, allowlist, link=invite_link),
        daily_cap=daily_cap,
    )
    return gateway, flow


def dev_cipher(store_path: Path) -> SessionCipher:
    """The session key for the file store: SESSION_ENCRYPTION_KEY if set, otherwise a dev key kept next to the
    file (owner-only) so stored sessions stay readable after a restart. Production keeps the key elsewhere."""
    env = os.environ.get("SESSION_ENCRYPTION_KEY")
    if env:
        return SessionCipher(env)
    from easytop.onboarding.crypto import new_key

    key_file = store_path.with_suffix(".key")
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(new_key())
    return SessionCipher(key_file.read_bytes().strip())


def make_lifecycle(runtime, scheduler, *, preload=None, health=None):
    """The adapter's start and stop hooks: the login page (when served), the nudge timer and the hourly session
    health check start with the bot and stop with it. `preload` runs first (the stored allowlist). (None, None)
    when there is nothing to run."""
    if runtime is None and scheduler is None and preload is None and health is None:
        return None, None
    tasks: list[asyncio.Task] = []

    async def start() -> None:
        if preload is not None:
            await preload()
        if runtime is not None:
            await runtime.start()
        if scheduler is not None:
            tasks.append(asyncio.create_task(scheduler.run()))
        if health is not None:
            tasks.append(asyncio.create_task(health.run()))

    async def stop() -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()
        if runtime is not None:
            await runtime.stop()

    return start, stop


MULTI_ENV = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SESSION_ENCRYPTION_KEY")


def make_viewer_check(bot_token: str):
    """The login page opens only for the Telegram user the link was made for (signed Mini App data)."""

    def check(parent_id: str, init_data: str) -> bool:
        return verify_init_data(init_data, bot_token) == chat_id_of(parent_id)

    return check


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bot", description="Run the Easytop Telegram bot (polling)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--session", help="saved Webtop cookie file: use the real account (read-only)"
    )
    mode.add_argument(
        "--multi",
        action="store_true",
        help="several parents, each connecting their own Webtop in the chat",
    )
    ap.add_argument(
        "--store",
        choices=["supabase", "memory", "file"],
        default="supabase",
        help="with --multi: where parents and encrypted sessions live (memory: lost on restart; file: a "
        "development store that survives restarts, see --store-file)",
    )
    ap.add_argument(
        "--store-file",
        default="spike_data/easytop_store.json",
        help="with --store file: the file (its dev encryption key sits next to it, owner-only)",
    )
    ap.add_argument(
        "--serve-login",
        action="store_true",
        help="with --multi: also run the phone login page (a cloudflared tunnel, or the fixed "
        "address in EASYTOP_CONNECT_URL)",
    )
    ap.add_argument(
        "--login-host",
        default="127.0.0.1",
        help="address the login page listens on (0.0.0.0 inside a container)",
    )
    ap.add_argument(
        "--login-port",
        type=int,
        default=int(os.environ.get("PORT", 8765)),
        help="login page port (default: $PORT, as Cloud Run sets it, else 8765)",
    )
    ap.add_argument("--login-chrome", action="store_true", help="login browser = installed Chrome")
    ap.add_argument(
        "--login-save-dir",
        help="DEV ONLY: also save each captured Webtop session as a file here (owner-only) so its real "
        "lifetime can be probed; never use with other people's accounts",
    )
    ap.add_argument(
        "--login-remember",
        action="store_true",
        help="(now the default, kept so older commands still work) set Webtop's own 'remember me' preference "
        "before the code screen, so the code login lasts as long as a remembered password login",
    )
    ap.add_argument(
        "--login-no-remember",
        action="store_true",
        help="DEV: do NOT set it (the SMS / email code login then lasts about 9 hours)",
    )
    ap.add_argument(
        "--login-trace",
        action="store_true",
        help="DEV ONLY: log the secret-free shape of the login requests and the cookie lifetime Webtop returns",
    )
    ap.add_argument("--login-headed", action="store_true", help="show the login browser window")
    args = ap.parse_args(argv)
    if args.serve_login and not args.multi:
        ap.error("--serve-login needs --multi")
    needed = REQUIRED_ENV + (MULTI_ENV if args.multi and args.store == "supabase" else ())
    missing = [
        v
        for v in needed
        if not (supabase_setting(v) if v.startswith("SUPABASE_") else os.environ.get(v))
    ]
    if missing:
        print(f"error: set {', '.join(missing)} in .env (run with: uv run --env-file .env ...)")
        return 2
    import anthropic

    configure_logging(os.environ.get("EASYTOP_LOG_FILE"), json_lines=json_logs_requested())
    configure_analytics()
    client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], timeout=90.0)
    flow: OnboardingFlow | None = None
    runtime: LoginRuntime | None = None
    scheduler: NudgeScheduler | None = None
    health: SessionHealth | None = None
    adapter: TelegramAdapter | None = (
        None  # the invite link needs the bot's username, known after start
    )
    public_url = os.environ.get("EASYTOP_CONNECT_URL")
    try:
        if args.multi:
            if args.store == "file":
                from easytop.store.file import FileStore

                path = Path(args.store_file)
                store: Store = FileStore(path)
                cipher = dev_cipher(path)
                print(
                    f"note: file store {path}: parents and sessions survive restarts (dev key next to it)"
                )
            elif args.store == "memory":
                from easytop.onboarding.crypto import new_key
                from easytop.store.memory import InMemoryStore

                store = InMemoryStore()
                cipher = SessionCipher(os.environ.get("SESSION_ENCRYPTION_KEY") or new_key())
                print("note: memory store, every connection is lost on restart")
            else:
                from easytop.store.supabase import SupabaseStore

                store, cipher = SupabaseStore.from_env(), SessionCipher.from_env()
            gateway, flow = build_multi_gateway(
                Allowlist.from_env(),
                store=store,
                cipher=cipher,
                anthropic_client=client,
                connect_base_url=lambda: runtime.base_url if runtime is not None else public_url,
                invite_link=lambda payload: adapter.invite_link(payload) if adapter else None,
                daily_cap=daily_cap_from_env(),
            )
            if args.serve_login:
                runtime = LoginRuntime(
                    flow.links,
                    flow.on_connected,
                    tunnel=None if public_url else CloudflaredTunnel(),
                    host=args.login_host,
                    port=args.login_port,
                    public_url=public_url,
                    viewer_check=make_viewer_check(os.environ["TELEGRAM_BOT_TOKEN"]),
                    chrome=args.login_chrome,
                    headless=not args.login_headed,
                    save_dir=Path(args.login_save_dir) if args.login_save_dir else None,
                    trace=args.login_trace,
                    remember=not args.login_no_remember,
                )
        else:
            gateway = build_gateway(
                Allowlist.from_env(), session=args.session, anthropic_client=client
            )
    except (EasytopError, ValueError) as e:
        print(f"error: {e}")
        return 2
    print(
        "Bot starting (Ctrl+C to stop). Data: "
        + (
            "each parent's own Webtop account (read-only)"
            if args.multi
            else "real Webtop account (read-only)"
            if args.session
            else "SYNTHETIC demo family"
        )
    )
    log = logging.getLogger("easytop.bot")
    log.info("bot started")
    try:
        adapter = TelegramAdapter(gateway)
        if flow is not None:
            flow.notify = (
                adapter.notify
            )  # the login page reports back; the parent gets the next question
            scheduler = NudgeScheduler(
                store, flow.registry, adapter.notify, expired_reply=flow.expired_reply
            )
            health = SessionHealth(store, flow.registry)
        on_start, on_stop = make_lifecycle(
            runtime,
            scheduler,
            preload=gateway.invites.load if gateway.invites is not None else None,
            health=health,
        )
        adapter.run_polling(os.environ["TELEGRAM_BOT_TOKEN"], on_start=on_start, on_stop=on_stop)
    except Exception as e:
        # Type only: exception text from the HTTP stack can hold a request URL, which holds the bot token.
        log.error("bot stopped by an error: %s", type(e).__name__)
        analytics.emit_error("bot", e)
        raise
    log.info("bot stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
