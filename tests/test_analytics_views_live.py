"""The dashboard views against real BigQuery, compared with the same numbers computed in Python (Spec 12).

Opt-in, like the live store tests:  EASYTOP_LIVE_BQ=1 uv run pytest tests/test_analytics_views_live.py -q
It needs `gcloud`/`bq` logged in to the project and it REPLACES `easytop_analytics_dev.synthetic_events` (made-up
data only) and recreates the views there. What it loads is the standard demo dataset: seed 1, 12 families, 60 days
up to now, so the dev dashboard keeps working afterwards.
"""

import json
import os
import shutil
import statistics
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tools.analytics.synthetic import ISRAEL, generate, to_ndjson

PROJECT = "your-gcp-project"
DATASET = "easytop_analytics_dev"
ROOT = Path(__file__).resolve().parent.parent
FAILURES = ("error", "truncated", "tool_limit", "session_expired")

pytestmark = pytest.mark.skipif(
    os.environ.get("EASYTOP_LIVE_BQ") != "1" or shutil.which("bq") is None,
    reason="live BigQuery test: set EASYTOP_LIVE_BQ=1 with `bq` logged in to your-gcp-project "
    "(it replaces the SYNTHETIC table in easytop_analytics_dev)",
)


# ---------------------------------------------------------------- BigQuery, through the bq command line


def bq_rows(sql: str) -> list[dict]:
    done = subprocess.run(
        [
            "bq",
            "--project_id",
            PROJECT,
            "query",
            "--use_legacy_sql=false",
            "--quiet",
            "--format=json",
            "--max_rows=100000",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout) if done.stdout.strip() else []


def view_rows(name: str) -> list[dict]:
    return bq_rows(
        f"SELECT * FROM `{PROJECT}.{DATASET}.{name}` WHERE env = 'dev' AND synthetic = TRUE"
    )


def num(v):
    return None if v is None else float(v)


def whole(v):
    return None if v is None else int(v)


def israel_today() -> date:
    return datetime.now(UTC).astimezone(ISRAEL).date()


# ---------------------------------------------------------------- the same definitions, in Python


def when(e) -> datetime:
    return datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))


def day_of(e) -> date:
    return when(e).astimezone(ISRAEL).date()


def week_of(d: date) -> date:
    return d - timedelta(days=d.isoweekday() % 7)  # the Sunday that starts the week


def school_evening(d: date) -> bool:
    return d.isoweekday() % 7 <= 4  # Sunday (0) to Thursday (4)


def asked(events):
    return [e for e in events if e["event"] == "question" and e["outcome"] != "rate_limited"]


def periods(events, today):
    first_day = min(day_of(q) for q in asked(events))
    out = {}
    for n in (7, 28, 90):
        for ago in (0, 1):
            end = today - timedelta(days=1 + ago * n)
            start = end - timedelta(days=n - 1)
            window_start = max(start, first_day)
            days = max((end - window_start).days + 1, 0)
            out[(n, ago)] = {
                "start": start,
                "end": end,
                "window_start": window_start,
                "window_days": days,
                "school_evenings": sum(
                    school_evening(window_start + timedelta(days=i)) for i in range(days)
                ),
            }
    return out


def in_window(events, p):
    return [q for q in asked(events) if p["window_start"] <= day_of(q) <= p["end"]]


