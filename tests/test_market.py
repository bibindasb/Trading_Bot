from unittest.mock import MagicMock

import pytest

from data import market

SETTINGS = {"exchange": "binance", "symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "15m", "live_trading": False}
M15 = 900_000


def fake_exchange(now_ms=10 * M15 + 1):
    ex = MagicMock()
    ex.parse_timeframe.return_value = 900
    ex.milliseconds.return_value = now_ms
    # Candles opening at 7, 8, 9 and 10 intervals; the last one is still forming.
    ex.fetch_ohlcv.return_value = [[i * M15, 1.0, 2.0, 0.5, 1.5, 10.0] for i in (7, 8, 9, 10)]
    ex.fetch_balance.return_value = {"total": {"USDT": 1000.0, "BTC": 0.0, "ETH": 0.5}}
    ex.fetch_ticker.return_value = {"last": 1.5}
    return ex


@pytest.mark.parametrize(
    "flag, env, live",
    [(False, "false", False), (True, "false", False), (False, "true", False), (True, None, False),
     ("true", "true", False), (True, "true", True)],
)
def test_live_needs_both_switches(monkeypatch, flag, env, live):
    monkeypatch.delenv("LIVE_TRADING", raising=False)
    if env is not None:
        monkeypatch.setenv("LIVE_TRADING", env)
    settings = {**SETTINGS, "live_trading": flag}
    assert market.is_live(settings) is live
    sandbox = MagicMock()
    monkeypatch.setattr(market.ccxt.binance, "set_sandbox_mode", sandbox)
    market.make_exchange(settings)
    assert sandbox.called is not live


def test_ohlcv_drops_forming_candle_and_parses_time():
    df = market.fetch_ohlcv(fake_exchange(), "BTC/USDT", "15m")
    assert list(df.columns) == market.COLUMNS
    assert len(df) == 3
    assert df["timestamp"].iloc[-1].value == 9 * M15 * 1_000_000  # ns
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_ohlcv_empty_response():
    ex = fake_exchange()
    ex.fetch_ohlcv.return_value = []
    assert market.fetch_ohlcv(ex, "BTC/USDT", "15m").empty


def test_balances_skip_zero():
    assert market.fetch_balances(fake_exchange()) == {"USDT": 1000.0, "ETH": 0.5}


def test_fetch_all_covers_every_configured_symbol():
    snapshot = market.fetch_all(fake_exchange(), SETTINGS)
    assert set(snapshot) == set(SETTINGS["symbols"])
    assert snapshot["ETH/USDT"]["ticker"]["last"] == 1.5
    assert len(snapshot["BTC/USDT"]["ohlcv"]) == 3
