"""Market data via CCXT. Uses the exchange sandbox unless live trading is switched on twice."""
import os

import ccxt
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def is_live(settings: dict) -> bool:
    # Both switches are required: the config flag and the environment variable.
    return settings["live_trading"] is True and os.getenv("LIVE_TRADING", "").lower() == "true"


def make_exchange(settings: dict) -> ccxt.Exchange:
    exchange = getattr(ccxt, settings["exchange"])({
        "apiKey": os.getenv("EXCHANGE_API_KEY"),
        "secret": os.getenv("EXCHANGE_API_SECRET"),
        "enableRateLimit": True,
    })
    if not is_live(settings):
        exchange.set_sandbox_mode(True)
    return exchange


def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, timeframe, limit=limit), columns=COLUMNS)
    # Drop the candle still forming, so live signals match what a backtest sees.
    closed = df["timestamp"] + exchange.parse_timeframe(timeframe) * 1000 <= exchange.milliseconds()
    df = df[closed].reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def fetch_balances(exchange) -> dict:
    return {asset: amount for asset, amount in exchange.fetch_balance()["total"].items() if amount}


def fetch_all(exchange, settings: dict) -> dict:
    """Candles and ticker per configured symbol. Balances come from the executor, which knows paper mode."""
    return {
        symbol: {"ohlcv": fetch_ohlcv(exchange, symbol, settings["timeframe"]), "ticker": exchange.fetch_ticker(symbol)}
        for symbol in settings["symbols"]
    }


if __name__ == "__main__":  # manual check: .venv/bin/python -m data.market
    import yaml
    from dotenv import load_dotenv

    load_dotenv()
    settings = yaml.safe_load(open("config/settings.yaml"))
    exchange = make_exchange(settings)
    for symbol, market in fetch_all(exchange, settings).items():
        print(symbol, "last:", market["ticker"]["last"], "candles:", len(market["ohlcv"]))
    print("balances:", fetch_balances(exchange))
