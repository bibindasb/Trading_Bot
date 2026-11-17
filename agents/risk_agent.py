"""Risk advisor agent (fast model): recommends how big the next buy may be and what to trim.
It can only make trading more cautious; the hard limits live in risk/risk_guard.py."""
import json

from agents.schemas import RiskAdvice

SYSTEM = ("You are the risk advisor of a spot crypto trading bot. You get the account's equity, positions, "
          "exposure, drawdown from peak, today's profit or loss, per-symbol volatility (ATR as a fraction of "
          "price) and the hard limits. Recommend max_size: the fraction from 0 to 1 of the maximum position size "
          "that the next buy may use. Lower it as volatility, exposure or losses rise; use 0 to pause new buys. "
          "A buy is worth max_size x buy_budget_usdt at most and must reach min_order_usdt (the exchange "
          "minimum), so a value below min_order_usdt / buy_budget_usdt also stops new buys. "
          "List any held symbols that should be reduced, and explain briefly. Code enforces the hard limits "
          "separately.")


def advise(client, account: dict) -> RiskAdvice | None:
    """account: numbers computed by code. None means an invalid reply (HOLD)."""
    return client.ask("fast", SYSTEM, json.dumps(account), RiskAdvice)
