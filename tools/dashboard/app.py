"""Easytop usage page (Spec 12): a headline, four sections, one sentence each. Layout only.

    uv run --extra dashboard streamlit run tools/dashboard/app.py

The numbers come from `sources` (BigQuery with your own Google login, or a saved snapshot), the queries from
`queries`, the sentences from `insights`. Nothing here shows a pseudonym, a per-family row or any text from events.
Offline run on the saved SYNTHETIC snapshot: EASYTOP_DASHBOARD_SOURCE=snapshot.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src"):  # `streamlit run` puts only this folder on the import path
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from easytop import analytics  # noqa: E402
from tools.dashboard import insights, queries  # noqa: E402
from tools.dashboard.sources import (  # noqa: E402
    BigQuerySource,
    NoDataYet,
    SnapshotSource,
    login_problem,
)

ISRAEL = ZoneInfo("Asia/Jerusalem")
PERIODS = (7, 28, 90)
CACHE_SECONDS = 600
OUTCOME_COLORS = {  # neutral shades for normal outcomes, red only for the ones that are real failures
    "answered": "#6b8eb5",
    "answered_no_data": "#a9b6c4",
    "rate_limited": "#d5dbe2",
    "error": "#c0392b",
    "truncated": "#e0675a",
    "tool_limit": "#e88f85",
    "session_expired": "#9e2a20",
}
SOURCE_NAME = os.environ.get("EASYTOP_DASHBOARD_SOURCE", "bigquery")
SNAPSHOT_DIR = os.environ.get("EASYTOP_DASHBOARD_SNAPSHOT_DIR")
CANNOT_TELL = (
    "What this page cannot tell: whether an answer was right (the guardrail evals cover that), whether a follow-up "
    "question means a parent is engaged or confused, and how much time a parent saves compared with opening Webtop. "
    "Coverage counts holidays as school evenings and gives every family the whole period, so it reads a little low "
    "in holiday weeks and for families that joined during the period."
)
NO_PSEUDONYMS = (
    "no text, no names, no ids: parents appear only as a pseudonym, and this page never shows one"
)


def make_source():
    if SOURCE_NAME == "snapshot":
        return SnapshotSource(Path(SNAPSHOT_DIR)) if SNAPSHOT_DIR else SnapshotSource()
    return BigQuerySource()


@st.cache_data(ttl=CACHE_SECONDS, show_spinner="Asking BigQuery...")
def load(source_key: str, section: str, start: date, end: date, env: str, synthetic: bool):
    """One query per section, kept for 10 minutes. Returns the rows, the job stats and when it was fetched."""
    result = make_source().load(section, queries.Params(start, end, env, synthetic))
    return result, time.time()


def fetch(section: str, params: queries.Params) -> tuple[list[dict], queries.Stats]:
    key = f"{SOURCE_NAME}:{SNAPSHOT_DIR or ''}"
    result, fetched_at = load(key, section, params.start, params.end, params.env, params.synthetic)
    stats = result.stats
    if time.time() - fetched_at > 2:  # the page's own cache answered: nothing was scanned this time
        stats = queries.Stats(source="cache" if stats.source != "snapshot" else "snapshot")
    return result.rows, stats


def grey(stats: queries.Stats) -> None:
    if stats.line():
        st.caption(stats.line())


def say(sentence: insights.Sentence) -> None:
    st.markdown(f":red[{sentence.text}]" if sentence.alert else sentence.text)
    if sentence.note:
        st.caption(sentence.note)


def sundays(start: date, end: date) -> list[date]:
    first = start - timedelta(days=start.isoweekday() % 7)
    return [first + timedelta(days=7 * i) for i in range(((end - first).days // 7) + 1)]


# ---------------------------------------------------------------- sections


def headline(params: queries.Params) -> None:
    rows, stats = fetch("headline", params)
    now, before, start, end = queries.parse_headline(rows)
    sentence = insights.headline(params.days, now, before)
    left, right = st.columns([1, 3])
    left.metric(
        "Tomorrow-evening coverage",
        insights.percent(now.coverage) if now and now.coverage is not None else "-",
    )
    with right:
        st.markdown(f"### {sentence.text}")
        if sentence.note:
            st.caption(sentence.note)
    shown_start, shown_end = start or params.start, end or params.end
    st.caption(
        f"Complete days, {shown_start:%d %b} to {shown_end:%d %b %Y}: today is not counted until it is over. "
        "Coverage = of the school evenings (Sunday to Thursday) in the period, on what share did an active "
        "family ask about tomorrow? Holidays count as school evenings in v1, so the number reads a little low "
        "in holiday weeks."
    )
    grey(stats)


def habit_section(params: queries.Params) -> None:
    st.subheader("1. Habit: do they come back?")
    rows, stats = fetch("habit", params)
    weeks, week2 = queries.parse_habit(rows)
    say(insights.habit(weeks, week2))
    if any(w.active_families for w in weeks):
        by_week = {w.week: w.active_families for w in weeks}
        labels = sundays(params.start, params.end)
        active = pd.DataFrame(
            {"Active families": [by_week.get(w, 0) for w in labels]},
            index=[w.isoformat() for w in labels],
        )
        hist = [sum(w.days_hist[i] for w in weeks) for i in range(5)]
        days = pd.DataFrame(
            {"Family-weeks": hist}, index=["1 day", "2 days", "3 days", "4 days", "5+ days"]
        )
        left, right = st.columns(2)
        left.caption("Active families per week (week starts on Sunday)")
        left.bar_chart(active, sort=False)
        right.caption("How many days a family was active in a week")
        right.bar_chart(days, sort=False)
    grey(stats)


def activation_section(params: queries.Params) -> None:
    st.subheader("2. Activation: can a new parent get in?")
    rows, stats = fetch("activation", params)
    steps, median, timed = queries.parse_activation(rows)
    say(insights.activation(steps, median, timed))
    shown = [s for s in steps if s.order <= insights.LAST_STEP]
    if shown and shown[0].n:
        st.caption("Parents reaching each step, since the start (not only this period)")
        st.bar_chart(
            pd.DataFrame(
                {"Parents": [s.n for s in sorted(shown, key=lambda s: s.order)]},
                index=insights.funnel_labels(steps),
            ),
            horizontal=True,
            sort=False,
        )
        st.caption(insights.kept_line(steps))
    grey(stats)


def friction_section(params: queries.Params) -> None:
    st.subheader("3. Friction: what gets in the way?")
    rows, stats = fetch("friction", params)
    weeks, routes = queries.parse_friction(rows)
    say(insights.friction(weeks, routes))
    left, right = st.columns(2)
    if weeks:
        by_week = {w.week: w for w in weeks}
        labels = sundays(params.start, params.end)
        rate = [
            by_week[w].reconnects / by_week[w].active_families
            if w in by_week and by_week[w].active_families
            else 0.0
            for w in labels
        ]
        left.caption("Reconnects per active family, per week")
        left.line_chart(
            pd.DataFrame({"Reconnects per family": rate}, index=[w.isoformat() for w in labels])
        )
    if routes:
        names = {r.route for r in routes}
        table = pd.DataFrame(
            {
                state: {
                    insights.ROUTE_NAMES.get(n, "Other"): next(
                        (r.median_hours for r in routes if r.route == n and r.state == state), 0.0
                    )
                    for n in sorted(names)
                }
                for state in ("ended", "alive")
            }
        ).rename(columns={"ended": "Ended (median hours)", "alive": "Still valid (hours so far)"})
        right.caption("How long a login lives, by sign-in route")
        right.bar_chart(table, stack=False, sort=False)
    grey(stats)


def worth_it_section(params: queries.Params) -> None:
    st.subheader("4. Worth it: fast, reliable and cheap enough?")
    rows, stats = fetch("worth_it", params)
    parsed = queries.parse_worth_it(rows)
    say(
        insights.worth_it(
            params.days, parsed.now, parsed.before, parsed.nudges_sent, parsed.nudges_followed
        )
    )
    now = parsed.now
    if now and now.questions:
        cost, speed, failed, nodata = st.columns(4)
        cost.metric(
            "Cost per family per month",
            insights.usd(now.cost_per_family_month_usd)
            if now.cost_per_family_month_usd is not None
            else "-",
        )
        speed.metric(
            "95% of answers within",
            insights.seconds(now.p95_seconds) if now.p95_seconds is not None else "-",
        )
        failed.metric(
            "Failures",
            f"{now.failures} of {now.questions}",
            delta=f"{insights.percent(now.failures / now.questions)} of questions"
            if now.failures
            else None,
            delta_color="inverse",
        )
        nodata.metric("Needed no school data", f"{now.no_data} of {now.questions}")
    if parsed.outcomes:
        weeks = sundays(params.start, params.end)
        table = pd.DataFrame(0, index=[w.isoformat() for w in weeks], columns=[])
        for week, outcome, n in parsed.outcomes:
            if outcome not in table.columns:
                table[outcome] = 0
            if week.isoformat() in table.index:
                table.loc[week.isoformat(), outcome] += n
        table = table[[o for o in OUTCOME_COLORS if o in table.columns]]
        st.caption("How each question ended, per week (red = a failure)")
        st.bar_chart(table, color=[OUTCOME_COLORS[o] for o in table.columns], sort=False)
    grey(stats)


# ---------------------------------------------------------------- footer


def kind_label(kind: analytics.Kind) -> str:
    match kind.family:
        case "int" | "float":
            return "number"
        case "bool":
            return "yes/no"
        case "list":
            return "list of tool names"
        case _:
            return "one of a fixed list" if kind.values else "exception class name"


def footer() -> None:
    st.divider()
    st.subheader("What we record")
    table = pd.DataFrame(
        [
            {
                "Event": event,
                "What it records": ", ".join(f"{n} ({kind_label(k)})" for n, k in fields.items())
                or "(only that it happened)",
            }
            for event, fields in analytics.SCHEMA.items()
        ]
    )
    st.table(table)
    st.caption(
        "Every event also carries: "
        + ", ".join(analytics.COMMON)
        + f" (schema version {analytics.SCHEMA_VERSION}). "
        "parent_ref is a pseudonym that cannot be turned back into a person without the environment's key."
    )
    st.markdown(f"**{NO_PSEUDONYMS[0].upper() + NO_PSEUDONYMS[1:]}.**")
    st.caption(CANNOT_TELL)


# ---------------------------------------------------------------- page


def sidebar() -> queries.Params:
    st.sidebar.title("Easytop usage")
    on_snapshot = SOURCE_NAME == "snapshot"
    env = st.sidebar.radio(
        "Environment",
        list(queries.ENVIRONMENTS),
        index=1 if on_snapshot else 0,
        help="prod has no data until it is deployed",
    )
    days = st.sidebar.selectbox("Period", PERIODS, index=0, format_func=lambda d: f"Last {d} days")
    synthetic = False
    if env == "dev":
        synthetic = st.sidebar.checkbox(
            "Show the SYNTHETIC demo data instead of real events",
            value=on_snapshot,
            help="Made-up families. Real and made-up numbers are never added together.",
        )
    st.sidebar.caption(
        "Saved snapshot of the synthetic dev data (offline)."
        if on_snapshot
        else "Live from BigQuery, with your own Google login."
    )
    end = datetime.now(ISRAEL).date() - timedelta(days=1)
    return queries.Params(end - timedelta(days=days - 1), end, env, synthetic)


def explain(error: Exception, params: queries.Params) -> None:
    if isinstance(error, NoDataYet):
        if params.env == "prod":
            st.info(
                "No prod data yet. Prod gets a dataset when it is deployed and the analytics setup has run; "
                "choose dev in the sidebar to see the dev data."
            )
        else:
            st.info(str(error))
        return
    problem = login_problem(error)
    if problem == "login":
        st.error(
            "No usable Google login. Run `gcloud auth application-default login` with the project's account, "
            "then reload this page."
        )
    elif problem == "permission":
        st.error(
            "Google refused the request: the logged-in account needs BigQuery access on your-gcp-project."
        )
    else:
        st.error(
            f"The query failed ({type(error).__name__}). The details are in the terminal that runs this page."
        )
        raise error


def main() -> None:
    st.set_page_config(page_title="Easytop usage", layout="wide")
    params = sidebar()
    st.title("Easytop: is it becoming a habit?")
    if params.synthetic:
        st.warning(
            "SYNTHETIC demo data: made-up families run through the real bot code. Not real parents."
        )
    try:
        headline(params)
        habit_section(params)
        activation_section(params)
        friction_section(params)
        worth_it_section(params)
    except Exception as error:  # noqa: BLE001 - explained to the reader, unknown ones re-raised
        explain(error, params)
    footer()


main()
