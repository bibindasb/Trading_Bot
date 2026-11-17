import math

import ccxt

T0_MS = 1_800_000_000_000


class FakeMarket:
    """Stands in for the ccxt data exchange: prices, 1m candles and amount precision (6 decimals)."""

    def __init__(self):
        self.now = T0_MS
        self.tickers = {"BTC/USDT": {"bid": 49_990.0, "ask": 50_010.0, "last": 50_000.0},
                        "ETH/USDT": {"bid": 1_999.0, "ask": 2_001.0, "last": 2_000.0}}
        self.candles = []  # 1m candles [timestamp, open, high, low, close, volume] for paper stop checks
        # 100 closed 15m candles drifting up to 50_000, served for any symbol when no `since` is given
        self.history = [[T0_MS - (100 - i) * 900_000, p, p + 10, p - 10, p, 1.0]
                        for i, p in enumerate(range(49_010, 50_010, 10))]

    def milliseconds(self):
        return self.now

    def parse_timeframe(self, timeframe):
        return {"1m": 60, "15m": 900}[timeframe]

    def load_markets(self):
        return {}

    def fetch_ticker(self, symbol):
        return self.tickers[symbol]

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        if since is None:
            return self.history
        return [c for c in self.candles if c[0] >= since]

    def amount_to_precision(self, symbol, amount):
        result = math.floor(round(amount * 1e6, 6)) / 1e6
        if result == 0:
            raise ccxt.InvalidOrder("amount below precision")
        return str(result)
