import math

import ccxt

T0_MS = 1_800_000_000_000


class FakeMarket:
    """Stands in for the ccxt data exchange: prices, 1m candles and amount precision (6 decimals)."""

    def __init__(self):
        self.now = T0_MS
        self.tickers = {"BTC/USDT": {"bid": 49_990.0, "ask": 50_010.0, "last": 50_000.0},
                        "ETH/USDT": {"bid": 1_999.0, "ask": 2_001.0, "last": 2_000.0}}
        self.candles = []  # [timestamp, open, high, low, close, volume], served for any symbol

    def milliseconds(self):
        return self.now

    def load_markets(self):
        return {}

    def fetch_ticker(self, symbol):
        return self.tickers[symbol]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        return [c for c in self.candles if c[0] >= since]

    def amount_to_precision(self, symbol, amount):
        result = math.floor(round(amount * 1e6, 6)) / 1e6
        if result == 0:
            raise ccxt.InvalidOrder("amount below precision")
        return str(result)
