"""Eval harness: cases are data, checks are programmatic. Live runs need ANTHROPIC_API_KEY.

Golden and guardrail cases run against the SYNTHETIC family (made-up names) with a fixed clock.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from easytop.agent.core import MODEL, Agent, AgentResult, Conversation
from easytop.agent.pricing import cost_usd
from easytop.connector.fake import FakeConnector, load_synthetic
from easytop.family.service import FamilyService

NOW = datetime(2026, 10, 5, 18, 42)  # Monday evening; next school day = Tue 2026-10-06
SKIP_MESSAGE = "Live evals skipped: set ANTHROPIC_API_KEY to run them (they call the paid API)."


@dataclass(frozen=True)
class Check:
    name: str
    test: Callable[[AgentResult], str | None]  # None = pass, else failure message
    positive: bool = False  # "answer must contain something": used to sanity-check against the data


def _norm(s: str) -> str:
    return s.casefold()


def contains_all(*subs: str) -> Check:
    def t(r):
        miss = [s for s in subs if _norm(s) not in _norm(r.text)]
        return f"missing {miss}" if miss else None

    return Check(f"contains_all{subs}", t, positive=True)


def contains_any(*subs: str) -> Check:
    def t(r):
        return None if any(_norm(s) in _norm(r.text) for s in subs) else f"none of {subs} found"

    return Check(f"contains_any{subs}", t, positive=True)


def regex_present(pattern: str) -> Check:
    """The answer must match `pattern` (a formatting rule, not a fact from the data)."""

    def t(r):
        return None if re.search(pattern, r.text) else f"pattern {pattern!r} not found"

    return Check(f"regex_present({pattern!r})", t)


def wording_any(*subs: str) -> Check:
    """Like contains_any, for the model's own wording (headings, hedges): not a fact from the data, so the
    offline data-reachability test skips it."""
    check = contains_any(*subs)
    return Check(f"wording_any{subs}", check.test, positive=False)


def not_contains(*subs: str) -> Check:
    def t(r):
        bad = [s for s in subs if _norm(s) in _norm(r.text)]
        return f"forbidden present: {bad}" if bad else None

    return Check(f"not_contains{subs}", t)


def regex_absent(pattern: str) -> Check:
    def t(r):
        m = re.search(pattern, r.text)
        return f"pattern {pattern!r} matched {m.group(0)!r}" if m else None

    return Check(f"regex_absent({pattern})", t)


def no_tool_calls() -> Check:
    return Check(
        "no_tool_calls", lambda r: f"tools called: {r.tool_calls}" if r.tool_calls else None
    )


def max_lines(n: int) -> Check:
    def t(r):
        k = len([ln for ln in r.text.splitlines() if ln.strip()])
        return f"{k} lines > {n}" if k > n else None

    return Check(f"max_lines({n})", t)


def max_chars(n: int) -> Check:
    return Check(
        f"max_chars({n})",
        lambda r: f"{len(r.text)} characters > {n}" if len(r.text) > n else None,
    )


def script_ratio(low: str, high: str, at_least: float) -> Check:
    """Share of letters within [low, high] (e.g. Cyrillic / Hebrew) must be >= at_least."""

    def t(r):
        letters = [c for c in r.text if c.isalpha()]
        if not letters:
            return "no letters in answer"
        share = sum(low <= c <= high for c in letters) / len(letters)
        return None if share >= at_least else f"script share {share:.2f} < {at_least}"

    return Check(f"script_ratio({low!r}-{high!r}>={at_least})", t)


def equipment_lacks(*words: str) -> Check:
    """None of `words` may sit in an Equipment section: from its 🎒 heading to the next category heading
    (📚 📝 🗓️), the next child or the general block, a blank line, or the end. Every Equipment section is judged."""

    def t(r):
        sections = re.findall(r"🎒(.*?)(?=\n\s*\n|\n\s*(?:📚|📝|🗓|\*\*__)|\Z)", r.text, re.S)
        if not sections:
            return "no Equipment section (🎒) in the answer"
        bad = [w for w in words if any(_norm(w) in _norm(s) for s in sections)]
        return f"Equipment section holds {bad}" if bad else None

    return Check(f"equipment_lacks{words}", t)


def _calls(r) -> list[str]:
    return [u["name"] for u in r.tool_uses]


def tool_called(name: str, **args: str) -> Check:
    """The agent called `name` with (at least) these arguments, whatever it wrote afterwards."""

    def t(r):
        for u in r.tool_uses:
            given = u.get("input") or {}
            if u["name"] == name and all(given.get(k) == v for k, v in args.items()):
                return None
        return f"{name}{args or ''} was not called (calls: {_calls(r)})"

    return Check(f"tool_called({name},{args})", t)


def tool_not_called(name: str) -> Check:
    return Check(
        f"tool_not_called({name})",
        lambda r: f"{name} was called: {r.tool_uses}" if name in _calls(r) else None,
    )


def tool_arg_absent(name: str, arg: str) -> Check:
    """No call to `name` passed `arg` at all (a call that is refused for lacking it is fine)."""

    def t(r):
        bad = [u for u in r.tool_uses if u["name"] == name and (u.get("input") or {}).get(arg)]
        return f"{name} was called with {arg}: {bad}" if bad else None

    return Check(f"tool_arg_absent({name},{arg})", t)


def tool_arg_not(name: str, arg: str, *values: str) -> Check:
    """No call to `name` passed `arg` as one of `values` (an absent argument is fine)."""

    def t(r):
        bad = [
            u for u in r.tool_uses if u["name"] == name and (u.get("input") or {}).get(arg) in values
        ]
        return f"{name} was called with {arg} in {values}: {bad}" if bad else None

    return Check(f"tool_arg_not({name},{arg},{values})", t)


def tool_called_any(*names: str) -> Check:
    return Check(
        f"tool_called_any{names}",
        lambda r: None if set(names) & set(_calls(r)) else f"none of {names} called ({_calls(r)})",
    )


class RecordingReminders:
    """Stands in for the parent's reminder setting in evals: the synthetic family has no store."""

    def __init__(self):
        self.calls: list = []

    async def set(self, when, content: str | None = None) -> str | None:
        self.calls.append((when, content))
        return None if when is None else (content or "none")


