from datetime import datetime, timezone

import pytest

import main
from execution.executor import Executor
from execution.paper import PaperExchange
from risk.risk_guard import RiskGuard
from tests.conftest import FakeMarket

BASE = {"exchange": "binance", "symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "15m",
        "run_interval_minutes": 15, "live_trading": False, "paper_trading": True,
        "paper": {"starting_cash": 10_000, "fee_pct": 0.001},
        "risk": {"max_position_pct": 0.02, "max_total_exposure_pct": 0.20, "max_daily_loss_pct": 0.03,
                 "max_drawdown_pct": 0.10, "max_orders_per_hour": 4, "kill_switch_file": "unused"}}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("EXCHANGE_API_KEY", "key")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "secret")
    monkeypatch.delenv("LIVE_TRADING", raising=False)


def test_paper_reads_real_prices_with_no_keys():
    market = main.make_market(BASE)
    assert main.mode(BASE) == "PAPER"
    assert not market.apiKey and "testnet" not in str(market.urls["api"])


def test_testnet_mode_uses_sandbox_with_keys():
    settings = {**BASE, "paper_trading": False}
    market = main.make_market(settings)
    assert main.mode(settings) == "TESTNET" and market.apiKey == "key" and "testnet" in str(market.urls["api"])


def test_live_needs_both_switches(monkeypatch):
    assert main.mode({**BASE, "live_trading": True}) == "PAPER"
    monkeypatch.setenv("LIVE_TRADING", "true")
    settings = {**BASE, "live_trading": True}
    market = main.make_market(settings)
    assert main.mode(settings) == "LIVE" and market.apiKey == "key" and "testnet" not in str(market.urls["api"])


@pytest.fixture
def paper_executor(tmp_path):
    settings = {**BASE, "risk": {**BASE["risk"], "kill_switch_file": str(tmp_path / "KILL")}}
    market = FakeMarket()
    executor = Executor(PaperExchange(market, settings, tmp_path / "paper.json"),
                        RiskGuard(settings, tmp_path / "risk.json"), settings)
    return settings, market, executor


def test_daily_summary(paper_executor):
    settings, market, executor = paper_executor
    executor.guard.halts(10_000.0)
    executor.submit({"symbol": "BTC/USDT", "side": "buy", "amount": 0.004, "stop_loss": 49_000.0},
                    {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0})
    text = main.daily_summary(settings, market, executor)
    assert text.startswith("DAILY SUMMARY (PAPER)\nEquity: 9,999.76 USDT")
    assert "Today: -0.00%" in text and "BTC/USDT: 0.004 (~200.00)" in text and text.endswith("Trading active")


def test_daily_summary_shows_halt(paper_executor, tmp_path):
    settings, market, executor = paper_executor
    (tmp_path / "KILL").write_text("maintenance")
    assert main.daily_summary(settings, market, executor).endswith("Trading HALTED: kill switch on: maintenance")


class Recorder:
    def __init__(self):
        self.sent, self.failed = [], []

    def send(self, text):
        self.sent.append(text)

    def run_failed(self, error):
        self.failed.append(error)

    def run_finished(self, state):
        pass


def test_schedule_runs_now_then_on_candle_close_without_overlap(paper_executor):
    settings, market, executor = paper_executor
    scheduler = main.build_scheduler(settings, market, executor, graph=None, alerter=Recorder())
    trade, summary = scheduler.get_job("trade"), scheduler.get_job("summary")
    assert "minute='*/15'" in str(trade.trigger) and "second='10'" in str(trade.trigger)
    assert trade.max_instances == 1 and trade.coalesce
    assert (datetime.now(timezone.utc) - trade.next_run_time).total_seconds() < 5
    assert "hour='23'" in str(summary.trigger) and "minute='55'" in str(summary.trigger)


def test_report_page_runs_inside_the_bot(monkeypatch, tmp_path):
    import urllib.request

    monkeypatch.setenv("REPORT_PORT", "0")  # any free port
    server = main.start_report(Recorder())
    try:
        host, port = server.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/") as r:
            assert r.status == 200 and "Trading bot report" in r.read().decode()
    finally:
        server.shutdown()


def test_busy_report_port_does_not_stop_the_bot(monkeypatch):
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    monkeypatch.setenv("REPORT_PORT", str(taken.getsockname()[1]))
    alerter = Recorder()
    try:
        assert main.start_report(alerter) is None
        assert alerter.sent[0].startswith("WARNING: report page not started")
    finally:
        taken.close()


def test_jobs_never_raise(paper_executor, monkeypatch):
    settings, market, executor = paper_executor
    alerter = Recorder()
    monkeypatch.setattr(main, "run_once", lambda graph, a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(market, "fetch_ticker", lambda s: (_ for _ in ()).throw(RuntimeError("down")))
    scheduler = main.build_scheduler(settings, market, executor, graph=None, alerter=alerter)
    scheduler.get_job("trade").func()
    scheduler.get_job("summary").func()
    assert str(alerter.failed[0]) == "down"
