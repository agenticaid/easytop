"""Where the page gets its numbers: BigQuery (real) or a committed CSV snapshot (tests, offline runs).

Both give back the same thing for a section: a `Result` with plain row dicts and the stats for the grey line.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from tools.dashboard import queries
from tools.dashboard.queries import Params, Result, Stats

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshot"
SNAPSHOT_PERIODS = (7, 28, 90)


class NoDataYet(Exception):
    """There is no dataset (or no snapshot) for the chosen environment: said on the page, not a crash."""


def login_problem(error: Exception) -> str | None:
    """ "login" (no usable Google login), "permission" (the account may not read the dataset) or None (anything else).

    Told apart by class name so this module needs no Google library at import time."""
    name = type(error).__name__
    if any(word in name for word in ("Credentials", "Refresh", "Reauth", "Unauthenticated")):
        return "login"
    if any(word in name for word in ("Forbidden", "PermissionDenied")):
        return "permission"
    return None


class Source(Protocol):
    name: str

    def load(self, section: str, params: Params) -> Result: ...


class BigQuerySource:
    name = "bigquery"

    def __init__(self, project: str = queries.DEFAULT_PROJECT, client: Any = None):
        self.project = project
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project)
        return self._client

    def load(self, section: str, params: Params) -> Result:
        from google.api_core.exceptions import NotFound

        try:
            return queries.run(self.client, section, params, self.project)
        except NotFound as e:
            raise NoDataYet(
                f"No {params.env} data yet: the dataset easytop_analytics_{params.env} does not exist "
                "(for prod it is created when prod is deployed and the analytics setup has run)."
            ) from e


class SnapshotSource:
    """The five queries' results for the SYNTHETIC dev data, saved as CSV by tools/dashboard/make_snapshot.py.

    One file per section and period length (`headline_28.csv`). It answers for dev with synthetic data only; the
    start and end the page asks for are ignored (the rows carry their own dates), only the length picks the file.
    """

    name = "snapshot"

    def __init__(self, directory: Path = SNAPSHOT_DIR):
        self.directory = Path(directory)

    def meta(self) -> dict[str, Any]:
        path = self.directory / "meta.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def load(self, section: str, params: Params) -> Result:
        if params.env != "dev" or not params.synthetic:
            raise NoDataYet(
                "The saved snapshot holds the SYNTHETIC dev data only: choose dev with synthetic data."
            )
        days = min(SNAPSHOT_PERIODS, key=lambda d: abs(d - params.days))
        path = self.directory / f"{section}_{days}.csv"
        if not path.exists():
            raise NoDataYet(f"No snapshot file {path.name}: run tools/dashboard/make_snapshot.py.")
        return Result(read_csv(path), Stats(source="snapshot"))


# ---------------------------------------------------------------- CSV with the value types back again

_INT = re.compile(r"-?\d+")
_FLOAT = re.compile(r"-?\d+\.\d+(e-?\d+)?|-?\d+e-?\d+", re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _coerce(text: str) -> Any:
    if text == "":
        return None
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    if _DATE.fullmatch(text):
        return date.fromisoformat(text)
    return text


def _plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def read_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: _coerce(v) for k, v in row.items()} for row in csv.DictReader(f)]


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str] | None = None) -> None:
    columns = columns or (list(rows[0]) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _plain(row.get(c)) for c in columns})
