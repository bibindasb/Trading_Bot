import pytest

from agents.schemas import Decision, MarketReport, RiskAdvice, SentimentReport
from execution import executor as executor_module
from execution.executor import Executor
from execution.paper import PaperExchange
from graph import workflow
from risk.risk_guard import RiskGuard
from tests.conftest import FakeMarket

MARKET = MarketReport(trend="up", strength=0.6, support=49_500.0, resistance=51_000.0, summary="Uptrend.")
SENTIMENT = SentimentReport(score=0.4, confidence=0.7)
ADVICE = RiskAdvice(max_size=1.0, reasoning="Calm market.")
BUY = Decision(action="BUY", symbol="BTC/USDT", size=0.5, stop_loss=48_000.0, reasoning="Trend and news agree.")
ALL = {MarketReport: MARKET, SentimentReport: SENTIMENT, RiskAdvice: ADVICE, Decision: BUY}


class SchemaClient:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def ask(self, tier, system, user, schema):
        self.calls.append((tier, schema.__name__))
        return self.replies.get(schema)


@pytest.fixture(autouse=True)
def no_sleep_or_network(monkeypatch):
    monkeypatch.setattr(executor_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(workflow, "fetch_headlines", lambda s: {"BTC/USDT": ["Bitcoin rallies"], "ETH/USDT": []})


@pytest.fixture
def env(tmp_path):
    settings = {"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "15m", "live_trading": False,
                "paper_trading": True, "paper": {"starting_cash": 10_000, "fee_pct": 0.001},
                "risk": {"max_position_pct": 0.02, "max_total_exposure_pct": 0.20, "max_daily_loss_pct": 0.03,
                         "max_drawdown_pct": 0.10, "max_orders_per_hour": 10,
                         "kill_switch_file": str(tmp_path / "KILL")}}
    market = FakeMarket()
    paper = PaperExchange(market, settings, tmp_path / "paper.json")
    executor = Executor(paper, RiskGuard(settings, tmp_path / "risk.json"), settings)
    return settings, market, executor, paper, tmp_path


def run(env, client, checkpointer=None):
    settings, market, executor, _, _ = env
    return workflow.run_once(workflow.build_graph(settings, market, executor, client, checkpointer))


def test_full_run_buys_and_protects(env):
    client = SchemaClient(ALL)
    state = run(env, client)
    paper = env[3]
    assert state["order"]["amount"] == pytest.approx(0.5 * 0.02 * 10_000 / 50_000)
    assert state["result"]["status"] == "filled" and state["result"]["issues"] == []
    assert [(o["amount"], o["triggerPrice"]) for o in paper.fetch_open_orders("BTC/USDT")] == [(0.002, 48_000.0)]
    # Market for both coins, sentiment only where there is news, then risk and one reasoning call.
    assert sorted(client.calls) == sorted([("fast", "MarketReport")] * 2 + [("fast", "SentimentReport"),
                                          ("fast", "RiskAdvice"), ("reasoning", "Decision")])
    assert state["sentiment"]["ETH/USDT"] == {"score": 0.0, "confidence": 0.0}


def test_invalid_agent_output_holds_and_places_nothing(env):
    client = SchemaClient({**ALL, MarketReport: None})
    state = run(env, client)
    assert state["decision"]["action"] == "HOLD" and state["order"] is None and state["result"] is None
    assert ("reasoning", "Decision") not in client.calls
    assert env[3].state["orders"] == []


def test_kill_switch_skips_every_model_call(env):
    (env[4] / "KILL").write_text("manual stop")
    client = SchemaClient(ALL)
    state = run(env, client)
    assert any("manual stop" in h for h in state["halts"])
    assert client.calls == [] and "decision" not in state and env[3].state["orders"] == []


def test_second_run_sees_the_position(env):
    run(env, SchemaClient(ALL))
    state = run(env, SchemaClient({**ALL, Decision: Decision(action="HOLD")}))
    acct = state["account"]
    assert acct["positions"]["BTC/USDT"] == 0.002
    assert acct["exposure_pct"] == pytest.approx(100 / acct["equity"])
    assert acct["drawdown_pct"] == pytest.approx(1 - acct["equity"] / 10_000)
    assert "kill_switch_file" not in acct["limits"]
    assert state["result"] is None and state["issues"] == []


def test_sell_with_nothing_held_places_nothing(env):
    state = run(env, SchemaClient({**ALL, Decision: Decision(action="SELL", symbol="BTC/USDT", size=1.0)}))
    assert state["order"] is None and state["result"] is None


def test_data_failure_stops_the_run(env, monkeypatch):
    monkeypatch.setattr(env[1], "fetch_ticker", lambda s: (_ for _ in ()).throw(RuntimeError("exchange down")))
    client = SchemaClient(ALL)
    with pytest.raises(RuntimeError, match="exchange down"):
        run(env, client)
    assert client.calls == []


def test_runs_are_checkpointed_and_old_runs_deleted(env):
    settings, market, executor, _, tmp_path = env
    db = tmp_path / "checkpoints.sqlite"
    graph = workflow.build_graph(settings, market, executor, SchemaClient(ALL), workflow.make_checkpointer(db))
    old = {"configurable": {"thread_id": "run-20000101T000000Z-aaaaaa"}}
    graph.invoke({}, old)
    state = workflow.run_once(graph)

    reopened = workflow.make_checkpointer(db)
    saved = reopened.get_tuple({"configurable": {"thread_id": state["run_id"]}})
    assert saved.checkpoint["channel_values"]["result"]["status"] == "filled"
    assert reopened.get_tuple(old) is None
