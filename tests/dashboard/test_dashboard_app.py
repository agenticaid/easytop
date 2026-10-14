"""The page, run with Streamlit's AppTest on the saved SYNTHETIC snapshot (no Google login, no network)."""

import re
import shutil
from datetime import date
from pathlib import Path

import pytest

st = pytest.importorskip("streamlit")
pytest.importorskip("pandas")
from streamlit.testing.v1 import AppTest  # noqa: E402

from easytop import analytics  # noqa: E402
from tools.dashboard import insights, queries  # noqa: E402
from tools.dashboard.sources import SNAPSHOT_DIR, SnapshotSource, read_csv, write_csv  # noqa: E402

APP = Path(__file__).resolve().parents[2] / "tools" / "dashboard" / "app.py"
PSEUDONYM = re.compile(r"\bp_[0-9a-f]{16}\b")
SECTION_TITLES = [
    "1. Habit: do they come back?",
    "2. Activation: can a new parent get in?",
    "3. Friction: what gets in the way?",
    "4. Worth it: fast, reliable and cheap enough?",
]


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    """A private copy of the saved snapshot, so a test can change a number in it."""
    target = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT_DIR, target)
    monkeypatch.setenv("EASYTOP_DASHBOARD_SOURCE", "snapshot")
    monkeypatch.setenv("EASYTOP_DASHBOARD_SNAPSHOT_DIR", str(target))
    return target


def run_page(snapshot_dir) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def walk(node):
    for child in getattr(node, "children", {}).values():
        yield child
        yield from walk(child)


def page_text(at: AppTest) -> str:
    """Every string and table on the page, sidebar included."""
    out = []
    for el in [*walk(at.main), *walk(at.sidebar)]:
        for attr in ("value", "label", "body", "help", "delta", "options"):
            v = getattr(el, attr, None)
            if isinstance(v, str):
                out.append(v)
            elif isinstance(v, list | tuple):
                out.extend(str(x) for x in v)
            elif hasattr(v, "to_csv"):
                out.append(v.to_csv())
    return "\n".join(out)


def markdown(at: AppTest) -> list[str]:
    return [m.value for m in at.markdown]


def test_the_page_shows_a_headline_four_sections_and_the_footer(snapshot_dir):
    at = run_page(snapshot_dir)
    assert [s.value for s in at.subheader][:4] == SECTION_TITLES
    headline = [m for m in markdown(at) if m.startswith("### Last 7 days:")]
    assert len(headline) == 1
    assert "What we record" in [s.value for s in at.subheader]
    assert (
        "no text, no names, no ids: parents appear only as a pseudonym, and this page never shows one"
        in page_text(at).lower()
    )


def test_the_page_shows_exactly_the_sentences_the_insights_write_from_the_source_rows(snapshot_dir):
    at = run_page(snapshot_dir)
    source = SnapshotSource(snapshot_dir)
    params = queries.Params(date(2026, 1, 1), date(2026, 1, 7), "dev", True)
    rows = {s: source.load(s, params).rows for s in queries.SECTIONS}
    now, before, _, _ = queries.parse_headline(rows["headline"])
    texts = markdown(at)
    assert f"### {insights.headline(7, now, before).text}" in texts
    weeks, week2 = queries.parse_habit(rows["habit"])
    assert insights.habit(weeks, week2).text in texts
    steps, median, timed = queries.parse_activation(rows["activation"])
    assert insights.activation(steps, median, timed).text in texts
    rweeks, routes = queries.parse_friction(rows["friction"])
    assert insights.friction(rweeks, routes).text in texts
    w = queries.parse_worth_it(rows["worth_it"])
    worth = insights.worth_it(7, w.now, w.before, w.nudges_sent, w.nudges_followed)
    assert worth.text in texts or f":red[{worth.text}]" in texts


def test_each_section_has_its_grey_stats_line(snapshot_dir):
    at = run_page(snapshot_dir)
    lines = [c.value for c in at.caption if c.value == "from the saved snapshot"]
    assert len(lines) == 5, "headline plus four sections"


def test_no_pseudonym_appears_anywhere_on_the_page(snapshot_dir):
    at = run_page(snapshot_dir)
    assert not PSEUDONYM.search(page_text(at))
    for period in (7, 28, 90):
        at.sidebar.selectbox[0].set_value(period).run()
        assert not PSEUDONYM.search(page_text(at)), period
    for path in SNAPSHOT_DIR.glob("*.csv"):
        assert not PSEUDONYM.search(path.read_text()), path.name


def test_the_footer_lists_every_event_and_field_from_the_schema_in_the_code(snapshot_dir):
    at = run_page(snapshot_dir)
    shown = {row["Event"]: row["What it records"] for _, row in at.table[-1].value.iterrows()}
    assert set(shown) == set(analytics.SCHEMA)
    for event, fields in analytics.SCHEMA.items():
        for name in fields:
            assert name in shown[event], (event, name)


def test_changing_the_period_changes_the_headline_and_the_dates(snapshot_dir):
    at = run_page(snapshot_dir)
    at.sidebar.selectbox[0].set_value(28).run()
    assert not at.exception
    assert any(m.startswith("### Last 28 days:") for m in markdown(at))


def test_prod_with_nothing_there_says_no_prod_data_yet_and_offers_dev(snapshot_dir):
    at = run_page(snapshot_dir)
    at.sidebar.radio[0].set_value("prod").run()
    assert not at.exception
    assert len(at.info) == 1
    message = at.info[0].value
    assert "No prod data yet" in message and "dev" in message
    assert not any(m.startswith("### Last") for m in markdown(at))
    assert "What we record" in [s.value for s in at.subheader], "the footer stays"


def test_synthetic_data_is_marked_as_such_on_the_page(snapshot_dir):
    at = run_page(snapshot_dir)
    assert any("SYNTHETIC" in w.value for w in at.warning)


def set_failures(snapshot_dir, failures: int):
    for days in (7, 28, 90):
        path = snapshot_dir / f"worth_it_{days}.csv"
        rows = read_csv(path)
        for r in rows:
            if r["part"] == "tiles":
                r["failures"], r["questions"] = failures, max(r["questions"] or 0, 30)
        write_csv(path, rows)


def test_red_is_for_a_real_failure_only(snapshot_dir):
    set_failures(snapshot_dir, 0)
    at = run_page(snapshot_dir)
    assert not any(":red[" in m for m in markdown(at))
    set_failures(snapshot_dir, 2)
    st.cache_data.clear()  # the page keeps its numbers for 10 minutes
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    red = [m for m in markdown(at) if m.startswith(":red[")]
    assert len(red) == 1 and "2 failures" in red[0]


def test_prod_is_the_default_for_the_real_source_and_a_missing_google_login_is_explained(
    monkeypatch,
):
    monkeypatch.setenv("EASYTOP_DASHBOARD_SOURCE", "bigquery")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/credentials.json")
    monkeypatch.setenv("CLOUDSDK_CONFIG", "/nonexistent")
    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception
    assert at.sidebar.radio[0].options == ["prod", "dev"] and at.sidebar.radio[0].value == "prod"
    assert any("gcloud auth application-default login" in e.value for e in at.error)
