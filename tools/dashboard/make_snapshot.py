"""Save the five queries' results for the SYNTHETIC dev data as CSV, for tests and offline runs.

    uv run --extra dashboard python -m tools.dashboard.make_snapshot

Needs Google login (`gcloud auth application-default login`) and the synthetic data loaded in dev
(`uv run python -m tools.analytics.synthetic --load your-gcp-project`). Writes tools/dashboard/snapshot/<section>_<days>.csv
for 7, 28 and 90 days plus meta.json. The rows are aggregates of made-up families; the script refuses to write
anything that looks like a pseudonym.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tools.dashboard import queries
from tools.dashboard.sources import SNAPSHOT_DIR, SNAPSHOT_PERIODS, BigQuerySource, write_csv

PSEUDONYM = re.compile(r"\bp_[0-9a-f]{16}\b")


def main() -> int:
    today = datetime.now(ZoneInfo("Asia/Jerusalem")).date()
    end = today - timedelta(days=1)
    source = BigQuerySource()
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    written = 0
    for days in SNAPSHOT_PERIODS:
        params = queries.Params(end - timedelta(days=days - 1), end, "dev", True)
        for section in queries.SECTIONS:
            result = source.load(section, params)
            if PSEUDONYM.search(json.dumps(result.rows, default=str)):
                raise SystemExit(f"{section}: a pseudonym is in the result; not writing it")
            write_csv(SNAPSHOT_DIR / f"{section}_{days}.csv", result.rows)
            written += 1
            print(f"{section}_{days}.csv: {len(result.rows)} rows ({result.stats.line()})")
    (SNAPSHOT_DIR / "meta.json").write_text(
        json.dumps(
            {
                "note": "SYNTHETIC dev data (made-up families), aggregates only",
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "period_end": end.isoformat(),
                "periods": list(SNAPSHOT_PERIODS),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{written} files in {SNAPSHOT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
