"""The five queries the analytics page sends to BigQuery, one per section (Spec 12).

Rules, each checked by tests/dashboard/test_dashboard_queries.py:
- values reach BigQuery only as query parameters (@start, @end, @env, @synthetic), never inside the SQL text; the
  only text that varies is the dataset name, which comes from the two allowed environments;
- the queries read the aggregate views only (APP_VIEWS): counts, shares, medians, never a pseudonym;
- each query is one job, so the grey line under a section can show that job's own bytes and time.

The period is [@start, @end], the last N complete days (the `periods` view uses the same days); the views also hold
the N days before it, which is what "compared with the previous period" reads.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from tools.dashboard.insights import (
    PeriodNumbers,
    ReconnectWeek,
    RouteLifetime,
    Step,
    Week2,
    WeekNumbers,
    WorthNumbers,
)

SECTIONS = ("headline", "habit", "activation", "friction", "worth_it")
ENVIRONMENTS = ("prod", "dev")
DEFAULT_PROJECT = "your-gcp-project"
PARAM_TYPES = {"start": "DATE", "end": "DATE", "env": "STRING", "synthetic": "BOOL"}

# Views the page may read. None of them has a parent column (tests/test_analytics_views.py checks the SQL files and
# tests/test_analytics_views_live.py checks the real schemas).
APP_VIEWS = frozenset(
    {
        "tomorrow_coverage",
        "habit_weekly",
        "week2_return",
        "time_to_first_answer",
        "reconnects_weekly",
        "worth_it",
        "nudge_conversion",
        "session_lifetime_by_route",
        "funnel",
        "outcomes_daily",
    }
)

# {ds} is "<project>.easytop_analytics_<env>": an identifier built from the allowed environments only.
SQL = {
    "headline": """
SELECT
  c.periods_ago, c.period_start, c.period_end, c.school_evenings, c.active_families, c.tomorrow_pairs,
  c.coverage, w.questions, w.cost_per_family_month_usd
FROM `{ds}.tomorrow_coverage` c
JOIN `{ds}.worth_it` w
  ON w.env = c.env AND w.synthetic = c.synthetic
  AND w.period_days = c.period_days AND w.periods_ago = c.periods_ago
WHERE c.env = @env AND c.synthetic = @synthetic
  AND c.period_days = DATE_DIFF(@end, @start, DAY) + 1
  AND c.period_end IN (@end, DATE_SUB(@start, INTERVAL 1 DAY))
ORDER BY c.periods_ago
""",
    "habit": """
SELECT 'weekly' AS part, h.week, h.active_families, h.days_1, h.days_2, h.days_3, h.days_4, h.days_5plus,
  CAST(NULL AS INT64) AS families, CAST(NULL AS INT64) AS returned
FROM `{ds}.habit_weekly` h
WHERE h.env = @env AND h.synthetic = @synthetic
  AND h.week BETWEEN DATE_TRUNC(@start, WEEK(SUNDAY)) AND @end
UNION ALL
SELECT 'week2', CAST(NULL AS DATE), CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64),
  CAST(NULL AS INT64), CAST(NULL AS INT64), CAST(NULL AS INT64), r.families, r.returned
FROM `{ds}.week2_return` r
WHERE r.env = @env AND r.synthetic = @synthetic
ORDER BY part, week
""",
    "activation": """
SELECT f.step, f.n, t.families AS timed_families, t.median_minutes
FROM `{ds}.funnel` f
LEFT JOIN `{ds}.time_to_first_answer` t ON t.env = f.env AND t.synthetic = f.synthetic
WHERE f.env = @env AND f.synthetic = @synthetic
ORDER BY f.step
""",
    "friction": """
SELECT 'weekly' AS part, r.week, r.active_families, r.reconnects,
  CAST(NULL AS STRING) AS route, CAST(NULL AS STRING) AS state, CAST(NULL AS INT64) AS logins,
  CAST(NULL AS FLOAT64) AS median_hours, CAST(NULL AS FLOAT64) AS max_hours
FROM `{ds}.reconnects_weekly` r
WHERE r.env = @env AND r.synthetic = @synthetic
  AND r.week BETWEEN DATE_TRUNC(@start, WEEK(SUNDAY)) AND @end
UNION ALL
SELECT 'lifetime', CAST(NULL AS DATE), CAST(NULL AS INT64), CAST(NULL AS INT64),
  l.route, l.state, l.logins, l.median_hours, l.max_hours
