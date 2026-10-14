import json
import logging
import sys

from easytop import bot
from easytop.logjson import JsonFormatter


def _record(level, msg, *args, exc=None):
    return logging.LogRecord("easytop.gateway", level, __file__, 1, msg, args, exc)


def test_one_json_object_per_line_with_cloud_severity():
    line = JsonFormatter().format(
        _record(logging.WARNING, "answered parent=%s cost_usd=%s", "telegram:1", "0.0100")
    )
    data = json.loads(line)
    assert data["severity"] == "WARNING"
    assert data["message"] == "answered parent=telegram:1 cost_usd=0.0100"
    assert data["logger"] == "easytop.gateway"
    assert "\n" not in line


def test_exceptions_are_reduced_to_their_type():
    try:
        raise ValueError("secret https://api.telegram.org/bot123:ABC/getUpdates")
    except ValueError:
        line = JsonFormatter().format(_record(logging.ERROR, "failed", exc=sys.exc_info()))
    assert "ABC" not in line and "telegram.org" not in line
    assert json.loads(line)["exception_type"] == "ValueError"


def test_hebrew_stays_readable_and_a_multiline_message_stays_one_line():
    line = JsonFormatter().format(_record(logging.INFO, "a\nb שלום"))
    assert "\n" not in line and "שלום" in line
    assert json.loads(line)["message"] == "a\nb שלום"


def test_configure_logging_can_switch_every_handler_to_json(tmp_path, capsys):
    log_file = tmp_path / "bot.log"
    bot.configure_logging(log_file, json_lines=True)
    try:
        logging.getLogger("easytop.test").info("hello parent=%s", "telegram:1")
        for h in logging.getLogger().handlers:
            h.flush()
        data = json.loads(log_file.read_text().splitlines()[-1])
        assert data["message"] == "hello parent=telegram:1" and data["severity"] == "INFO"
    finally:
        bot.configure_logging(None)


def test_the_environment_switch_turns_on_json_lines(monkeypatch):
    monkeypatch.setenv("EASYTOP_LOG_FORMAT", "json")
    assert bot.json_logs_requested() is True
    monkeypatch.setenv("EASYTOP_LOG_FORMAT", "text")
    assert bot.json_logs_requested() is False
    monkeypatch.delenv("EASYTOP_LOG_FORMAT")
    assert bot.json_logs_requested() is False
