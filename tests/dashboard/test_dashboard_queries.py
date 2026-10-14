"""The five queries the page sends (Spec 12 "Efficient aggregation"): parameters only, aggregate views only."""

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from tools.dashboard import queries as q

bigquery = pytest.importorskip("google.cloud.bigquery")

PARAMS = q.Params(start=date(2026, 9, 14), end=date(2026, 10, 10), env="dev", synthetic=True)
ALLOWED_LITERALS = {
    "weekly",
    "week2",
    "lifetime",
    "tiles",
    "outcomes",
}  # labels that split a result in two


class FakeRow(dict):
    pass


class FakeJob:
    def __init__(self, rows, *, bytes_processed=41_000, cache_hit=False, seconds=0.6):
        self._rows = rows
        self.total_bytes_processed = bytes_processed
        self.cache_hit = cache_hit
        self.started = datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC)
        self.ended = self.started + timedelta(seconds=seconds)

    def result(self):
        return [FakeRow(r) for r in self._rows]


class FakeClient:
    def __init__(self, rows=None, **job):
        self.rows, self.job, self.sent = rows or [], job, []

    def query(self, sql, job_config=None):
        self.sent.append((sql, job_config))
        return FakeJob(self.rows, **self.job)


def test_there_are_five_sections_and_one_query_each():
    assert q.SECTIONS == ("headline", "habit", "activation", "friction", "worth_it")
    for section in q.SECTIONS:
        client = FakeClient()
        q.run(client, section, PARAMS)
        assert len(client.sent) == 1


@pytest.mark.parametrize("section", q.SECTIONS)
def test_every_query_binds_all_four_parameters_and_builds_no_values_into_the_text(section):
    client = FakeClient()
    q.run(client, section, PARAMS)
    sql, config = client.sent[0]
    used = set(re.findall(r"@(\w+)", sql))
    bound = {p.name: p for p in config.query_parameters}
    assert used <= set(bound) == {"start", "end", "env", "synthetic"}
    assert {"env", "synthetic"} <= used, (
        "every section is filtered by environment and the synthetic flag"
    )
    assert (bound["start"].type_, bound["start"].value) == ("DATE", date(2026, 9, 14))
    assert (bound["end"].type_, bound["end"].value) == ("DATE", date(2026, 10, 10))
    assert (bound["env"].type_, bound["env"].value) == ("STRING", "dev")
    assert (bound["synthetic"].type_, bound["synthetic"].value) == ("BOOL", True)
    # the text carries no environment name, no date and no flag: only identifiers and fixed labels
    literals = set(re.findall(r"'([^']*)'", sql))
    assert literals <= ALLOWED_LITERALS, literals
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", sql)
    assert "{ds}" not in sql


@pytest.mark.parametrize("section", q.SECTIONS)
def test_every_query_reads_only_the_aggregate_views_of_the_chosen_environment(section):
    for env in q.ENVIRONMENTS:
        sql = q.render(section, "your-gcp-project", env)
        named = re.findall(r"`([^`]+)`", sql)
        assert named, section
        for ref in named:
            project, dataset, view = ref.split(".")
            assert (project, dataset) == ("your-gcp-project", f"easytop_analytics_{env}")
            assert view in q.APP_VIEWS, (section, view)


def test_the_views_the_app_may_read_never_include_a_per_family_or_per_event_view():
    assert not {"events", "events_raw", "session_lifetime", "periods"} & set(q.APP_VIEWS)


def test_an_environment_outside_the_list_cannot_reach_the_dataset_name():
    with pytest.raises(ValueError):
        q.Params(start=date(2026, 1, 1), end=date(2026, 1, 7), env="dev`; DROP", synthetic=False)


def test_the_period_length_is_the_inclusive_day_count():
    assert PARAMS.days == 27
    assert q.Params(date(2026, 10, 4), date(2026, 10, 10), "prod", False).days == 7


def test_the_stats_come_from_the_job_itself():
    result = q.run(FakeClient([{"x": 1}], bytes_processed=41_000, seconds=0.6), "habit", PARAMS)
    assert result.rows == [{"x": 1}]
    assert result.stats.bytes_scanned == 41_000
    assert result.stats.seconds == pytest.approx(0.6)
    assert result.stats.cache_hit is False
    assert q.run(FakeClient(cache_hit=True, bytes_processed=0), "habit", PARAMS).stats.cache_hit


def test_the_stats_line_is_grey_text_in_kb_or_from_cache():
    scanned = q.Stats(bytes_scanned=41_000, seconds=0.6)
    assert scanned.line() == "scanned 40 KB in 0.6 s"
    assert q.Stats(bytes_scanned=0, seconds=0.2, cache_hit=True).line() == "from cache"
    assert q.Stats(bytes_scanned=3_200_000, seconds=1.4).line() == "scanned 3.1 MB in 1.4 s"
    assert q.Stats(bytes_scanned=500, seconds=0.3).line() == "scanned 500 bytes in 0.3 s"
    assert q.Stats(source="snapshot").line() == "from the saved snapshot"
    assert q.Stats(source="cache", bytes_scanned=41_000, seconds=0.6).line() == "from cache"


