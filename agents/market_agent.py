"""Market analysis agent (fast model): code computes the indicators, the model summarizes them."""
import json
import math

import pandas as pd
import ta

from agents.schemas import MarketReport

SYSTEM = ("You are the market analysis agent of a crypto trading bot, looking at {symbol} on {timeframe} candles. "
          "You get technical indicators computed by code. Classify the trend, rate its strength from 0 to 1, "
          "give the nearest support and resistance prices, and summarize the picture in one or two sentences. "
          "Use only the numbers given.")


def _num(value) -> float | None:
    return None if value is None or not math.isfinite(value) else float(f"{value:.6g}")


def indicators(ohlcv: pd.DataFrame) -> dict:
    """Latest indicator values from closed candles. Too little history gives None, never an error."""
    close, high, low = ohlcv["close"], ohlcv["high"], ohlcv["low"]

    def last(series):
        return _num(series.iloc[-1]) if len(series) else None

    atr = ta.volatility.AverageTrueRange(high, low, close, window=14, fillna=False).average_true_range() \
        if len(close) > 14 else pd.Series(dtype=float)
    bands = ta.volatility.BollingerBands(close, window=20)
    return {
        "close": last(close),
        "ema_20": last(ta.trend.EMAIndicator(close, window=20).ema_indicator()),
        "ema_50": last(ta.trend.EMAIndicator(close, window=50).ema_indicator()),
        "rsi_14": last(ta.momentum.RSIIndicator(close, window=14).rsi()),
        "macd_diff": last(ta.trend.MACD(close).macd_diff()),
        "atr_14": last(atr),
        "atr_pct": _num(atr.iloc[-1] / close.iloc[-1]) if len(atr) else None,
        "bollinger_high": last(bands.bollinger_hband()),
        "bollinger_low": last(bands.bollinger_lband()),
        "high_50": _num(high.tail(50).max()) if len(high) else None,
        "low_50": _num(low.tail(50).min()) if len(low) else None,
        "return_20": _num(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 20 else None,
        "last_closes": [_num(c) for c in close.tail(10)],
    }


def analyze(client, symbol: str, timeframe: str, values: dict) -> MarketReport | None:
    """values: output of indicators(). None means the model gave no valid report, which makes the run HOLD."""
    return client.ask("fast", SYSTEM.format(symbol=symbol, timeframe=timeframe), json.dumps(values), MarketReport)
