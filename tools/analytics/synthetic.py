"""SYNTHETIC demo traffic for the DEV analytics dashboard (task 6.11). Never real data.

Made-up families use the FakeConnector demo family (made-up kids and schools) through the REAL gateway, onboarding
flow, invites and registry, with a scripted stand-in for Claude, so the question labels, follow-ups, outcomes and
funnel steps are produced by the same code as in production. What has no offline path (the streamed browser login,
the hourly Webtop session check, the evening nudge timer, Telegram network errors) is emitted directly through the
same schema-checked emitter, with lifetimes and rates chosen to look like what the dev environment has shown.

Every event carries `synthetic: true` and a pseudonym from a demo key, so it can never be mistaken for a real parent.

    uv run python -m tools.analytics.synthetic --out synthetic_events.ndjson               # write the file only
    uv run python -m tools.analytics.synthetic --out synthetic_events.ndjson --load your-gcp-project
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace as NS
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
import httpx

from easytop import analytics
from easytop.access.invites import InviteService
from easytop.adapters.gateway import WELCOME, Gateway
from easytop.adapters.model import Incoming
from easytop.adapters.security import Allowlist, RateLimiter
from easytop.agent.core import Agent
from easytop.connector.fake import FakeConnector
from easytop.family.service import FamilyService
from easytop.onboarding.crypto import SessionCipher, new_key
from easytop.onboarding.flow import OnboardingFlow
from easytop.onboarding.links import ConnectLinks
from easytop.onboarding.registry import AgentRegistry
from easytop.onboarding.vault import SessionVault
from easytop.store.memory import InMemoryStore

ISRAEL = ZoneInfo("Asia/Jerusalem")
ADMIN = "telegram:1000000001"  # a made-up admin id; only its pseudonym is ever written
DEMO_KEY = b"easytop synthetic demo data, not a real environment key"
DATASET = "easytop_analytics_dev"

# (weight, kind) of what a parent asks; the kind decides which tools the scripted model calls.
ASKS = [
    (30, "tomorrow_all"),
    (18, "tomorrow_equipment"),
    (9, "tomorrow_homework"),
    (5, "tomorrow_flow"),
    (8, "tomorrow_one_kid"),
    (8, "messages"),
    (5, "exams"),
    (4, "schedule"),
    (3, "events"),
    (4, "mixed"),
    (6, "no_tool"),
]
TEXT = {  # one made-up question per language; the text never leaves this process
    "he": "מה הילדים צריכים מחר?",
    "en": "What do the kids need tomorrow?",
    "ru": "Что детям нужно завтра?",
}
ROUTES = [(70, "ministry"), (20, "code"), (10, "password")]
LIFETIME_H = {"ministry": (60.0, 400.0), "code": (8.0, 30.0), "password": (7.0, 10.0)}


def _pick(rng: random.Random, weighted: list[tuple[int, str]]) -> str:
    return rng.choices([v for _, v in weighted], weights=[w for w, _ in weighted])[0]


class VirtualClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def local(self) -> datetime:
        return self.now.astimezone(ISRAEL).replace(tzinfo=None)

    def monotonic(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _usage(rng: random.Random, *, final: bool, warm: bool) -> NS:
    return NS(
        input_tokens=rng.randint(900, 2600),
        output_tokens=rng.randint(180, 650) if final else rng.randint(30, 90),
        cache_read_input_tokens=3300 if warm else 0,
        cache_creation_input_tokens=0 if warm else 3300,
    )


class PlannedClient:
    """Stands in for Claude: before each question the generator sets the plan (tool calls, then a final answer, or
    an API failure). Every call moves the virtual clock by a model-like time."""

    def __init__(self, rng: random.Random, clock: VirtualClock):
        self.rng, self.clock = rng, clock
        self.plan: list[Any] = []
        self.messages = NS(create=self._create)
        self._last_call: datetime | None = None

    async def _create(self, **kwargs):
        after_tools = isinstance(
            kwargs["messages"][-1]["content"], list
        )  # tool results: Webtop time first
        self.clock.advance(
            self.rng.uniform(1.2, 3.4) + (self.rng.uniform(0.3, 1.6) if after_tools else 0)
        )
        item = self.plan.pop(0)
        if isinstance(item, Exception):
            raise item
        warm = self._last_call is not None and self.clock.now - self._last_call < timedelta(
            minutes=5
        )
        self._last_call = self.clock.now
        uses = item
        if uses:
            blocks = [
                NS(type="tool_use", id=f"t{i}", name=n, input=a) for i, (n, a) in enumerate(uses)
            ]
            return NS(
                content=blocks,
                stop_reason="tool_use",
                usage=_usage(self.rng, final=False, warm=warm),
            )
        text = NS(type="text", text="(synthetic answer)")
        return NS(
            content=[text], stop_reason="end_turn", usage=_usage(self.rng, final=True, warm=warm)
        )


def _tool_plan(kind: str, day: date) -> list[list[tuple[str, dict]]]:
    """The tool calls for one kind of question, as successive model turns, then the final answer ([])."""
    iso, later = day.isoformat(), (day + timedelta(days=14)).isoformat()
    turns = {
        "tomorrow_all": [[("get_tomorrow_brief", {})]],
        "tomorrow_equipment": [[("get_tomorrow_brief", {"focus": "equipment"})]],
        "tomorrow_homework": [[("get_tomorrow_brief", {"focus": "homework"})]],
        "tomorrow_flow": [[("get_tomorrow_brief", {"focus": "flow"})]],
        "tomorrow_one_kid": [[("get_tomorrow_brief", {"kid": "k1"})]],
        "messages": [[("get_messages", {"unread": True})]],
        "exams": [[("list_kids", {})], [("get_exams", {"kid": "k2", "from": iso, "to": later})]],
        "schedule": [[("get_schedule", {"kid": "k1", "date": iso})]],
        "events": [[("get_events", {"kid": "k2", "from": iso, "to": later})]],
        "mixed": [[("get_tomorrow_brief", {}), ("get_messages", {"unread": True})]],
        "no_tool": [],
    }[kind]
    return [*turns, []]


class World:
    def __init__(self, rng: random.Random, clock: VirtualClock):
        self.rng, self.clock = rng, clock
        self.client = PlannedClient(rng, clock)
        self.store = InMemoryStore()
        vault = SessionVault(self.store, SessionCipher(new_key()))

        def connector_factory(cookies, on_expired):
            return FakeConnector(clock.local().date())

        def agent_factory(connector):
            return Agent(
                self.client, FamilyService(connector, clock=clock.local), clock=clock.local
            )

        self.registry = AgentRegistry(
            self.store, vault, connector_factory=connector_factory, agent_factory=agent_factory
        )
        self.flow = OnboardingFlow(
            self.store,
            vault,
            self.registry,
            ConnectLinks(),
            connector_factory=connector_factory,
            connect_url=lambda t: f"https://login.example/?t={t}",
            welcome=WELCOME,
        )

        async def notify(pid, reply):
            return None

        self.flow.notify = notify
        allow = Allowlist({ADMIN})
        self.invites = InviteService(
            self.store, allow, link=lambda p: f"https://t.me/demo_bot?start={p}", clock=clock
        )
        self.gateway = Gateway(
            allow,
            RateLimiter(30, clock=clock.local),
            self.registry.agent_for,
            onboarding=self.flow,
            registry=self.registry,
            invites=self.invites,
            monotonic=clock.monotonic,
        )

    async def say(self, pid: str, text: str):
        return await self.gateway.handle(Incoming(pid, text=text))

    async def ask(self, pid: str, lang: str) -> None:
        kind = _pick(self.rng, ASKS)
        if self.rng.random() < 0.015:  # the model API fails twice (one retry): an error outcome
            err = anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            )
            self.client.plan = [err, err]
        else:
            self.client.plan = _tool_plan(kind, self.clock.local().date())
        await self.say(pid, TEXT[lang])
        self.client.plan = []


class Family:
    def __init__(self, n: int, rng: random.Random, days: int):
        self.pid = f"telegram:{2_000_000_000 + n}"
        self.lang = _pick(rng, [(80, "he"), (15, "en"), (5, "ru")])
        self.engagement = rng.choice([0.85, 0.7, 0.55, 0.35])
        self.join_day = rng.randint(0, max(0, days // 2))
        # Some families try the bot for a few days and stop asking (no week-2 return); nudges keep coming to them.
        self.last_active_day = self.join_day + rng.randint(1, 5) if rng.random() < 0.3 else None
        self.reacts_to_nudge = (
            rng.random() < 0.55
        )  # asks within minutes of the nudge when it comes on a day they ask
        self.tries_it_now = (
            rng.random() < 0.5
        )  # asks something right after the setup, before the first evening
        self.redeems = rng.random() > 0.12
        self.route = _pick(rng, ROUTES)
        self.nudge_hour: int | None = None
        self.login_at: datetime | None = None
        self.checked_until: datetime | None = None  # the last hourly check already emitted
        self.lifetime_h = 0.0


async def _login(world: World, fam: Family, rng: random.Random, *, reconnect: bool) -> None:
    clock = world.clock
    clock.advance(rng.uniform(5, 40))
    analytics.emit("connect_opened", fam.pid)
    if not reconnect and rng.random() < 0.18:  # leaves the first time, comes back later
        clock.advance(rng.uniform(20, 120))
        analytics.emit(
            "connect_abandoned",
            fam.pid,
            reason="left",
            login_seconds=round(rng.uniform(20, 120), 1),
        )
        clock.advance(rng.uniform(600, 3600))
        await world.say(fam.pid, "/start")
        analytics.emit("connect_opened", fam.pid)
    seconds = {
        "ministry": rng.uniform(25, 60),
        "code": rng.uniform(60, 150),
        "password": rng.uniform(30, 80),
    }[fam.route]
    clock.advance(seconds)
    analytics.emit(
        "login_captured",
        fam.pid,
        route=fam.route,
        login_seconds=round(seconds, 1),
        cookie_hours=4316.67,
        taps=rng.randint(4, 40),
        frames_dropped=rng.choice([0, 0, 0, 1, 2]),
    )
    fam.login_at = clock.now
    fam.checked_until = clock.now
    lo, hi = LIFETIME_H[fam.route]
    fam.lifetime_h = rng.uniform(lo, hi)
    await world.flow.on_connected(fam.pid, {"webToken": "synthetic"})


async def _onboard(world: World, fam: Family, rng: random.Random) -> bool:
    invite = await world.say(ADMIN, "invite")
    if not fam.redeems:
        return False
    world.clock.advance(rng.uniform(600, 6 * 3600))
    payload = invite.text.split("start=")[1].split()[0]
    await world.say(fam.pid, f"/start {payload}")  # redeems, then the connect button
    drop = rng.random()
    if drop < 0.1:  # never opens the login page
        return False
    if drop < 0.2:  # opens it, leaves, never comes back
        world.clock.advance(rng.uniform(30, 600))
        analytics.emit("connect_opened", fam.pid)
        analytics.emit(
            "connect_abandoned", fam.pid, reason="left", login_seconds=round(rng.uniform(15, 90), 1)
        )
        return False
    await _login(world, fam, rng, reconnect=False)
    world.clock.advance(rng.uniform(10, 60))
    await world.say(fam.pid, {"he": "כן", "en": "yes", "ru": "yes"}[fam.lang])
    world.clock.advance(rng.uniform(10, 60))
    choice = rng.random()
    if choice < 0.5:
        fam.nudge_hour = rng.choice([19, 20, 20, 21])
        await world.say(fam.pid, f"{fam.nudge_hour}:00")
    elif choice < 0.8:
        await world.say(fam.pid, {"he": "לא", "en": "no", "ru": "no"}[fam.lang])
    else:
        await world.ask(
            fam.pid, fam.lang
        )  # answers the offer with a question: it goes to the agent
    if choice < 0.8 and fam.tries_it_now:
        world.clock.advance(rng.uniform(30, 300))
        await world.ask(fam.pid, fam.lang)
    return True


def _session_alive(fam: Family, at: datetime) -> bool:
    return fam.login_at is not None and (at - fam.login_at).total_seconds() / 3600 < fam.lifetime_h


async def _hourly_checks(world: World, fam: Family, until: datetime) -> None:
    """The health check's view of this family's session, hour by hour up to `until`."""
    if fam.login_at is None:
        return
    t = fam.checked_until.replace(minute=12, second=33, microsecond=0) + timedelta(hours=1)
    while t <= until and fam.login_at is not None:
        fam.checked_until = t
        world.clock.now = t
        age = (t - fam.login_at).total_seconds() / 3600
        if age >= fam.lifetime_h:
            analytics.emit("session_check", fam.pid, status="dead", age_h=round(age, 1))
            await world.registry.drop_session(fam.pid)
            fam.login_at = None
            return
        analytics.emit("session_check", fam.pid, status="ok", age_h=round(age, 1))
        t += timedelta(hours=1)


async def _simulate(
    families: int, days: int, seed: int, end: datetime, clock: VirtualClock
) -> None:
    rng = random.Random(seed)
    start = (end - timedelta(days=days)).astimezone(ISRAEL).date()
    world = World(rng, clock)
    for n in range(families):
        fam = Family(n, rng, days)
        first = datetime.combine(
            start + timedelta(days=fam.join_day),
            time(rng.randint(9, 20), rng.randint(0, 59)),
            ISRAEL,
        )
        clock.now = first.astimezone(UTC)
        if not await _onboard(world, fam, rng):
            continue
        for d in range(fam.join_day, days + 1):
            day = start + timedelta(days=d)
            own = datetime.combine(
                day, time(rng.randint(16, 21), rng.randint(0, 59)), ISRAEL
            ).astimezone(UTC)
            nudge_at = None
            if fam.nudge_hour is not None and day.weekday() not in (4, 5):  # evenings before school
                nudge_at = datetime.combine(day, time(fam.nudge_hour, 0, 5), ISRAEL).astimezone(UTC)
            asks = (
                day.weekday() != 5
                and rng.random() <= fam.engagement
                and (fam.last_active_day is None or d <= fam.last_active_day)
            )
            visit = own
            if asks and nudge_at is not None:
                if fam.reacts_to_nudge and _session_alive(fam, nudge_at):
                    visit = nudge_at + timedelta(
                        minutes=rng.uniform(1, 25)
                    )  # the nudge brought them
                elif nudge_at <= own <= nudge_at + timedelta(minutes=30):
                    visit = nudge_at + timedelta(minutes=rng.uniform(31, 120))  # their own habit
            moments = []
            if nudge_at is not None:
                moments.append((nudge_at, "nudge"))
            elif fam.nudge_hour is not None and day.weekday() == 4:
                moments.append((own, "no_school"))
            if asks:
                moments.append((visit, "visit"))
            for at, kind in sorted(moments):
                if at > end or at < clock.now:
                    continue
                await _hourly_checks(world, fam, at)
                clock.now = at
                if kind == "nudge":
                    result = "sent" if _session_alive(fam, at) else "expired"
                    analytics.emit("nudge", fam.pid, result=result)
                elif kind == "no_school":
                    analytics.emit("nudge", fam.pid, result="no_school")
                else:
                    if not _session_alive(fam, clock.now) and fam.login_at is None:
                        await world.say(
                            fam.pid, TEXT[fam.lang]
                        )  # gets the reconnect button (connect_offered expired)
                        await _login(world, fam, rng, reconnect=True)
                    for i in range(rng.choice([1, 1, 2, 2, 3])):
                        clock.advance(rng.uniform(20, 300) if i else 0)
                        await world.ask(fam.pid, fam.lang)
                    if rng.random() < 0.1:
                        analytics.emit_error("telegram", TimeoutError())
        await _hourly_checks(world, fam, end)


def generate(
    *, families: int = 12, days: int = 60, seed: int = 1, end: datetime | None = None
) -> list[dict]:
    """Synthetic events, oldest first. Uses a strict emitter: anything off-schema fails here, not in BigQuery."""
    end = end or datetime.now(UTC)
    clock = VirtualClock(end)
    previous = analytics.current()
    analytics.configure(env="dev", key=DEMO_KEY, clock=clock, strict=True, synthetic=True)
    captured: list[dict] = []

    class Capture(logging.Handler):
        def emit(self, record):
            if hasattr(record, "analytics"):
                captured.append(record.analytics)

    log = logging.getLogger("easytop.analytics")
    handler, old_level, old_propagate = Capture(), log.level, log.propagate
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        asyncio.run(_simulate(families, days, seed, end, clock))
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        log.propagate = old_propagate
        analytics.set_current(previous)
    return sorted((e for e in captured if e["ts"] <= _iso(end)), key=lambda e: e["ts"])


def _iso(t: datetime) -> str:
    t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def to_ndjson(events: list[dict]) -> str:
    """One row per event for the `synthetic_events` table: {"payload": "<the event as JSON>"}."""
    return "".join(
        json.dumps({"payload": json.dumps(e, ensure_ascii=False, separators=(",", ":"))}) + "\n"
        for e in events
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="SYNTHETIC analytics events for the dev dashboard (never real data)"
    )
    ap.add_argument("--families", type=int, default=12)
    ap.add_argument(
        "--days",
        type=int,
        default=60,
        help="history length: 60 lets the 28-day period have a previous one",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=1,
        help="1 gives every dashboard panel something to show (7 has almost no nudge users)",
    )
    ap.add_argument("--out", default="synthetic_events.ndjson")
    ap.add_argument(
        "--load", metavar="PROJECT_ID", help=f"replace {DATASET}.synthetic_events in this project"
    )
    args = ap.parse_args(argv)
    events = generate(families=args.families, days=args.days, seed=args.seed)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(to_ndjson(events))
    counts = Counter(e["event"] for e in events)
    print(
        f"SYNTHETIC data (FakeConnector demo family, synthetic=true): {len(events)} events -> {args.out}"
    )
    for name, n in counts.most_common():
        print(f"  {name:18} {n}")
    if args.load:
        table = f"{args.load}:{DATASET}.synthetic_events"
        cmd = [
            "bq",
            "--project_id",
            args.load,
            "load",
            "--replace",
            "--source_format=NEWLINE_DELIMITED_JSON",
            table,
            args.out,
            "payload:STRING",
        ]
        print("loading:", " ".join(cmd))
        return subprocess.call(cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
