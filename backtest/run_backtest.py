"""Backtest: replays historical candles through the same LangGraph workflow, agents, risk guard, executor and
paper fills (fees and slippage), then reports return, max drawdown, win rate and Sharpe ratio.

Caveats:
- News is not replayed, so sentiment is always neutral.
- An LLM may already know how past markets moved, which flatters LLM backtests. Prompts carry no dates.
- Every step that is not halted makes one market call per symbol, one risk call and one decision call.

Usage: .venv/bin/python -m backtest.run_backtest --days 7 [--end 2026-11-01]
"""
import argparse
import json
import math
import statistics
import tempfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

from execution.executor import Executor
from execution.paper import PaperExchange
from graph.workflow import build_graph
from risk.risk_guard import RiskGuard

WARMUP_CANDLES = 200  # history before the start, so indicators are ready on the first step
YEAR_MS = 365 * 24 * 3_600_000  # crypto trades every day


class HistoricalMarket:
    """Stands in for the ccxt data exchange, showing only candles that had closed by `now`."""

    def __init__(self, candles: dict[str, list], timeframe: str, slippage_pct: float, source):
        self.candles, self.slippage, self.source = candles, slippage_pct, source
        self.tf_ms = ccxt.Exchange.parse_timeframe(timeframe) * 1000
        self.times = {s: [c[0] for c in cs] for s, cs in candles.items()}
        self.now = 0

    def milliseconds(self):
        return self.now

    def seconds(self):
        return self.now / 1000

    def parse_timeframe(self, timeframe):
        return ccxt.Exchange.parse_timeframe(timeframe)

    def load_markets(self):
        return self.source.load_markets()

    def amount_to_precision(self, symbol, amount):
        return self.source.amount_to_precision(symbol, amount)

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        # Serves the backtest timeframe whatever is asked; paper stop checks ask for 1m but only need the lows.
        times = self.times[symbol]
        end = bisect_right(times, self.now - self.tf_ms)  # closed candles only: no peeking ahead
        start = bisect_left(times, since) if since is not None else 0
        closed = self.candles[symbol][start:end]
        return closed[-limit:] if limit else closed

    def fetch_ticker(self, symbol):
        price = self.fetch_ohlcv(symbol, None, limit=1)[-1][4]
        return {"last": price, "bid": price * (1 - self.slippage), "ask": price * (1 + self.slippage)}


def download_candles(exchange, symbol: str, timeframe: str, since_ms: int, until_ms: int) -> list:
    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    candles = []
    while since_ms < until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
        if not batch:
            break
        candles += [c for c in batch if c[0] < until_ms]
        since_ms = batch[-1][0] + tf_ms
    return candles


def realized_pnls(orders: list, fee_pct: float) -> list[float]:
    """Profit or loss of every sell, including stop losses, against the average cost of what was held."""
    held, cost, pnls = defaultdict(float), defaultdict(float), []
    fills = [o for o in orders if o["status"] == "closed" and o["filled"]]
    for o in sorted(fills, key=lambda o: o["lastTradeTimestamp"]):  # a stop fills long after it was placed
        s, qty, value = o["symbol"], o["filled"], o["filled"] * o["average"]
        if o["side"] == "buy":
            held[s] += qty
            cost[s] += value * (1 + fee_pct)
        elif held[s] > 0:
            basis = cost[s] * min(qty, held[s]) / held[s]
            pnls.append(value * (1 - fee_pct) - basis)
            held[s] -= qty
            cost[s] -= basis
    return pnls


def report(curve: list[float], orders: list, fee_pct: float, tf_ms: int) -> dict:
    returns = [b / a - 1 for a, b in zip(curve, curve[1:])]
    peak, max_drawdown = curve[0], 0.0
    for equity in curve:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1 - equity / peak)
    sd = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    pnls = realized_pnls(orders, fee_pct)
    return {
        "start_equity": curve[0],
        "end_equity": curve[-1],
        "return_pct": curve[-1] / curve[0] - 1,
        "max_drawdown_pct": max_drawdown,
        "sharpe": statistics.fmean(returns) / sd * math.sqrt(YEAR_MS / tf_ms) if sd else 0.0,
        "trades": len(pnls),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
    }


def run_backtest(settings: dict, client, candles: dict[str, list], source, start_ms: int, end_ms: int,
                 workdir: Path) -> dict:
    """Step from start_ms to end_ms one candle at a time. workdir must be empty: it holds the simulated state."""
    workdir = Path(workdir)
    settings = {**settings, "risk": {**settings["risk"], "kill_switch_file": str(workdir / "KILL")}}
    market = HistoricalMarket(candles, settings["timeframe"], settings["backtest"]["slippage_pct"], source)
    paper = PaperExchange(market, settings, workdir / "paper.json")
    executor = Executor(paper, RiskGuard(settings, workdir / "risk.json", clock=market.seconds), settings)
    graph = build_graph(settings, market, executor, client, news=lambda s: {})

    curve = []
    for now in range(start_ms, end_ms + 1, market.tf_ms):
        market.now = now
        curve.append(graph.invoke({})["account"]["equity"])
    # Value the account once more so the last step's trade counts.
    curve.append(executor.account({s: market.fetch_ticker(s)["last"] for s in settings["symbols"]})[0])
    return report(curve, paper.state["orders"], settings["paper"]["fee_pct"], market.tf_ms)


def main():
    import yaml
    from dotenv import load_dotenv

    from data.market import make_public_exchange
    from llm.client import LLMClient
    from monitoring.logger import setup_logging

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=float, default=7)
    parser.add_argument("--end", help="UTC date, default now")
    args = parser.parse_args()

    load_dotenv()
    setup_logging("logs/backtest.jsonl")
    settings = yaml.safe_load(open("config/settings.yaml"))
    source = make_public_exchange(settings)
    source.load_markets()
    tf_ms = source.parse_timeframe(settings["timeframe"]) * 1000
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    end_ms = int(end.timestamp() * 1000) // tf_ms * tf_ms
    start_ms = int((end - timedelta(days=args.days)).timestamp() * 1000) // tf_ms * tf_ms
    steps = (end_ms - start_ms) // tf_ms + 1
    print(f"{steps} steps, up to {steps * (len(settings['symbols']) + 2)} model calls", flush=True)

    candles = {s: download_candles(source, s, settings["timeframe"], start_ms - WARMUP_CANDLES * tf_ms, end_ms)
               for s in settings["symbols"]}
    with tempfile.TemporaryDirectory() as workdir:
        result = run_backtest(settings, LLMClient(settings), candles, source, start_ms, end_ms, Path(workdir))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
