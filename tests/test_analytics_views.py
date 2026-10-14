"""The BigQuery views behind the dashboard (docs/analytics/*.sql), checked offline as text.

The numbers they produce are checked against Python in tests/test_analytics_views_live.py (needs BigQuery).
Here: the shape rules that keep the dashboard content-free and the setup script working.
"""

import re
from pathlib import Path

import pytest

from tools.dashboard.queries import APP_VIEWS as APP_VIEWS_SET

VIEW_DIR = Path(__file__).resolve().parent.parent / "docs" / "analytics"

# The views the dashboard reads (tools/dashboard/queries.py). They return counts, shares and medians only: never a
# parent_ref.
APP_VIEWS = sorted(APP_VIEWS_SET)
# Helpers and per-event views: read by other views only.
INTERNAL_VIEWS = ["events", "periods", "session_lifetime"]
NEW_VIEWS = [
    "periods",
    "tomorrow_coverage",
    "habit_weekly",
    "week2_return",
    "time_to_first_answer",
    "reconnects_weekly",
    "worth_it",
    "nudge_conversion",
    "session_lifetime_by_route",
]


def view_file(name: str) -> Path:
    found = sorted(VIEW_DIR.glob(f"[0-9]*_{name}.sql"))
    assert len(found) == 1, f"expected one numbered SQL file for view {name}, got {found}"
    return found[0]


def code_of(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts, depth, current = [], 0, []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def output_columns(sql: str) -> list[str]:
    """Names in the select list of the view's final (top-level) SELECT."""
    text = code_of(sql)
    depth, starts = 0, []
    for m in re.finditer(r"\(|\)|\bSELECT\b", text, re.I):
        if m.group() == "(":
            depth += 1
        elif m.group() == ")":
            depth -= 1
        elif depth == 0:
            starts.append(m.end())
    start = starts[-1]
    depth, end = 0, len(text)
    for m in re.finditer(r"\(|\)|\bFROM\b|;", text[start:], re.I):
        if m.group() == "(":
            depth += 1
        elif m.group() == ")":
            depth -= 1
        elif depth == 0:
            end = start + m.start()
            break
    select_list = re.sub(r"^\s*DISTINCT\b", "", text[start:end], flags=re.I)
    names = []
    for item in split_top_level(select_list):
        alias = re.search(r"\bAS\s+(\w+)\s*$", item, re.I)
        names.append(alias.group(1) if alias else re.split(r"[\s.]", item)[-1])
    return names


def test_every_new_view_has_one_numbered_file_that_creates_it():
    for name in NEW_VIEWS:
        sql = view_file(name).read_text()
        assert f"CREATE OR REPLACE VIEW `${{PROJECT}}.${{DATASET}}.{name}` AS" in sql, name


def test_files_are_ordered_so_a_view_is_created_after_the_views_it_reads():
    files = sorted(VIEW_DIR.glob("[0-9]*.sql"))
    created: list[str] = []
    for f in files:
        sql = f.read_text()
        name = re.search(r"CREATE OR REPLACE VIEW `\$\{PROJECT\}\.\$\{DATASET\}\.(\w+)`", sql)[1]
        reads = set(re.findall(r"`\$\{PROJECT\}\.\$\{DATASET\}\.(\w+)`", code_of(sql))) - {name}
        reads -= {"events_raw"}  # written by tools/ops/analytics_views.sh before the files
        assert reads <= set(created), f"{f.name} reads {reads - set(created)} before it exists"
        created.append(name)


@pytest.mark.parametrize("name", NEW_VIEWS)
def test_new_views_keep_env_and_synthetic_columns(name):
    columns = output_columns(view_file(name).read_text())
    assert "env" in columns and "synthetic" in columns, columns


@pytest.mark.parametrize("name", APP_VIEWS)
def test_views_the_app_reads_never_expose_a_parent(name):
    sql = view_file(name).read_text()
    columns = output_columns(sql)
    assert "*" not in columns or name in ("funnel",), f"{name} selects *: list the columns"
    assert not any("parent" in c.lower() for c in columns), columns


def test_the_column_reader_itself_sees_a_parent_column():
    sql = "CREATE VIEW v AS WITH a AS (SELECT 1) SELECT env, synthetic, parent_ref, COUNT(*) AS n FROM a;"
    assert output_columns(sql) == ["env", "synthetic", "parent_ref", "n"]


def test_the_views_read_events_not_the_raw_log_table():
    for name in NEW_VIEWS:
        sql = code_of(view_file(name).read_text())
        assert "events_raw" not in sql and "run_googleapis" not in sql, name


def test_internal_views_exist():
    for name in INTERNAL_VIEWS:
        view_file(name)
