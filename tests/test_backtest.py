import pytest

from agents.schemas import Decision, MarketReport, RiskAdvice
from backtest.run_backtest import HistoricalMarket, download_candles, realized_pnls, report, run_backtest
from risk.risk_guard import RiskGuard
from tests.conftest import FakeMarket

TF = 900_000  # 15m
T0 = 1_800_000_000_000 // TF * TF
SETTINGS = {"symbols": ["BTC/USDT"], "timeframe": "15m", "live_trading": False, "paper_trading": True,
            "paper": {"starting_cash": 10_000, "fee_pct": 0.001}, "backtest": {"slippage_pct": 0.0005},
            "risk": {"max_position_pct": 0.02, "max_total_exposure_pct": 0.20, "max_daily_loss_pct": 0.03,
                     "max_drawdown_pct": 0.10, "max_orders_per_hour": 4, "kill_switch_file": "unused"}}
MARKET = MarketReport(trend="up", strength=0.6, support=49_000.0, resistance=51_000.0, summary="Up.")
ADVICE = RiskAdvice(max_size=1.0, reasoning="Fine.")


def candles(closes, lows=None):
    """15m candles from 200 before T0; lows default to 10 under the close."""
    lows = lows or {}
    return [[T0 + (i - 200) * TF, c, c + 10, lows.get(i - 200, c - 10), c, 1.0] for i, c in enumerate(closes)]


class ScriptedClient:
    """Agents always answer; the decision agent follows a script, one decision per call, then HOLDs."""

    def __init__(self, decisions):
        self.decisions, self.calls = list(decisions), []

    def ask(self, tier, system, user, schema):
        self.calls.append(schema.__name__)
        if schema is Decision:
            return self.decisions.pop(0) if self.decisions else Decision(action="HOLD")
        return {MarketReport: MARKET, RiskAdvice: ADVICE}[schema]


BUY = Decision(action="BUY", symbol="BTC/USDT", size=1.0, stop_loss=49_000.0)


def backtest(tmp_path, closes, decisions, steps, lows=None):
    client = ScriptedClient(decisions)
    result = run_backtest(SETTINGS, client, {"BTC/USDT": candles(closes, lows)}, FakeMarket(),
                          T0, T0 + (steps - 1) * TF, tmp_path)
    return result, client


# --- historical market ---

def test_market_shows_only_closed_candles():
    hist = HistoricalMarket({"BTC/USDT": candles([100.0 + i for i in range(210)])}, "15m", 0.001, FakeMarket())
    hist.now = T0
    assert hist.fetch_ohlcv("BTC/USDT", "15m")[-1][0] == T0 - TF  # the candle opening at T0 is not closed yet
    assert hist.fetch_ohlcv("BTC/USDT", "15m", limit=3)[0][0] == T0 - 3 * TF
    assert hist.fetch_ohlcv("BTC/USDT", "1m", since=T0) == []
    ticker = hist.fetch_ticker("BTC/USDT")
    assert ticker["last"] == 299.0 and ticker["ask"] == pytest.approx(299.299) and ticker["bid"] < 299.0
    hist.now = T0 + 2 * TF
    assert [c[0] for c in hist.fetch_ohlcv("BTC/USDT", "1m", since=T0)] == [T0, T0 + TF]


def test_download_pages_until_the_end():
    class Paged(FakeMarket):
        def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
            return [[t, 1, 1, 1, 1, 1] for t in range(since, since + 3 * TF, TF)]

    got = download_candles(Paged(), "BTC/USDT", "15m", T0, T0 + 7 * TF)
    assert [c[0] for c in got] == [T0 + i * TF for i in range(7)]


# --- metrics ---

def test_report_metrics():
    r = report([100.0, 110.0, 99.0, 120.0], [], 0.001, TF)
    assert r["return_pct"] == pytest.approx(0.2)
    assert r["max_drawdown_pct"] == pytest.approx(0.1)
    assert r["sharpe"] > 0 and r["trades"] == 0 and r["win_rate"] is None


def test_flat_curve_has_zero_sharpe():
    assert report([100.0] * 5, [], 0.001, TF)["sharpe"] == 0.0


def test_realized_pnls_use_fill_order_and_average_cost():
    def fill(side, qty, price, t, status="closed"):
        return {"symbol": "BTC/USDT", "side": side, "filled": qty, "average": price, "status": status,
                "lastTradeTimestamp": t}

    orders = [fill("buy", 1, 100, 1), fill("sell", 1, 90, 5),  # stop placed early, filled last
              fill("buy", 1, 120, 2), fill("sell", 1, 150, 3), fill("sell", 1, 1, 9, status="canceled")]
    pnls = realized_pnls(orders, 0.0)
    assert pnls == [pytest.approx(150 - 110), pytest.approx(90 - 110)]


# --- full replay through the workflow ---

def test_stop_loss_trade_through_the_workflow(tmp_path):
    closes = [50_000.0] * 210
    result, client = backtest(tmp_path, closes, [BUY], steps=5, lows={2: 48_500.0})
    assert result["trades"] == 1 and result["win_rate"] == 0
    assert result["return_pct"] < 0 and result["max_drawdown_pct"] > 0
    # The buy was 2% of equity and the stop 2% below, so the loss is small: about 2% of 2% plus costs.
    assert result["return_pct"] == pytest.approx(-0.0006, abs=0.0002)
    assert client.calls.count("Decision") == 5


def test_winning_sell_through_the_workflow(tmp_path):
    closes = [50_000.0] * 201 + [52_000.0] * 9
    sell = Decision(action="SELL", symbol="BTC/USDT", size=1.0)
    result, _ = backtest(tmp_path, closes, [BUY, Decision(action="HOLD"), sell], steps=4)
    assert result["trades"] == 1 and result["win_rate"] == 1 and result["return_pct"] > 0


def test_halted_steps_make_no_model_calls(tmp_path):
    (tmp_path / "KILL").write_text("stop")
    result, client = backtest(tmp_path, [50_000.0] * 210, [BUY], steps=3)
    assert client.calls == [] and result["trades"] == 0 and result["return_pct"] == 0


def test_guard_runs_on_the_replayed_clock(tmp_path):
    clock = iter([1_000.0, 90_000.0])
    guard = RiskGuard({**SETTINGS, "risk": {**SETTINGS["risk"], "kill_switch_file": str(tmp_path / "K")}},
                      tmp_path / "r.json", clock=lambda: next(clock))
    guard.halts(10_000.0)
    assert guard._load()["day"] == "1970-01-01"
    guard.halts(10_000.0)
    assert guard._load()["day"] == "1970-01-02"
