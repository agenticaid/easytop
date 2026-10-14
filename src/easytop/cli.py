"""`tomorrow` CLI. Without --session it runs on the FAKE connector (synthetic data); with --session FILE it
reads the real Webtop account (read-only calls only) using the saved cookies in FILE."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime
from typing import Any

from easytop.connector.base import Connector
from easytop.connector.fake import FakeConnector
from easytop.connector.session_file import load_cookies
from easytop.connector.webtop import WebtopConnector
from easytop.errors import EasytopError, KidNotFound
from easytop.family.render import kid_lines
from easytop.family.service import FamilyService

SYNTHETIC = "Webtop (SYNTHETIC demo data)"
LIVE = "Webtop (live)"


def build_live_connector(cookies: dict[str, str], today: date) -> WebtopConnector:
    return WebtopConnector(cookies, clock=lambda: today)


def render_brief(brief: dict[str, Any], source: str = SYNTHETIC) -> str:
    lines = [f"Tomorrow: {brief['weekday']} {brief['date']}"]
    if brief.get("skipped"):
        lines.append(
            "(skipped: " + ", ".join(f"{s['date']} {s['reason']}" for s in brief["skipped"]) + ")"
        )
    for k in brief["kids"]:
        lines.append(f"\n{k['name']} ({k['class']})")
        if k.get("lessons"):
            lines.append("  Lessons:")
            lines += [
                f"    - {x['subject']}" + (f": {x['note']}" if x.get("note") else "")
                for x in k["lessons"]
            ]
        lines += [f"  • {i}" for i in kid_lines(k)]
    if brief.get("messages"):
        lines.append("\nMessages (last 7 days):")
        lines += [f"  • {m['sender']}: {m['subject']}" for m in brief["messages"]]
    lines.append(f"\nSource: {source}, checked {brief['checked_at'][11:16]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tomorrow", description="What do the kids need tomorrow? (fake data)"
    )
    ap.add_argument("--kid", help="kid id or name; default all")
    ap.add_argument("--session", help="saved cookie file: read the real Webtop account (read-only)")
    args = ap.parse_args(argv)
    now = now or datetime.now()
    connector: Connector
    if args.session:
        try:
            connector = build_live_connector(load_cookies(args.session), now.date())
        except EasytopError as e:
            print(f"error: {e}")
            return 2
        source = LIVE
    else:
        connector, source = FakeConnector(now.date()), SYNTHETIC
    svc = FamilyService(connector, clock=lambda: now)

    async def run() -> dict[str, Any]:
        try:
            return await svc.get_tomorrow_brief(args.kid)
        finally:  # close on the loop that opened the connections
            close = getattr(connector, "aclose", None)
            if close:
                await close()

    try:
        brief = asyncio.run(run())
    except KidNotFound as e:
        print(f"error: {e}")
        return 2
    print(render_brief(brief, source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
