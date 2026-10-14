"""The two places the page gets numbers from: BigQuery (tested with a fake client in test_dashboard_queries.py) and
the saved CSV snapshot."""

from datetime import date, timedelta

import pytest

from tools.dashboard import queries
from tools.dashboard.sources import (
    SNAPSHOT_DIR,
    NoDataYet,
    SnapshotSource,
    login_problem,
    read_csv,
    write_csv,
)


def params(days=28, env="dev", synthetic=True):
    end = date(2026, 10, 10)
    return queries.Params(end - timedelta(days=days - 1), end, env, synthetic)


def test_csv_round_trip_keeps_dates_numbers_and_missing_values(tmp_path):
    rows = [
        {"part": "weekly", "week": date(2026, 10, 4), "n": 3, "share": 0.25, "note": None},
        {"part": "week2", "week": None, "n": 0, "share": 1.0, "note": "1 invite created"},
    ]
    write_csv(tmp_path / "x.csv", rows)
    assert read_csv(tmp_path / "x.csv") == rows


def test_an_empty_result_round_trips_as_no_rows(tmp_path):
    write_csv(tmp_path / "x.csv", [])
    assert read_csv(tmp_path / "x.csv") == []


@pytest.mark.parametrize("section", queries.SECTIONS)
@pytest.mark.parametrize("days", [7, 28, 90])
def test_the_committed_snapshot_has_every_section_for_every_period(section, days):
    result = SnapshotSource().load(section, params(days))
    assert result.stats.line() == "from the saved snapshot"
    assert result.rows, f"{section}_{days}.csv is empty"


def test_the_snapshot_picks_the_file_by_period_length_and_ignores_the_dates():
    source = SnapshotSource()
    short = source.load("headline", params(7)).rows
    long = source.load("headline", params(28)).rows
    assert short != long
    odd = queries.Params(date(2020, 1, 1), date(2020, 1, 7), "dev", True)
    assert source.load("headline", odd).rows == short


@pytest.mark.parametrize(("env", "synthetic"), [("prod", False), ("prod", True), ("dev", False)])
def test_the_snapshot_answers_for_synthetic_dev_data_only(env, synthetic):
    with pytest.raises(NoDataYet):
        SnapshotSource().load("headline", params(7, env, synthetic))


def test_a_snapshot_directory_without_the_file_says_how_to_make_it(tmp_path):
    with pytest.raises(NoDataYet) as caught:
        SnapshotSource(tmp_path).load("headline", params(7))
    assert "make_snapshot" in str(caught.value)


def test_the_snapshot_holds_aggregates_only_and_says_it_is_synthetic():
    meta = SnapshotSource().meta()
    assert "SYNTHETIC" in meta["note"]
    for path in SNAPSHOT_DIR.glob("*.csv"):
        header = path.read_text().splitlines()[0].lower()
        assert "parent" not in header, path.name


class Refresh(Exception):
    pass


class Forbidden(Exception):
    pass


class DefaultCredentialsError(Exception):
    pass


class Surprise(Exception):
    pass


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (DefaultCredentialsError(), "login"),
        (type("RefreshError", (Exception,), {})(), "login"),
        (type("ReauthRequired", (Exception,), {})(), "login"),
        (Forbidden(), "permission"),
        (type("PermissionDenied", (Exception,), {})(), "permission"),
        (Surprise(), None),
    ],
)
def test_google_login_problems_are_told_apart_from_other_failures(error, kind):
    assert login_problem(error) == kind
