"""One trading run as a LangGraph flow:

fetch -> account -> [market, sentiment, risk agents in parallel] -> decide -> execute -> log
                 \\-> log  (when the risk guard has halted trading: no model calls at all)

The risk guard runs inside the executor, so no path through the graph can place an unchecked order.
State holds only plain JSON values, so every checkpoint is readable without unpickling anything.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agents import decision_agent, market_agent, risk_agent, sentiment_agent
from agents.schemas import MarketReport, RiskAdvice, SentimentReport
from data.market import fetch_all
from data.news import fetch_headlines

log = logging.getLogger(__name__)
KEEP_DAYS = 14  # checkpoints of older runs are deleted


class RunState(TypedDict, total=False):
    prices: dict[str, float]
    indicators: dict[str, dict]
    headlines: dict[str, list[str]]
    account: dict
    halts: list[str]
    issues: list[str]
    market: dict[str, dict | None]
    sentiment: dict[str, dict | None]
    advice: dict | None
    decision: dict
    order: dict | None
    result: dict | None


def _dump(report):
    return report.model_dump() if report else None


def make_checkpointer(path: str | Path = "state/checkpoints.sqlite") -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # The parallel agent nodes run in threads; SqliteSaver serializes access with its own lock.
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def build_graph(settings: dict, market, executor, client, checkpointer=None):
    """market: data-layer ccxt exchange. executor: execution.executor.Executor. client: llm.client.LLMClient."""
    symbols, guard = settings["symbols"], executor.guard

    def fetch(state):
        snapshot = fetch_all(market, settings)
        return {"prices": {s: m["ticker"]["last"] for s, m in snapshot.items()},
                "indicators": {s: market_agent.indicators(m["ohlcv"]) for s, m in snapshot.items()},
                "headlines": fetch_headlines(settings)}

    def account(state):
        prices = state["prices"]
        equity, positions = executor.account(prices)
        halts = guard.halts(equity)  # records equity for the drawdown and daily-loss limits, even on HOLD
        levels = guard.levels()
        values = {s: a * prices[s] for s, a in positions.items()}
        acct = {
            "equity": equity,
            "positions": positions,
            "position_values": values,
            "exposure_pct": sum(values.values()) / equity if equity else None,
            "drawdown_pct": 1 - equity / levels["peak_equity"] if levels.get("peak_equity") else None,
            "daily_pnl_pct": equity / levels["day_start_equity"] - 1 if levels.get("day_start_equity") else None,
            "volatility_atr_pct": {s: state["indicators"][s]["atr_pct"] for s in symbols},
            "prices": prices,
            "allowlist": symbols,
            "limits": {k: v for k, v in settings["risk"].items() if k != "kill_switch_file"},
        }
        return {"account": acct, "halts": halts, "issues": executor.reconcile()}

    def route(state):
        return "log" if state["halts"] else ["market", "sentiment", "risk"]

    def market_node(state):
        return {"market": {s: _dump(market_agent.analyze(client, s, settings["timeframe"], state["indicators"][s]))
                           for s in symbols}}

    def sentiment_node(state):
        return {"sentiment": {s: _dump(sentiment_agent.score(client, s, state["headlines"].get(s, [])))
                              for s in symbols}}

    def risk_node(state):
        return {"advice": _dump(risk_agent.advise(client, state["account"]))}

    def decide(state):
        market_reports = {s: MarketReport.model_validate(r) if r else None for s, r in state["market"].items()}
        sentiment = {s: SentimentReport.model_validate(r) if r else None for s, r in state["sentiment"].items()}
        advice = RiskAdvice.model_validate(state["advice"]) if state["advice"] else None
        decision = decision_agent.decide(client, market_reports, sentiment, advice, state["account"])
        acct = state["account"]
        order = decision_agent.to_order(decision, advice, acct["equity"], acct["positions"], state["prices"],
                                        settings["risk"]["max_position_pct"])
        return {"decision": decision.model_dump(), "order": order}

    def execute(state):
        return {"result": executor.submit(state["order"], state["prices"]) if state.get("order") else None}

    def log_run(state):
        summary = {k: state.get(k) for k in ("halts", "issues", "decision", "order", "result")}
        log.info("run: %s", json.dumps(summary, default=str), extra={"data": dict(state)})
        return {}

    g = StateGraph(RunState)
    for name, fn in [("fetch", fetch), ("account", account), ("market", market_node), ("sentiment", sentiment_node),
                     ("risk", risk_node), ("decide", decide), ("execute", execute), ("log", log_run)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "account")
    g.add_conditional_edges("account", route, ["market", "sentiment", "risk", "log"])
    g.add_edge(["market", "sentiment", "risk"], "decide")
    g.add_edge("decide", "execute")
    g.add_edge("execute", "log")
    g.add_edge("log", END)
    return g.compile(checkpointer=checkpointer)


def run_once(graph, alerter=None) -> dict:
    """Run the flow once under a new thread id; returns the final state. A failed run is alerted, then re-raised.

    A run that crashes is not resumed: its prices are stale by the next run, and a resumed execute step could
    repeat an order. Recovery comes from the exchange (executor.reconcile) and the risk guard's own state.
    """
    # Unique per run: reusing a thread id would carry the previous run's state into this one.
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    config = {"configurable": {"thread_id": run_id}}
    try:
        state = graph.invoke({}, config)
    except Exception as e:
        log.exception("run %s failed", run_id)
        if alerter:
            alerter.run_failed(e)
        raise
    if alerter:
        alerter.run_finished(state)
    if isinstance(graph.checkpointer, SqliteSaver):
        _delete_runs_before(graph.checkpointer, datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS))
    return {"run_id": run_id, **state}


def _delete_runs_before(checkpointer: SqliteSaver, cutoff: datetime) -> None:
    # Run ids sort by time. Reads the same `checkpoints` table that SqliteSaver.delete_thread deletes from.
    with checkpointer.cursor(transaction=False) as cur:
        old = [row[0] for row in cur.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id < ?",
                                             (f"run-{cutoff:%Y%m%dT%H%M%SZ}",))]
    for thread_id in old:
        checkpointer.delete_thread(thread_id)
