import json
import logging

import httpx
import pytest

from monitoring import alerts
from monitoring.alerts import Alerter
from monitoring.logger import setup_logging

TOKEN = "123456:SECRET-TOKEN"


# --- logger ---

@pytest.fixture
def log_file(tmp_path):
    path = tmp_path / "logs" / "bot.jsonl"
    setup_logging(path)
    yield path
    logging.getLogger().handlers.clear()


def lines(path):
    for handler in logging.getLogger().handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_log_lines_are_json_with_data(log_file):
    logging.getLogger("agents").info("llm call", extra={"data": {"user": "BTC up", "output": {"score": 0.4}}})
    entry = lines(log_file)[-1]
    assert entry["msg"] == "llm call" and entry["logger"] == "agents" and entry["level"] == "INFO"
    assert entry["data"] == {"user": "BTC up", "output": {"score": 0.4}} and entry["ts"].endswith("+00:00")


def test_exceptions_are_logged_with_traceback(log_file):
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("x").exception("run failed")
    assert "ValueError: boom" in lines(log_file)[-1]["error"]


def test_setup_twice_does_not_double_lines_and_quiets_url_loggers(log_file):
    setup_logging(log_file)
    logging.getLogger("x").info("once")
    assert [e["msg"] for e in lines(log_file)].count("once") == 1
    assert logging.getLogger("httpx").level == logging.WARNING == logging.getLogger("httpx2").level


# --- alerts ---

@pytest.fixture
def sent(monkeypatch):
    sent = []

    def post(url, json, timeout):
        sent.append({"url": url, **json})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(alerts.httpx, "post", post)
    return sent


@pytest.fixture
def alerter():
    return Alerter(TOKEN, "42")


BUY_STATE = {"halts": [], "issues": [], "decision": {"reasoning": "Trend and news agree."},
             "order": {"symbol": "BTC/USDT", "side": "buy", "amount": 0.002, "stop_loss": 48_000.0},
             "result": {"status": "filled", "issues": [],
                        "order": {"side": "buy", "symbol": "BTC/USDT", "filled": 0.002, "average": 50_010.0}}}


def test_trade_alert(alerter, sent):
    text = alerter.run_finished(BUY_STATE)
    assert text == "TRADE (filled): BUY 0.002 BTC/USDT at 50010.0, stop 48000.0. Trend and news agree."
    assert sent == [{"url": f"https://api.telegram.org/bot{TOKEN}/sendMessage", "chat_id": "42", "text": text}]


def test_quiet_hold_sends_nothing(alerter, sent):
    assert alerter.run_finished({"halts": [], "issues": [], "result": None}) is None and sent == []


def test_rejected_and_failed_orders_are_alerted(alerter, sent):
    alerter.run_finished({"result": {"status": "rejected", "reasons": ["position size 300 over 2% of equity"]}})
    alerter.run_finished({"result": {"status": "error", "error": "InsufficientFunds()",
                                     "issues": ["BTC/USDT: could not place stop loss: x"]}})
    assert sent[0]["text"] == "REJECTED by risk guard: position size 300 over 2% of equity"
    assert sent[1]["text"] == "ORDER ERROR: InsufficientFunds()\nWARNING: BTC/USDT: could not place stop loss: x"


def test_halt_is_alerted_once_and_when_it_clears(alerter, sent):
    halted = {"halts": ["kill switch on: max drawdown hit"], "issues": []}
    alerter.run_finished(halted)
    alerter.run_finished(halted)
    alerter.run_finished({"halts": [], "issues": []})
    assert [s["text"] for s in sent] == ["HALTED: kill switch on: max drawdown hit",
                                         "RESUMED: trading is no longer halted"]


def test_ongoing_issue_is_alerted_once(alerter, sent):
    state = {"halts": [], "issues": ["BTC/USDT: 0.01 held with no stop loss"]}
    alerter.run_finished(state)
    alerter.run_finished(state)
    assert len(sent) == 1 and sent[0]["text"].startswith("WARNING: BTC/USDT")


def test_failing_runs_alert_once_then_recover(alerter, sent):
    alerter.run_failed(RuntimeError("exchange down"))
    alerter.run_failed(RuntimeError("exchange down"))
    alerter.run_finished({"halts": [], "issues": []})
    assert [s["text"] for s in sent] == ["ERROR: run failed: RuntimeError('exchange down')",
                                         "RECOVERED: runs are succeeding again"]


def test_without_credentials_alerts_are_only_logged(sent, caplog):
    caplog.set_level(logging.INFO)
    assert Alerter("", "").send("hello") is False
    assert sent == [] and any(getattr(r, "data", {}).get("text") == "hello" for r in caplog.records)


def test_telegram_failure_never_raises_or_logs_the_token(monkeypatch, alerter, caplog):
    def fail(url, json, timeout):
        response = httpx.Response(401, request=httpx.Request("POST", url))
        response.raise_for_status()

    monkeypatch.setattr(alerts.httpx, "post", fail)
    assert alerter.send("hello") is False
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and TOKEN not in errors[0] and "***" in errors[0]


def test_long_messages_are_cut_to_telegram_limit(alerter, sent):
    alerter.send("x" * 10_000)
    assert len(sent[0]["text"]) == alerts.MAX_LENGTH
