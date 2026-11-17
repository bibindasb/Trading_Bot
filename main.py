"""Entry point: runs the trading workflow every run_interval_minutes, plus a daily Telegram summary.

Modes:
- PAPER (default): real market prices, simulated fills. No exchange keys needed.
- TESTNET (paper_trading: false): real orders on the exchange sandbox.
- LIVE: needs live_trading: true in config AND LIVE_TRADING=true in the environment.
"""
import logging
import signal
import threading
from datetime import datetime, timezone

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from data.market import make_exchange, make_public_exchange, mode
from execution.executor import make_executor
from graph.workflow import build_graph, make_checkpointer, run_once
from llm.client import LLMClient
from monitoring.alerts import Alerter
from monitoring.logger import setup_logging
from monitoring.web import make_server

log = logging.getLogger("main")


def make_market(settings: dict):
    """Paper trading reads real prices through a keyless exchange that cannot trade; other modes use the keys."""
    return make_public_exchange(settings) if mode(settings) == "PAPER" else make_exchange(settings)


def daily_summary(settings: dict, market, executor) -> str:
    prices = {s: market.fetch_ticker(s)["last"] for s in settings["symbols"]}
    equity, positions = executor.account(prices)
    levels = executor.guard.levels()
    lines = [f"DAILY SUMMARY ({mode(settings)})", f"Equity: {equity:,.2f} {executor.quote}"]
    if levels.get("day_start_equity"):
        lines.append(f"Today: {equity / levels['day_start_equity'] - 1:+.2%}")
    if levels.get("peak_equity"):
        lines.append(f"Drawdown from peak: {1 - equity / levels['peak_equity']:.2%}")
    lines += [f"{s}: {a} (~{a * prices[s]:,.2f})" for s, a in positions.items() if a]
    halts = executor.guard.halts(equity)
    lines.append("Trading HALTED: " + "; ".join(halts) if halts else "Trading active")
    return "\n".join(lines)


def start_report(alerter):
    """Serve the report page from this process. If it cannot start, trading carries on without it."""
    try:
        server = make_server()
    except OSError as e:
        log.error("report page not started: %r", e)
        alerter.send(f"WARNING: report page not started: {e!r}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True, name="report").start()
    log.info("report page on http://%s:%s", *server.server_address[:2])
    return server


def build_scheduler(settings: dict, market, executor, graph, alerter) -> BlockingScheduler:
    def trade():
        try:
            run_once(graph, alerter)
        except Exception:
            pass  # run_once has already logged and alerted it; the next run tries again

    def summary():
        try:
            alerter.send(daily_summary(settings, market, executor))
        except Exception as e:
            log.exception("daily summary failed")
            alerter.run_failed(e)

    scheduler = BlockingScheduler(timezone="UTC")
    # A few seconds after each candle closes, so the exchange has it. The first run starts at once.
    scheduler.add_job(trade, CronTrigger(minute=f"*/{settings['run_interval_minutes']}", second=10),
                      next_run_time=datetime.now(timezone.utc), max_instances=1, coalesce=True,
                      misfire_grace_time=300, id="trade")
    # Before midnight UTC, while the risk guard still measures from the start of today.
    scheduler.add_job(summary, CronTrigger(hour=23, minute=55), id="summary")
    return scheduler


def main():
    load_dotenv()
    setup_logging()
    settings = yaml.safe_load(open("config/settings.yaml"))
    market = make_market(settings)
    executor = make_executor(settings, market)
    alerter = Alerter()
    graph = build_graph(settings, market, executor, LLMClient(settings), make_checkpointer())
    scheduler = build_scheduler(settings, market, executor, graph, alerter)

    def stop(signum, frame):
        log.info("stopping: waiting for a running trade to finish")
        scheduler.shutdown(wait=True)  # never cut an order placement in half

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("starting in %s mode", mode(settings))
    start_report(alerter)
    alerter.send(f"Bot started in {mode(settings)} mode, every {settings['run_interval_minutes']} minutes")
    scheduler.start()
    alerter.send("Bot stopped")


if __name__ == "__main__":
    main()