def appears_before(first: str, later: str) -> Check:
    def t(r):
        a, b = _norm(r.text).find(_norm(first)), _norm(r.text).find(_norm(later))
        if a < 0:
            return f"{first!r} not in answer"
        return f"{later!r} comes before {first!r}" if 0 <= b < a else None

    return Check(f"appears_before({first!r},{later!r})", t)


@dataclass(frozen=True)
class EvalCase:
    id: str
    group: str  # "guardrail" | "golden" | "category"
    question: str
    checks: list[Check]
    patch: Callable[[dict], None] | None = None
    fail_kids: frozenset[str] = frozenset()
    now: datetime = NOW
    tags: list[str] = field(default_factory=list)
    followup: str | None = (
        None  # a second question in the same conversation; checks judge ITS answer
    )


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    failures: list[str]
    answer: str
    tool_calls: list[str]
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


def build_service(case: EvalCase) -> FamilyService:
    data = copy.deepcopy(load_synthetic())
    if case.patch:
        case.patch(data)
    conn = FakeConnector(case.now.date(), data, fail_kids=set(case.fail_kids))
    return FamilyService(conn, clock=lambda: case.now)


def build_agent(case: EvalCase, client, *, model: str = MODEL, effort: str | None = None) -> Agent:
    return Agent(
        client,
        build_service(case),
        model=model,
        effort=effort,
        clock=lambda: case.now,
        reminders=RecordingReminders(),
    )


async def run_case(
    case: EvalCase, client, *, model: str = MODEL, effort: str | None = None
) -> CaseOutcome:
    agent, convo = build_agent(case, client, model=model, effort=effort), Conversation()
    first = await agent.ask(convo, case.question)
    result, tin, tout = first, first.input_tokens, first.output_tokens
    cw, cr = first.cache_write_tokens, first.cache_read_tokens
    if case.followup:
        result = await agent.ask(convo, case.followup)
        tin, tout = tin + result.input_tokens, tout + result.output_tokens
        cw, cr = cw + result.cache_write_tokens, cr + result.cache_read_tokens
    failures = [f"{c.name}: {msg}" for c in case.checks if (msg := c.test(result))]
    return CaseOutcome(
        case.id,
        not failures,
        failures,
        result.text,
        [*first.tool_calls, *(result.tool_calls if case.followup else [])],
        tin,
        tout,
        cw,
        cr,
    )


def live_client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    return anthropic.AsyncAnthropic(timeout=90.0, max_retries=2)


