"""Read a saved Webtop session (Playwright-style cookie file) into the cookies the connector sends.

Only smartschool.co.il cookies are kept; cookies of other sites in the file (Google, ...) never leave it.
Errors never include file content: the cookies are live credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

from easytop.errors import SessionFileError

_DOMAIN = "smartschool.co.il"


def load_cookies(path: Path | str) -> dict[str, str]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))["cookies"]
        cookies = {
            c["name"]: c["value"] for c in raw if c.get("domain", "").lstrip(".").endswith(_DOMAIN)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise SessionFileError(f"cannot read a cookie list from {Path(path).name}") from None
    if "webToken" not in cookies:
        raise SessionFileError(f"no webToken cookie for {_DOMAIN} in {Path(path).name}")
    return cookies
