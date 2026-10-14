"""Numbers to plain sentences for the analytics page (Spec 12 "Easy reading rules").

Pure functions over small dataclasses: no pandas, no Streamlit, no BigQuery, so every sentence is unit-tested. The
rules that keep the page honest while the product has one or two families:
- below 5 families a number is said as a count ("2 of 3 families"), not as a percentage alone;
- a change against the previous period is stated only when both periods have at least 20 questions and it moved
  more than 10%; with fewer questions the answer is "too little data to compare yet";
- numbers are rounded for people: whole percentages, one decimal for seconds, cents with one decimal for amounts
  under ten cents.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

SMALL_GROUP = 5  # families (or logins): below this, say counts
MIN_QUESTIONS_TO_COMPARE = 20
MOVE_TO_STATE = 0.10
NOT_ENOUGH = "too little data to compare yet"


@dataclass(frozen=True)
class Sentence:
    text: str
    note: str | None = None  # a second, quieter line under the sentence
    alert: bool = False  # red: only for a real failure


@dataclass(frozen=True)
class PeriodNumbers:
    """One period of the headline: the coverage numbers and what the same period cost."""

    active_families: int
    school_evenings: int
    tomorrow_pairs: int
    coverage: float | None
    questions: int
    cost_per_family_month_usd: float | None


@dataclass(frozen=True)
class WeekNumbers:
    week: date
    active_families: int
    days_hist: tuple[int, int, int, int, int]  # families active on 1, 2, 3, 4, 5+ days that week


@dataclass(frozen=True)
class Week2:
    families: int  # first answered question at least 14 days old
    returned: int  # asked again 7-13 days after it


@dataclass(frozen=True)
class Step:
    order: int
    label: str
    n: int


@dataclass(frozen=True)
class ReconnectWeek:
    week: date
    active_families: int
    reconnects: int


@dataclass(frozen=True)
class RouteLifetime:
    route: str
    state: str  # ended | alive (alive = still valid, the hours are a lower bound)
    logins: int
    median_hours: float
    max_hours: float


@dataclass(frozen=True)
class WorthNumbers:
    questions: int
    active_families: int
    cost_per_family_month_usd: float | None
    p95_seconds: float | None
    failures: int
    no_data: int


# ---------------------------------------------------------------- rounding for people


def percent(x: float) -> str:
    return f"{int(x * 100 + 0.5)}%"


def usd(x: float) -> str:
    if x == 0:
        return "$0.00"
    if x >= 0.10:
        return f"${x:.2f}"
    return f"{x * 100:.1f} cents"


def seconds(x: float) -> str:
    text = f"{x:.1f}"
    return f"{text.removesuffix('.0')} s"


def _trim(x: float, digits: int = 1) -> str:
    return f"{x:.{digits}f}".removesuffix(".0")


def minutes_text(minutes: float) -> str:
    if minutes < 1:
        return "under a minute"
    if minutes < 90:
        n = round(minutes)
        return f"{n} minute{'' if n == 1 else 's'}"
    hours = minutes / 60
    if hours < 48:
        return f"{_trim(hours)} hours"
    return f"{_trim(hours / 24)} days"


def hours_text(hours: float) -> str:
    if hours < 48:
        return f"{round(hours)} h"
    return f"{_trim(hours / 24)} days"


def _families(n: int) -> str:
    return f"{n} family" if n == 1 else f"{n} families"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ---------------------------------------------------------------- comparing with the previous period


def change(
    now: float | None,
    before: float | None,
    questions_now: int,
    questions_before: int,
    show: Callable[[float], str] = percent,
) -> str:
    """ "up 40%, from 50%", "down 12%, from ...", "about the same", or "too little data to compare yet"."""
    if (
        now is None
        or not before
        or questions_now < MIN_QUESTIONS_TO_COMPARE
        or questions_before < MIN_QUESTIONS_TO_COMPARE
    ):
        return NOT_ENOUGH
    move = round((now - before) / before, 6)
    if abs(move) <= MOVE_TO_STATE:
        return "about the same"
    return f"{'up' if move > 0 else 'down'} {percent(abs(move))}, from {show(before)}"


# ---------------------------------------------------------------- the headline


def headline(days: int, now: PeriodNumbers | None, before: PeriodNumbers | None) -> Sentence:
    if now is None or now.active_families == 0:
        return Sentence(f"Nobody asked anything in the last {days} days.")
    families = now.active_families
    cost = (
        f" Cost about {usd(now.cost_per_family_month_usd)} per family per month."
        if now.cost_per_family_month_usd is not None
        else ""
    )
    if now.school_evenings == 0 or now.coverage is None:
        text = (
            f"Last {days} days: {_families(families)} asked questions, "
            f"but there was no school evening to measure yet.{cost}"
        )
        return Sentence(text)
    slots = families * now.school_evenings
    if families < SMALL_GROUP:
        lead = (
            f"{_families(families)} asked about tomorrow on {now.tomorrow_pairs} of {slots} "
            f"school evenings ({percent(now.coverage)})"
        )
    else:
        lead = (
            f"{families} active families asked about tomorrow on {percent(now.coverage)} of school "
            f"evenings ({now.tomorrow_pairs} of {slots} family-evenings)"
        )
    verdict = change(
        now.coverage,
        before.coverage if before else None,
        now.questions,
        before.questions if before else 0,
    )
    return Sentence(
        f"Last {days} days: {lead}.{cost}",
        note=f"Compared with the {days} days before: {verdict}.",
    )


# ---------------------------------------------------------------- 1. habit


def _typical_days(weeks: Sequence[WeekNumbers]) -> int | None:
    values: list[int] = []
    for w in weeks:
        for days, count in enumerate(w.days_hist, start=1):
            values += [days] * count
    if len(values) < SMALL_GROUP:
        return None
    values.sort()
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    return round(median)


def _second_week(w2: Week2 | None) -> str:
    if w2 is None or w2.families == 0:
        return "No family is old enough yet for the second-week check."
    n, back = w2.families, w2.returned
    if n == 1:
        return (
            "The family came back in its second week."
            if back
            else "The family did not come back in its second week."
        )
    if back == 0:
        return f"None of the {n} families came back in their second week."
    if back == n:
        return (
            "Both families came back in their second week."
            if n == 2
            else f"All {n} families came back in their second week."
        )
    shown = f" ({percent(back / n)})" if n >= SMALL_GROUP else ""
    return f"{back} of {n} families{shown} came back in their second week."


def habit(weeks: Sequence[WeekNumbers], week2: Week2 | None) -> Sentence:
    if not any(w.active_families for w in weeks):
        return Sentence("Nobody asked anything in this period.")
    text = _second_week(week2)
    typical = _typical_days(weeks)
    if typical is not None:
        text += f" In a typical week a family asked on {_plural(typical, 'day', 'days')}."
    return Sentence(text)


# ---------------------------------------------------------------- 2. activation

# Said for the step a parent did not reach, keyed by (the last step reached, the next one).
DROPPED_BETWEEN = {
    (1, 2): "the invite and redeeming it",
    (2, 3): "redeeming the invite and the connect button",
    (3, 4): "the connect button and opening the login page",
    (4, 5): "opening the login page and signing in",
    (5, 6): "signing in and confirming the kids",
    (6, 7): "confirming the kids and the first answer",
}
LAST_STEP = 7
FUNNEL_NAMES = {
    1: "invite",
    2: "redeemed",
    3: "connect button",
    4: "login page",
    5: "signed in",
    6: "kids confirmed",
    7: "first answer",
}


def _funnel(steps: Sequence[Step]) -> list[Step]:
    return sorted((s for s in steps if s.order <= LAST_STEP), key=lambda s: s.order)


def funnel_labels(steps: Sequence[Step]) -> list[str]:
    """Short axis labels ("3 connect button"): a chart axis cuts anything longer than about 16 characters."""
    return [f"{s.order} {FUNNEL_NAMES.get(s.order, s.label)}" for s in _funnel(steps)]


def kept_line(steps: Sequence[Step]) -> str:
    """The share that got through each step, from the step before it."""
    ordered = _funnel(steps)
    parts = [
        f"{FUNNEL_NAMES.get(b.order, b.label)} {percent(min(b.n / a.n, 1) if a.n else 0)}"
        for a, b in zip(ordered, ordered[1:], strict=False)
    ]
    return f"Kept from the step before: {', '.join(parts)}." if parts else ""


def activation(
    steps: Sequence[Step], median_minutes: float | None, timed_families: int
) -> Sentence:
    ordered = _funnel(steps)
    if not ordered or ordered[0].n == 0:
        return Sentence("Nobody has started signing up yet.")
    worst: tuple[int, Step, Step] | None = None
    for a, b in zip(ordered, ordered[1:], strict=False):
        lost = max(a.n - b.n, 0)
        if lost and (worst is None or lost > worst[0]):
            worst = (lost, a, b)
    wait = (
        f"the median parent had an answer {minutes_text(median_minutes)} after the button"
        if median_minutes is not None and timed_families
        else None
    )
    if worst is None:
        reached = ordered[-1].n
        head = (
            "The one parent got to a first answer"
            if reached == 1
            else f"All {reached} got to a first answer"
        )
    else:
        lost, a, b = worst
        where = DROPPED_BETWEEN.get(
            (a.order, b.order), f"{a.label.split(' ', 1)[-1]} and {b.label.split(' ', 1)[-1]}"
        )
        head = f"{lost} of {a.n} dropped between {where}"
    return Sentence(f"{head}; {wait}." if wait else f"{head}.")


# ---------------------------------------------------------------- 3. friction

ROUTE_NAMES = {
    "ministry": "Ministry",
    "code": "SMS-code",
    "password": "Password",
    "unknown": "Other",
}


def _lifetimes(routes: Sequence[RouteLifetime]) -> str:
    by_route: dict[str, dict[str, RouteLifetime]] = {}
    for r in routes:
        by_route.setdefault(r.route, {})[r.state] = r
    order = sorted(by_route, key=lambda k: -sum(r.logins for r in by_route[k].values()))
    parts = []
    for route in order:
        name = ROUTE_NAMES.get(route, "Other")
        ended, alive = by_route[route].get("ended"), by_route[route].get("alive")
        if ended:
            part = f"{name} logins last about {hours_text(ended.median_hours)}"
            if ended.logins < SMALL_GROUP:
                part += f" ({_plural(ended.logins, 'login', 'logins')})"
            if alive:
                verb = "is" if alive.logins == 1 else "are"
                part += f", and {alive.logins} {verb} still valid after about {hours_text(alive.median_hours)}"
        else:
            part = f"{name} logins were still valid after about {hours_text(alive.median_hours)} when last checked"
            if alive.logins < SMALL_GROUP:
                part += f" ({_plural(alive.logins, 'login', 'logins')})"
        parts.append(part)
    return "; ".join(parts) + "." if parts else ""


def friction(weeks: Sequence[ReconnectWeek], routes: Sequence[RouteLifetime]) -> Sentence:
    reconnects = sum(w.reconnects for w in weeks)
    family_weeks = sum(w.active_families for w in weeks)
    peak = max((w.active_families for w in weeks), default=0)
    lifetimes = _lifetimes(routes)
    if not weeks and not routes:
        return Sentence("No reconnects or logins in this period yet.")
    if not weeks:
        return Sentence(lifetimes)
    if reconnects == 0:
        first = "Nobody had to reconnect."
    elif peak < SMALL_GROUP or not family_weeks:
        verb = "was" if reconnects == 1 else "were"
        first = (
            f"There {verb} {_plural(reconnects, 'reconnect', 'reconnects')}; "
            f"at most {_families(peak)} {'was' if peak == 1 else 'were'} active in a week."
        )
    else:
        rate = _trim(reconnects / family_weeks)
        first = (
            "Families reconnected once a week."
            if rate == "1"
            else f"Families reconnected {rate} times a week."
        )
    return Sentence(f"{first} {lifetimes}".strip())


# ---------------------------------------------------------------- 4. worth it


def worth_it(
    days: int,
    now: WorthNumbers | None,
    before: WorthNumbers | None,
    nudges_sent: int,
    nudges_followed: int,
) -> Sentence:
    if now is None or now.questions == 0:
        return Sentence(
            f"No questions in the last {days} days, so there is nothing to time or price yet."
        )
    clauses = []
    if now.cost_per_family_month_usd is not None:
        clauses.append(f"about {usd(now.cost_per_family_month_usd)} per family per month")
    if now.p95_seconds is not None:
        clauses.append(f"95% of answers within {seconds(now.p95_seconds)}")
    if now.failures:
        clauses.append(
            f"{_plural(now.failures, 'failure', 'failures')} in {now.questions} questions"
        )
    else:
        clauses.append(f"no failures in {now.questions} questions")
    sentences = [", ".join(clauses)[0].upper() + ", ".join(clauses)[1:] + "."]
    if now.no_data:
        sentences.append(f"{now.no_data} of {now.questions} answers needed no school data.")
    if nudges_sent:
        verb = "was" if nudges_followed == 1 else "were"
        sentences.append(
            f"{nudges_followed} of {nudges_sent} evening nudges {verb} followed by a question within 30 minutes."
        )
    note = None
    if before is not None and now.cost_per_family_month_usd is not None:
        verdict = change(
            now.cost_per_family_month_usd,
            before.cost_per_family_month_usd,
            now.questions,
            before.questions,
            show=usd,
        )
        if verdict != NOT_ENOUGH:
            note = f"Cost per family compared with the {days} days before: {verdict}."
    return Sentence(" ".join(sentences), note=note, alert=now.failures > 0)