async def run_all(
    cases: list[EvalCase],
    client,
    *,
    concurrency: int = 4,
    on_done: Callable[[CaseOutcome], None] | None = None,
    model: str = MODEL,
    effort: str | None = None,
) -> list[CaseOutcome]:
    """Run the cases `concurrency` at a time; results come back in input order, `on_done` fires as each
    one finishes (so a long live run shows progress)."""
    gate = asyncio.Semaphore(concurrency)

    async def one(case: EvalCase) -> CaseOutcome:
        async with gate:
            outcome = await run_case(case, client, model=model, effort=effort)
        if on_done:
            on_done(outcome)
        return outcome

    return list(await asyncio.gather(*(one(c) for c in cases)))


def select_cases(
    cases: list[EvalCase], *, group: str | None = None, ids: list[str] | None = None
) -> list[EvalCase]:
    """Cases of one group and/or with the given ids (input order kept). Unknown ids are an error."""
    known = {c.id for c in cases}
    unknown = [i for i in ids or [] if i not in known]
    if unknown:
        raise ValueError(f"unknown case ids: {unknown}")
    return [
        c for c in cases if (group is None or c.group == group) and (ids is None or c.id in ids)
    ]


def repeated(cases: list[EvalCase], times: int) -> list[EvalCase]:
    """Each case `times` times in a row (model answers vary, so one pass shows little)."""
    return [c for c in cases for _ in range(times)]


def summarize(outcomes: list[CaseOutcome]) -> list[str]:
    """'<id>: <passed>/<runs> passed' per case id, in first-seen order."""
    runs: dict[str, list[bool]] = {}
    for o in outcomes:
        runs.setdefault(o.case_id, []).append(o.passed)
    return [f"{cid}: {sum(r)}/{len(r)} passed" for cid, r in runs.items()]


def totals_line(outcomes: list[CaseOutcome], model: str = MODEL) -> str:
    tin, tout = sum(o.input_tokens for o in outcomes), sum(o.output_tokens for o in outcomes)
    cw, cr = sum(o.cache_write_tokens for o in outcomes), sum(o.cache_read_tokens for o in outcomes)
    cost = cost_usd(model, tin, tout, cw, cr)
    cached = f" (cache written={cw} read={cr})" if cw or cr else ""
    return (
        f"{sum(o.passed for o in outcomes)}/{len(outcomes)} passed; tokens in={tin} out={tout}{cached}"
        + (f"; cost about ${cost:.2f} ({model})" if cost is not None else "")
    )


def format_outcome(o: CaseOutcome) -> str:
    """One line for a pass; for a failure the failed checks and the answer (the evals use synthetic data)."""
    if o.passed:
        return f"PASS {o.case_id}"
    lines = [f"FAIL {o.case_id}", *(f"   {f}" for f in o.failures), "   --- answer ---"]
    return "\n".join([*lines, *(f"   {ln}" for ln in o.answer.splitlines())])


def main(argv: list[str] | None = None) -> int:
    import argparse

    from easytop.evals.cases import ALL_CASES

    ap = argparse.ArgumentParser(
        prog="evals", description="Run the guardrail / golden / category evals"
    )
    ap.add_argument("--group", choices=["guardrail", "golden", "category"], help="only this group")
    ap.add_argument("--only", nargs="+", metavar="ID", help="only these case ids")
    ap.add_argument("--concurrency", type=int, default=4, help="cases in parallel (default 4)")
    ap.add_argument("--repeat", type=int, default=1, help="run each case this many times")
    ap.add_argument("--model", default=MODEL, help=f"model to score (default {MODEL})")
    ap.add_argument(
        "--effort", choices=["low", "medium", "high"], help="thinking effort (default: the model's own)"
    )
    args = ap.parse_args(argv)
    client = live_client()
    if client is None:
        print(SKIP_MESSAGE)
        return 0
    cases = repeated(select_cases(ALL_CASES, group=args.group, ids=args.only), args.repeat)
    print(f"running {len(cases)} cases, {args.concurrency} at a time (paid API)...", flush=True)

    def show(o: CaseOutcome) -> None:
        print(format_outcome(o), flush=True)

    outcomes = asyncio.run(
        run_all(
            cases,
            client,
            concurrency=args.concurrency,
            on_done=show,
            model=args.model,
            effort=args.effort,
        )
    )
    if args.repeat > 1:
        print(*summarize(outcomes), sep="\n")
    print(totals_line(outcomes, args.model))
    return 0 if all(o.passed for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