def p95(values):
    ordered = sorted(values)
    return ordered[-(-95 * len(ordered) // 100) - 1] if ordered else None


def median_or_none(values):
    return statistics.median(values) if values else None


@pytest.fixture(scope="module")
def world():
    """Load the demo dataset, recreate the views, and keep the events for the Python side."""
    before = israel_today()
    events = generate(families=12, days=60, seed=1, end=datetime.now(UTC))
    ndjson = Path(os.environ.get("TMPDIR", "/tmp")) / "easytop_live_synthetic.ndjson"
    ndjson.write_text(to_ndjson(events), encoding="utf-8")
    subprocess.run(
        [
            "bq",
            "--project_id",
            PROJECT,
            "load",
            "--replace",
            "--source_format=NEWLINE_DELIMITED_JSON",
            f"{PROJECT}:{DATASET}.synthetic_events",
            str(ndjson),
            "payload:STRING",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [str(ROOT / "tools/ops/analytics_views.sh"), PROJECT, "dev"],
        check=True,
        capture_output=True,
    )
    if israel_today() != before:
        pytest.skip("the Israel date changed during the load: run again")
    return {"events": events, "today": before}


@pytest.fixture
def events(world):
    return world["events"]


@pytest.fixture
def today(world):
    t = world["today"]
    if israel_today() != t:
        pytest.skip("the Israel date changed during the test: run again")
    return t


def by_period(rows):
    return {(whole(r["period_days"]), whole(r["periods_ago"])): r for r in rows}


# ---------------------------------------------------------------- one test per view


def test_periods(events, today):
    got = by_period(view_rows("periods"))
    want = periods(events, today)
    assert set(got) == set(want)
    for key, p in want.items():
        r = got[key]
        assert r["period_start"] == p["start"].isoformat()
        assert r["period_end"] == p["end"].isoformat()
        assert r["window_start"] == p["window_start"].isoformat()
        assert whole(r["window_days"]) == p["window_days"]
        assert whole(r["school_evenings"]) == p["school_evenings"]


def test_tomorrow_coverage(events, today):
    got = by_period(view_rows("tomorrow_coverage"))
    for key, p in periods(events, today).items():
        qs = in_window(events, p)
        families = {q["parent_ref"] for q in qs}
        pairs = {
            (q["parent_ref"], day_of(q))
            for q in qs
            if q.get("category") == "tomorrow" and school_evening(day_of(q))
        }
        r = got[key]
        assert whole(r["school_evenings"]) == p["school_evenings"], key
        assert whole(r["active_families"]) == len(families), key
        assert whole(r["tomorrow_pairs"]) == len(pairs), key
        denominator = len(families) * p["school_evenings"]
        if denominator:
            assert num(r["coverage"]) == pytest.approx(len(pairs) / denominator), key
        else:
            assert r["coverage"] is None, key
    live = got[(28, 0)]
    assert whole(live["tomorrow_pairs"]) > 0, "the demo data should give a real coverage number"


def test_habit_weekly(events, today):
    per_family_week = {}
    for q in asked(events):
        if day_of(q) < today:
            per_family_week.setdefault((week_of(day_of(q)), q["parent_ref"]), set()).add(day_of(q))
    want = {}
    for (week, _), days in per_family_week.items():
        row = want.setdefault(week, [0, 0, 0, 0, 0, 0])
        row[0] += 1
        row[min(len(days), 5)] += 1
    got = {r["week"]: r for r in view_rows("habit_weekly")}
    assert set(got) == {w.isoformat() for w in want}
    for week, counts in want.items():
        r = got[week.isoformat()]
        assert [
            whole(r[c])
            for c in ("active_families", "days_1", "days_2", "days_3", "days_4", "days_5plus")
        ] == counts, week


def test_week2_return(events, today):
    firsts = {}
    for q in sorted(events, key=when):
        if q["event"] == "question" and q["outcome"] == "answered":
            firsts.setdefault(q["parent_ref"], day_of(q))
    matured = {f: d for f, d in firsts.items() if d <= today - timedelta(days=14)}
    returned = [
        f
        for f, first in matured.items()
        if any(
            first + timedelta(days=7) <= day_of(q) <= first + timedelta(days=13)
            for q in asked(events)
            if q["parent_ref"] == f
        )
    ]
    rows = view_rows("week2_return")
    assert len(rows) == 1
    assert (whole(rows[0]["families"]), whole(rows[0]["returned"])) == (len(matured), len(returned))
    assert 0 < len(returned) < len(matured), "the demo data should have both kinds of family"


def test_time_to_first_answer(events):
    minutes = []
    for ref in {e["parent_ref"] for e in events if "parent_ref" in e}:
        mine = [e for e in events if e.get("parent_ref") == ref]
        offered = [when(e) for e in mine if e["event"] == "connect_offered"]
        answered = [
            when(e) for e in mine if e["event"] == "question" and e["outcome"] == "answered"
        ]
        if offered and answered and min(answered) >= min(offered):
            minutes.append((min(answered) - min(offered)).total_seconds() / 60)
    rows = view_rows("time_to_first_answer")
    assert len(rows) == 1
    assert whole(rows[0]["families"]) == len(minutes)
    assert num(rows[0]["median_minutes"]) == pytest.approx(statistics.median(minutes), abs=0.051)


def test_reconnects_weekly(events, today):
    active, reconnects = {}, {}
    for q in asked(events):
        if day_of(q) < today:
            active.setdefault(week_of(day_of(q)), set()).add(q["parent_ref"])
    for e in events:
        if e["event"] == "connect_offered" and e["reason"] == "expired" and day_of(e) < today:
            reconnects[week_of(day_of(e))] = reconnects.get(week_of(day_of(e)), 0) + 1
    got = {r["week"]: r for r in view_rows("reconnects_weekly")}
    assert set(got) == {w.isoformat() for w in set(active) | set(reconnects)}
    for week in set(active) | set(reconnects):
        r = got[week.isoformat()]
        n_active, n_reconnects = len(active.get(week, ())), reconnects.get(week, 0)
        assert (whole(r["active_families"]), whole(r["reconnects"])) == (n_active, n_reconnects)
        if n_active:
            assert num(r["reconnects_per_family"]) == pytest.approx(n_reconnects / n_active)
        else:
            assert r["reconnects_per_family"] is None
    assert sum(reconnects.values()) > 0, "the demo data should contain reconnects"


def test_worth_it(events, today):
    got = by_period(view_rows("worth_it"))
    for key, p in periods(events, today).items():
        qs = in_window(events, p)
        families = {q["parent_ref"] for q in qs}
        cost = sum(q.get("cost_usd", 0) for q in qs)
        times = [q["answer_ms"] for q in qs if "answer_ms" in q]
        failures = sum(q["outcome"] in FAILURES for q in qs)
        no_data = sum(q["outcome"] == "answered_no_data" for q in qs)
        r = got[key]
        assert whole(r["questions"]) == len(qs), key
        assert whole(r["active_families"]) == len(families), key
        assert num(r["cost_usd"]) == pytest.approx(cost, abs=6e-5), key
        assert whole(r["failures"]) == failures and whole(r["no_data"]) == no_data, key
        if families and p["window_days"]:
            want = cost / len(families) * 30 / p["window_days"]
            assert num(r["cost_per_family_month_usd"]) == pytest.approx(want, abs=6e-5), key
        else:
            assert r["cost_per_family_month_usd"] is None, key
        if times:
            assert num(r["p95_seconds"]) == pytest.approx(p95(times) / 1000), key
        else:
            assert r["p95_seconds"] is None, key
        if qs:
            assert num(r["failure_share"]) == pytest.approx(failures / len(qs)), key
            assert num(r["no_data_share"]) == pytest.approx(no_data / len(qs)), key
        else:
            assert r["failure_share"] is None and r["no_data_share"] is None, key
    assert whole(got[(28, 0)]["questions"]) >= 20, "the demo data should have enough questions"


def test_nudge_conversion(events):
    questions = asked(events)
    want = {}
    for n in (e for e in events if e["event"] == "nudge" and e["result"] == "sent"):
        followed = any(
            q["parent_ref"] == n["parent_ref"]
            and when(n) < when(q) <= when(n) + timedelta(minutes=30)
            for q in questions
        )
        sent, hit = want.get(day_of(n), (0, 0))
        want[day_of(n)] = (sent + 1, hit + followed)
    got = {
        r["day"]: (whole(r["sent"]), whole(r["followed"])) for r in view_rows("nudge_conversion")
    }
    assert got == {d.isoformat(): v for d, v in want.items()}
    assert sum(s for s, _ in want.values()) > 0 and sum(f for _, f in want.values()) > 0
    assert sum(f for _, f in want.values()) < sum(s for s, _ in want.values())


def lifetimes(events):
    """(route, state, hours) per login, by the rule of docs/analytics/60_session_lifetime.sql."""
    out = []
    for ref in {e["parent_ref"] for e in events if "parent_ref" in e}:
        mine = sorted((e for e in events if e.get("parent_ref") == ref), key=when)
        logins = [e for e in mine if e["event"] == "login_captured"]
        for i, login in enumerate(logins):
            until = logins[i + 1]["ts"] if i + 1 < len(logins) else None
            later = [
                e
                for e in mine
                if e["event"] in ("session_check", "session_expired")
                and e["ts"] > login["ts"]
                and (until is None or e["ts"] < until)
            ]
            dead = [
                e["age_h"] for e in later if e["event"] == "session_check" and e["status"] == "dead"
            ]
            expired = [
                (when(e) - when(login)).total_seconds() / 3600
                for e in later
                if e["event"] == "session_expired"
            ]
            ok = [
                e["age_h"] for e in later if e["event"] == "session_check" and e["status"] == "ok"
            ]
            hours = min(dead) if dead else min(expired) if expired else max(ok) if ok else 0
            out.append((login["route"], "ended" if dead or expired else "alive", round(hours, 1)))
    return out


def test_session_lifetime_by_route(events):
    groups = {}
    for route, state, hours in lifetimes(events):
        groups.setdefault((route, state), []).append(hours)
    got = {(r["route"], r["state"]): r for r in view_rows("session_lifetime_by_route")}
    assert set(got) == set(groups)
    for key, hours in groups.items():
        r = got[key]
        assert whole(r["logins"]) == len(hours), key
        assert num(r["median_hours"]) == pytest.approx(statistics.median(hours), abs=0.11), key
        assert num(r["max_hours"]) == pytest.approx(max(hours), abs=0.11), key


def test_funnel_and_outcomes_keep_their_shape():
    assert {r["step"] for r in view_rows("funnel")} >= {"1 invite created", "7 first question"}
    assert {r["outcome"] for r in view_rows("outcomes_daily")} >= {"answered"}


def test_no_view_the_app_reads_has_a_parent_column():
    from tools.dashboard.queries import APP_VIEWS

    for name in APP_VIEWS:
        done = subprocess.run(
            [
                "bq",
                "--project_id",
                PROJECT,
                "show",
                "--schema",
                "--format=json",
                f"{DATASET}.{name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        columns = [c["name"].lower() for c in json.loads(done.stdout)]
        assert not any("parent" in c for c in columns), (name, columns)