FROM `{ds}.session_lifetime_by_route` l
WHERE l.env = @env AND l.synthetic = @synthetic
ORDER BY part, week
""",
    "worth_it": """
SELECT 'tiles' AS part, w.periods_ago, w.period_start, w.period_end, w.questions, w.active_families,
  w.cost_per_family_month_usd, w.p95_seconds, w.failures, w.failure_share, w.no_data, w.no_data_share,
  (SELECT IFNULL(SUM(n.sent), 0) FROM `{ds}.nudge_conversion` n
    WHERE n.env = w.env AND n.synthetic = w.synthetic AND n.day BETWEEN w.period_start AND w.period_end
  ) AS nudges_sent,
  (SELECT IFNULL(SUM(n.followed), 0) FROM `{ds}.nudge_conversion` n
    WHERE n.env = w.env AND n.synthetic = w.synthetic AND n.day BETWEEN w.period_start AND w.period_end
  ) AS nudges_followed,
  CAST(NULL AS DATE) AS week, CAST(NULL AS STRING) AS outcome, CAST(NULL AS INT64) AS outcome_questions
FROM `{ds}.worth_it` w
WHERE w.env = @env AND w.synthetic = @synthetic
  AND w.period_days = DATE_DIFF(@end, @start, DAY) + 1
  AND w.period_end IN (@end, DATE_SUB(@start, INTERVAL 1 DAY))
UNION ALL
SELECT 'outcomes', CAST(NULL AS INT64), CAST(NULL AS DATE), CAST(NULL AS DATE), CAST(NULL AS INT64),
  CAST(NULL AS INT64), CAST(NULL AS FLOAT64), CAST(NULL AS FLOAT64), CAST(NULL AS INT64),
  CAST(NULL AS FLOAT64), CAST(NULL AS INT64), CAST(NULL AS FLOAT64), CAST(NULL AS INT64),
  CAST(NULL AS INT64), DATE_TRUNC(o.day, WEEK(SUNDAY)), o.outcome, SUM(o.questions)
