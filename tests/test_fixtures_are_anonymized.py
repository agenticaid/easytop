"""Guard for committed Webtop fixtures: nothing that looks like contact data, a token or free text."""

import json
import re
from pathlib import Path

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "webtop").glob("*.json"))
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(?<!\d)(\+972|0)[-\s]?\d{1,2}[-\s]?\d{3}[-\s]?\d{4}(?!\d)")
LONG_OPAQUE = re.compile(r"[A-Za-z0-9+/_=-]{60,}")


def _strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)
    elif isinstance(value, str):
        yield value


def test_fixtures_exist():
    assert len(FIXTURES) > 30


def test_no_real_contact_data_or_tokens():
    for path in FIXTURES:
        for s in _strings(json.loads(path.read_text(encoding="utf-8"))):
            for m in EMAIL.finditer(s):
                assert m.group(0).endswith("@example.test"), path.name
            for m in PHONE.finditer(s):
                assert m.group(0).startswith("050-000-"), path.name
            for m in LONG_OPAQUE.finditer(s):
                assert m.group(0).isdigit(), (
                    f"{path.name}: long opaque string that is not a fake id"
                )


def test_no_credentials_or_captcha_data_in_requests():
    for path in FIXTURES:
        text = path.read_text(encoding="utf-8").lower()
        assert "captcha" not in text and '"password"' not in text, path.name
