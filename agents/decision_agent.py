"""Decision agent (reasoning model): combines the three reports into BUY, SELL or HOLD."""
import json

from agents.schemas import Decision, MarketReport, RiskAdvice, SentimentReport

SYSTEM = ("You are the decision agent of a spot crypto trading bot. Combine the market, sentiment and risk "
          "reports into one decision for one symbol from the allowlist.\n"
          "- BUY: size is the fraction from 0 to 1 of the maximum position size. stop_loss is required and must "
          "be below the current price. take_profit is optional.\n"
          "- SELL: only a symbol that is held. size is the fraction from 0 to 1 of the amount held.\n"
          "- HOLD when signals are weak, conflicting or unclear. Every trade pays fees, so trade only with a "
          "clear edge.\n"
          "Follow the risk advice. Code enforces hard limits after you and rejects orders that break them.")


def hold(reason: str) -> Decision:
    return Decision(action="HOLD", reasoning=reason)


def decide(client, market: dict[str, MarketReport | None], sentiment: dict[str, SentimentReport | None],
           advice: RiskAdvice | None, account: dict) -> Decision:
    """Any missing or invalid input report, or an invalid reply, gives HOLD."""
    failed = [f"market {s}" for s, r in market.items() if r is None] + \
             [f"sentiment {s}" for s, r in sentiment.items() if r is None] + (["risk"] if advice is None else [])
    if failed:
        return hold(f"invalid agent output: {', '.join(failed)}")
    user = json.dumps({
        "account": account,
        "market": {s: r.model_dump() for s, r in market.items()},
        "sentiment": {s: r.model_dump() for s, r in sentiment.items()},
        "risk_advice": advice.model_dump(),
    })
    return client.ask("reasoning", SYSTEM, user, Decision) or hold("invalid decision output")


def to_order(decision: Decision, advice: RiskAdvice, equity: float, positions: dict[str, float],
             prices: dict[str, float], max_position_pct: float, max_order_usdt: float | None = None) -> dict | None:
    """Turn a decision into an order for the executor, which still runs it past the risk guard.

    positions: symbol -> base amount held. None means no trade.
    """
    if decision.action == "HOLD":
        return None
    if decision.action == "SELL":
        amount = decision.size * (positions.get(decision.symbol) or 0.0)
        return {"symbol": decision.symbol, "side": "sell", "amount": amount} if amount > 0 else None
    price = prices.get(decision.symbol)
    size = min(decision.size, advice.max_size)  # the risk advisor can only shrink a buy
    if not price or size <= 0:
        return None
    budget = max_position_pct * equity if max_order_usdt is None else min(max_position_pct * equity, max_order_usdt)
    return {"symbol": decision.symbol, "side": "buy", "amount": size * budget / price,
            "stop_loss": decision.stop_loss, "take_profit": decision.take_profit}