# ---------------------------------------------------------------- rows to the numbers the sentences use


def test_parse_headline_pairs_the_two_periods_by_periods_ago():
    rows = [
        dict(
            periods_ago=1,
            period_start=date(2026, 9, 7),
            period_end=date(2026, 9, 13),
            school_evenings=5,
            active_families=1,
            tomorrow_pairs=2,
            coverage=0.4,
            questions=12,
            cost_per_family_month_usd=0.3,
        ),
        dict(
            periods_ago=0,
            period_start=date(2026, 9, 14),
            period_end=date(2026, 9, 20),
            school_evenings=5,
            active_families=2,
            tomorrow_pairs=7,
            coverage=0.7,
            questions=30,
            cost_per_family_month_usd=0.4,
        ),
    ]
    now, before, start, end = q.parse_headline(rows)
    assert (now.active_families, now.tomorrow_pairs, now.coverage, now.questions) == (2, 7, 0.7, 30)
    assert before.questions == 12
    assert (start, end) == (date(2026, 9, 14), date(2026, 9, 20))
    assert q.parse_headline([]) == (None, None, None, None)


def test_parse_habit_splits_the_weeks_from_the_week_two_row():
    rows = [
        dict(
            part="weekly",
            week=date(2026, 10, 4),
            active_families=3,
            days_1=1,
            days_2=0,
            days_3=2,
            days_4=0,
            days_5plus=0,
            families=None,
            returned=None,
        ),
        dict(
            part="week2",
            week=None,
            active_families=None,
            days_1=None,
            days_2=None,
            days_3=None,
            days_4=None,
            days_5plus=None,
            families=4,
            returned=3,
        ),
    ]
    weeks, week2 = q.parse_habit(rows)
    assert weeks[0].days_hist == (1, 0, 2, 0, 0) and weeks[0].active_families == 3
    assert (week2.families, week2.returned) == (4, 3)
    assert q.parse_habit([]) == ([], None)


def test_parse_activation_returns_steps_in_order_and_the_median():
    rows = [
        dict(step="2 invite redeemed", n=15, timed_families=9, median_minutes=6.2),
        dict(step="1 invite created", n=16, timed_families=9, median_minutes=6.2),
    ]
    steps, median, timed = q.parse_activation(rows)
    assert [s.order for s in steps] == [1, 2] and steps[1].n == 15
    assert (median, timed) == (6.2, 9)
    assert q.parse_activation([]) == ([], None, 0)


def test_parse_friction_splits_weeks_and_routes():
    rows = [
        dict(
            part="weekly",
            week=date(2026, 10, 4),
            active_families=2,
            reconnects=3,
            route=None,
            state=None,
            logins=None,
            median_hours=None,
            max_hours=None,
        ),
        dict(
            part="lifetime",
            week=None,
            active_families=None,
            reconnects=None,
            route="code",
            state="ended",
            logins=34,
            median_hours=19.0,
            max_hours=30.2,
        ),
    ]
    weeks, routes = q.parse_friction(rows)
    assert (weeks[0].active_families, weeks[0].reconnects) == (2, 3)
    assert (routes[0].route, routes[0].logins, routes[0].median_hours) == ("code", 34, 19.0)


def test_parse_worth_it_gives_both_periods_the_nudge_counts_and_the_weekly_outcomes():
    tile = dict(
        part="tiles",
        periods_ago=0,
        period_start=date(2026, 9, 14),
        period_end=date(2026, 10, 10),
        questions=63,
        active_families=2,
        cost_per_family_month_usd=0.4,
        p95_seconds=9.0,
        failures=1,
        failure_share=1 / 63,
        no_data=4,
        no_data_share=4 / 63,
        nudges_sent=8,
        nudges_followed=3,
        week=None,
        outcome=None,
        outcome_questions=None,
    )
    out = dict(
        part="outcomes",
        periods_ago=None,
        period_start=None,
        period_end=None,
        questions=None,
        active_families=None,
        cost_per_family_month_usd=None,
        p95_seconds=None,
        failures=None,
        failure_share=None,
        no_data=None,
        no_data_share=None,
        nudges_sent=None,
        nudges_followed=None,
        week=date(2026, 10, 4),
        outcome="answered",
        outcome_questions=40,
    )
    parsed = q.parse_worth_it([tile, out])
    assert parsed.now.questions == 63 and parsed.before is None
    assert (parsed.nudges_sent, parsed.nudges_followed) == (8, 3)
    assert parsed.outcomes == [(date(2026, 10, 4), "answered", 40)]
    assert q.parse_worth_it([]).now is None


# ---------------------------------------------------------------- BigQuery source


def test_a_dataset_that_does_not_exist_yet_reads_as_no_data_yet():
    from google.api_core.exceptions import NotFound

    from tools.dashboard.sources import BigQuerySource, NoDataYet

    class Missing:
        def query(self, sql, job_config=None):
            raise NotFound("Not found: Dataset your-gcp-project:easytop_analytics_prod")

    source = BigQuerySource(client=Missing())
    with pytest.raises(NoDataYet) as caught:
        source.load("headline", q.Params(date(2026, 10, 4), date(2026, 10, 10), "prod", False))
    assert "prod" in str(caught.value)