FROM `{ds}.outcomes_daily` o
WHERE o.env = @env AND o.synthetic = @synthetic AND o.day BETWEEN @start AND @end
GROUP BY DATE_TRUNC(o.day, WEEK(SUNDAY)), o.outcome
ORDER BY part, week, outcome
""",
}


@dataclass(frozen=True)
class Params:
    start: date
    end: date
    env: str
    synthetic: bool

    def __post_init__(self):
        if self.env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "env": self.env, "synthetic": self.synthetic}


@dataclass(frozen=True)
class Stats:
    """What the job itself reports, for the grey line under a section."""

    bytes_scanned: int | None = None
    seconds: float | None = None
    cache_hit: bool = False
    source: str = "bigquery"  # bigquery | cache (served by the page's own cache) | snapshot

    def line(self) -> str:
        if self.source == "snapshot":
            return "from the saved snapshot"
        if self.cache_hit or self.source == "cache":
            return "from cache"
        if self.bytes_scanned is None or self.seconds is None:
            return ""
        return f"scanned {_size(self.bytes_scanned)} in {self.seconds:.1f} s"


@dataclass(frozen=True)
class Result:
    rows: list[dict[str, Any]]
    stats: Stats


def _size(n: int) -> str:
    if n < 1000:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def dataset_for(project: str, env: str) -> str:
    if env not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {ENVIRONMENTS}")
    return f"{project}.easytop_analytics_{env}"


def render(section: str, project: str, env: str) -> str:
    return SQL[section].replace("{ds}", dataset_for(project, env)).strip()


def job_config(params: Params):
    """The query parameters as BigQuery objects (imported here so the rest of this module needs no Google library)."""
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, PARAM_TYPES[name], value)
            for name, value in params.as_dict().items()
        ]
    )


def run(client, section: str, params: Params, project: str = DEFAULT_PROJECT) -> Result:
    """Send one section's query; the rows are plain dicts, the stats are the job's own."""
    began = time.perf_counter()
    job = client.query(render(section, project, params.env), job_config=job_config(params))
    rows = [dict(row.items()) for row in job.result()]
    if getattr(job, "started", None) and getattr(job, "ended", None):
        seconds = (job.ended - job.started).total_seconds()
    else:
        seconds = time.perf_counter() - began
    return Result(
        rows, Stats(job.total_bytes_processed, seconds, bool(getattr(job, "cache_hit", False)))
    )


# ---------------------------------------------------------------- result rows -> the numbers the sentences use


def _i(value) -> int:
    return int(value or 0)


def _f(value) -> float | None:
    return None if value is None else float(value)


def parse_headline(
    rows: Sequence[dict],
) -> tuple[PeriodNumbers | None, PeriodNumbers | None, date | None, date | None]:
    periods: dict[int, PeriodNumbers] = {}
    start = end = None
    for r in rows:
        periods[_i(r["periods_ago"])] = PeriodNumbers(
            active_families=_i(r["active_families"]),
            school_evenings=_i(r["school_evenings"]),
            tomorrow_pairs=_i(r["tomorrow_pairs"]),
            coverage=_f(r["coverage"]),
            questions=_i(r["questions"]),
            cost_per_family_month_usd=_f(r["cost_per_family_month_usd"]),
        )
        if _i(r["periods_ago"]) == 0:
            start, end = r["period_start"], r["period_end"]
    return periods.get(0), periods.get(1), start, end


def parse_habit(rows: Sequence[dict]) -> tuple[list[WeekNumbers], Week2 | None]:
    weeks, week2 = [], None
    for r in rows:
        if r["part"] == "weekly":
            weeks.append(
                WeekNumbers(
                    week=r["week"],
                    active_families=_i(r["active_families"]),
                    days_hist=(
                        _i(r["days_1"]),
                        _i(r["days_2"]),
                        _i(r["days_3"]),
                        _i(r["days_4"]),
                        _i(r["days_5plus"]),
                    ),
                )
            )
        elif r["part"] == "week2":
            week2 = Week2(families=_i(r["families"]), returned=_i(r["returned"]))
    return sorted(weeks, key=lambda w: w.week), week2


def parse_activation(rows: Sequence[dict]) -> tuple[list[Step], float | None, int]:
    steps = [
        Step(order=int(r["step"].split(" ", 1)[0]), label=r["step"], n=_i(r["n"])) for r in rows
    ]
    median = timed = None
    for r in rows:
        if r.get("median_minutes") is not None:
            median, timed = float(r["median_minutes"]), _i(r["timed_families"])
    return sorted(steps, key=lambda s: s.order), median, timed or 0


def parse_friction(rows: Sequence[dict]) -> tuple[list[ReconnectWeek], list[RouteLifetime]]:
    weeks, routes = [], []
    for r in rows:
        if r["part"] == "weekly":
            weeks.append(ReconnectWeek(r["week"], _i(r["active_families"]), _i(r["reconnects"])))
        elif r["part"] == "lifetime":
            routes.append(
                RouteLifetime(
                    route=r["route"],
                    state=r["state"],
                    logins=_i(r["logins"]),
                    median_hours=float(r["median_hours"]),
                    max_hours=float(r["max_hours"]),
                )
            )
    return sorted(weeks, key=lambda w: w.week), routes


@dataclass(frozen=True)
class WorthParsed:
    now: WorthNumbers | None
    before: WorthNumbers | None
    nudges_sent: int
    nudges_followed: int
    failure_share: float | None
    no_data_share: float | None
    outcomes: list[tuple[date, str, int]]


def parse_worth_it(rows: Sequence[dict]) -> WorthParsed:
    tiles: dict[int, WorthNumbers] = {}
    sent = followed = 0
    failure_share = no_data_share = None
    outcomes = []
    for r in rows:
        if r["part"] == "tiles":
            ago = _i(r["periods_ago"])
            tiles[ago] = WorthNumbers(
                questions=_i(r["questions"]),
                active_families=_i(r["active_families"]),
                cost_per_family_month_usd=_f(r["cost_per_family_month_usd"]),
                p95_seconds=_f(r["p95_seconds"]),
                failures=_i(r["failures"]),
                no_data=_i(r["no_data"]),
            )
            if ago == 0:
                sent, followed = _i(r["nudges_sent"]), _i(r["nudges_followed"])
                failure_share, no_data_share = _f(r["failure_share"]), _f(r["no_data_share"])
        elif r["part"] == "outcomes":
            outcomes.append((r["week"], r["outcome"], _i(r["outcome_questions"])))
    return WorthParsed(
        tiles.get(0), tiles.get(1), sent, followed, failure_share, no_data_share, outcomes
    )
