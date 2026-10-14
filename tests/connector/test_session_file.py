import json

import pytest

from easytop.connector.session_file import load_cookies
from easytop.errors import SessionFileError


def _write(tmp_path, cookies):
    p = tmp_path / "session.json"
    p.write_text(json.dumps({"saved_at": "2026-10-01T00:00:00+00:00", "cookies": cookies}))
    return p


def test_keeps_only_smartschool_cookies(tmp_path):
    p = _write(
        tmp_path,
        [
            {"name": "webToken", "value": "t", "domain": ".smartschool.co.il"},
            {"name": "deviceType", "value": "web", "domain": "webtop.smartschool.co.il"},
            {"name": "NID", "value": "g", "domain": "www.google.com"},
        ],
    )
    assert load_cookies(p) == {"webToken": "t", "deviceType": "web"}


def test_missing_web_token_is_an_error(tmp_path):
    p = _write(tmp_path, [{"name": "x", "value": "1", "domain": ".smartschool.co.il"}])
    with pytest.raises(SessionFileError, match="webToken"):
        load_cookies(p)


def test_unreadable_file_error_has_no_content(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json SECRETVALUE")
    with pytest.raises(SessionFileError) as e:
        load_cookies(p)
    assert "SECRETVALUE" not in str(e.value)
