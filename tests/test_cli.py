from datetime import datetime

from easytop.cli import main, render_brief

NOW = datetime(2026, 10, 5, 18, 42)


def test_cli_prints_brief_for_all_kids(capsys):
    assert main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert "Tuesday 2026-10-06" in out
    assert "Noa" in out and "Ido" in out
    assert "Workbook page 42" in out
    assert "cancelled" in out
    assert "SYNTHETIC" in out  # never mistaken for real data


def test_cli_lists_the_family_inbox_of_the_last_seven_days(capsys):
    assert main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert "Messages (last 7 days)" in out and "Class trip form" in out and "Parents evening" in out


def test_cli_single_kid_and_unknown_kid(capsys):
    assert main(["--kid", "Ido"], now=NOW) == 0
    out = capsys.readouterr().out
    assert "Ido" in out and "Noa" not in out
    assert main(["--kid", "Nobody"], now=NOW) == 2


def test_render_failure_named_per_kid():
    brief = {
        "date": "2026-10-06",
        "weekday": "Tuesday",
        "skipped": [],
        "checked_at": "2026-10-05T18:42",
        "kids": [
            {"name": "Noa", "class": "x", "ok": True, "lessons": [], "homework_due": []},
            {"name": "Ido", "class": "y", "ok": False, "error": "unavailable"},
        ],
    }
    text = render_brief(brief)
    assert "Ido" in text and "didn't respond" in text.lower()
    assert "Noa" in text and "nothing" in text.lower()


def test_cli_live_session_uses_real_connector_and_is_labelled(tmp_path, capsys, monkeypatch):
    import json

    from easytop import cli
    from easytop.connector.fake import FakeConnector

    session = tmp_path / "s.json"
    session.write_text(
        json.dumps(
            {"cookies": [{"name": "webToken", "value": "t", "domain": ".smartschool.co.il"}]}
        )
    )
    seen = {}

    def fake_build(cookies, today):
        seen["cookies"] = cookies
        return FakeConnector(today)

    monkeypatch.setattr(cli, "build_live_connector", fake_build)
    assert main(["--session", str(session)], now=NOW) == 0
    out = capsys.readouterr().out
    assert seen["cookies"] == {"webToken": "t"}
    assert "SYNTHETIC" not in out and "Source: Webtop (live)" in out


def test_cli_bad_session_file_exits_2_without_content(tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("SECRETVALUE")
    assert main(["--session", str(p)], now=NOW) == 2
    assert "SECRETVALUE" not in capsys.readouterr().out


def test_render_shows_each_subject_with_its_homework_text():
    brief = {
        "date": "2026-10-06",
        "weekday": "Tuesday",
        "skipped": [],
        "checked_at": "2026-10-05T18:42",
        "kids": [
            {
                "name": "Noa",
                "class": "x",
                "ok": True,
                "lessons": [{"subject": "Math"}, {"subject": "Art"}],
                "homework_due": [
                    {"subject": "Math", "description": "p.42", "assigned": "2026-10-01"}
                ],
            }
        ],
    }
    text = render_brief(brief, source="Webtop (live)")
    assert "    - Math\n" in text and "    - Art\n" in text
    assert "Math: p.42 (from the lesson of 2026-10-01)" in text
    assert "(due)" not in text


def test_cli_closes_connector_on_the_loop_that_used_it(tmp_path, monkeypatch):
    import asyncio
    import json

    from easytop import cli
    from easytop.connector.fake import FakeConnector

    loops = {}

    class Tracking(FakeConnector):
        async def list_kids(self):
            loops["used"] = asyncio.get_running_loop()
            return await super().list_kids()

        async def aclose(self):
            loops["closed"] = asyncio.get_running_loop()

    session = tmp_path / "s.json"
    session.write_text(
        json.dumps(
            {"cookies": [{"name": "webToken", "value": "t", "domain": ".smartschool.co.il"}]}
        )
    )
    monkeypatch.setattr(cli, "build_live_connector", lambda cookies, today: Tracking(today))
    assert main(["--session", str(session)], now=NOW) == 0
    assert loops["closed"] is loops["used"]


def test_render_shows_lesson_box_text_under_each_lesson():
    brief = {
        "date": "2026-10-06",
        "weekday": "Tuesday",
        "skipped": [],
        "checked_at": "2026-10-05T18:42",
        "kids": [
            {
                "name": "Noa",
                "class": "x",
                "ok": True,
                "lessons": [
                    {"subject": "Math", "note": "bring compass | topic: angles"},
                    {"subject": "Art"},
                ],
                "homework_due": [],
            }
        ],
    }
    text = render_brief(brief)
    assert "  Lessons:\n" in text
    assert "    - Math: bring compass | topic: angles" in text
    assert "    - Art\n" in text


def test_render_homework_line_shows_the_class_topic_and_date():
    brief = {
        "date": "2026-10-04",
        "weekday": "Sunday",
        "skipped": [],
        "checked_at": "2026-10-01T18:42",
        "kids": [
            {
                "name": "Noa",
                "class": "x",
                "ok": True,
                "lessons": [],
                "homework_due": [
                    {
                        "subject": "Math",
                        "description": "p.6",
                        "topic": "angles",
                        "assigned": "2026-09-27",
                    }
                ],
            }
        ],
    }
    assert "Math: p.6 (class topic: angles; from the lesson of 2026-09-27)" in render_brief(brief)
