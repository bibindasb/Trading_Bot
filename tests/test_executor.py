import re
from pathlib import Path

import ccxt
import pytest

from execution import executor as executor_module
from execution.executor import Executor, make_executor
from execution.paper import PaperExchange
from risk.risk_guard import RiskGuard
from tests.conftest import FakeMarket

ROOT = Path(__file__).parent.parent
PRICES = {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(executor_module.time, "sleep", lambda s: None)


@pytest.fixture
def settings(tmp_path):
    return {"symbols": ["BTC/USDT", "ETH/USDT"], "live_trading": False, "paper_trading": True,
            "paper": {"starting_cash": 10_000, "fee_pct": 0.001},
            "risk": {"max_position_pct": 0.02, "max_total_exposure_pct": 0.20, "max_daily_loss_pct": 0.03,
                     "max_drawdown_pct": 0.10, "max_orders_per_hour": 10,
                     "kill_switch_file": str(tmp_path / "KILL")}}


@pytest.fixture
def paper(settings, tmp_path):
    return PaperExchange(FakeMarket(), settings, tmp_path / "paper.json")


@pytest.fixture
def ex(paper, settings, tmp_path):
    return Executor(paper, RiskGuard(settings, tmp_path / "risk.json"), settings)


def buy(amount=0.004, stop=49_000.0):
    return {"symbol": "BTC/USDT", "side": "buy", "amount": amount, "stop_loss": stop}


def stops(paper):
    return [(o["amount"], o["triggerPrice"]) for o in paper.fetch_open_orders("BTC/USDT")]


def market_orders(paper):
    return [o for o in paper.state["orders"] if not o["triggerPrice"]]


def test_approved_buy_is_placed_and_protected(ex, paper):
    result = ex.submit(buy(), PRICES)
    assert result["status"] == "filled" and result["issues"] == []
    assert result["order"]["clientOrderId"].startswith("tr-")
    assert stops(paper) == [(0.004, 49_000.0)]
    assert ex.reconcile() == []


def test_rejected_order_places_nothing(ex, paper):
    result = ex.submit(buy(amount=1.0), PRICES)
    assert result["status"] == "rejected" and any("position size" in r for r in result["reasons"])
    assert paper.state["orders"] == [] and paper.fetch_balance()["total"] == {"USDT": 10_000}


def test_guard_checks_the_rounded_amount(ex, paper):
    assert ex.submit(buy(amount=0.0040009), PRICES)["status"] == "filled"  # 200.045 unrounded, 200 sent
    assert market_orders(paper)[0]["amount"] == 0.004


def test_malformed_order_rejected_without_raising(ex, paper):
    assert ex.submit({"symbol": "BTC/USDT", "side": "buy"}, PRICES)["status"] == "rejected"
    assert ex.submit(buy(), {})["status"] == "rejected"  # no prices
    assert paper.state["orders"] == []


def test_sell_keeps_the_rest_protected_at_the_old_stop(ex, paper):
    ex.submit(buy(), PRICES)
    result = ex.submit({"symbol": "BTC/USDT", "side": "sell", "amount": 0.001}, PRICES)
    assert result["status"] == "filled" and result["issues"] == []
    assert stops(paper) == [(0.003, 49_000.0)]


def test_buys_share_one_stop_at_the_tighter_price(ex, paper):
    ex.submit(buy(amount=0.002, stop=48_000.0), PRICES)
    ex.submit(buy(amount=0.002, stop=49_000.0), PRICES)
    assert stops(paper) == [(0.004, 49_000.0)]
    ex.submit(buy(amount=0.001, stop=40_000.0), PRICES)  # a looser stop never loosens the position
    assert stops(paper) == [(0.005, 49_000.0)]


def test_reconcile_flags_unprotected_and_unmanaged_coins(ex, paper):
    paper.state["total"].update({"BTC": 0.01, "DOGE": 100.0})
    issues = ex.reconcile()
    assert any("BTC/USDT" in i and "no stop loss" in i for i in issues)
    assert any("DOGE" in i and "not managed" in i for i in issues)


def test_unprotected_position_reported_after_sell(ex, paper):
    paper.state["total"]["BTC"] = 0.01  # held from before, with no stop
    result = ex.submit({"symbol": "BTC/USDT", "side": "sell", "amount": 0.004}, PRICES)
    assert result["status"] == "filled" and any("no stop loss" in i for i in result["issues"])


def test_timeout_after_exchange_accepted_does_not_double_the_order(ex, paper, monkeypatch):
    real = paper.create_order
    calls = []

    def accepted_then_timeout(*args):
        calls.append(args)
        order = real(*args)
        if len(calls) == 1:
            raise ccxt.RequestTimeout("timed out")
        return order

    monkeypatch.setattr(paper, "create_order", accepted_then_timeout)
    assert ex.submit(buy(), PRICES)["status"] == "filled"
    assert len(market_orders(paper)) == 1
    assert stops(paper) == [(0.004, 49_000.0)]


def test_timeout_before_exchange_resends_with_same_client_id(ex, paper, monkeypatch):
    real = paper.create_order
    sent = []

    def lost_then_ok(*args):
        sent.append(args[5]["clientOrderId"])
        if len(sent) == 1:
            raise ccxt.NetworkError("connection reset")
        return real(*args)

    monkeypatch.setattr(paper, "create_order", lost_then_ok)
    assert ex.submit(buy(), PRICES)["status"] == "filled"
    assert sent[0] == sent[1] and len(market_orders(paper)) == 1


def test_unknown_order_state_is_not_resent(ex, paper, monkeypatch):
    sent = []
    monkeypatch.setattr(paper, "create_order", lambda *a: sent.append(a) or (_ for _ in ()).throw(
        ccxt.RequestTimeout("timed out")))
    monkeypatch.setattr(paper, "fetch_order", lambda *a: (_ for _ in ()).throw(ccxt.NetworkError("down")))
    result = ex.submit(buy(), PRICES)
    assert result["status"] == "error" and len(sent) == 1


def test_exchange_rejection_is_not_retried(ex, paper, monkeypatch):
    sent = []
    monkeypatch.setattr(paper, "create_order", lambda *a: sent.append(a) or (_ for _ in ()).throw(
        ccxt.InsufficientFunds("no")))
    assert ex.submit(buy(), PRICES)["status"] == "error" and len(sent) == 1


def test_partial_fill_cancels_remainder_and_protects_what_filled(ex, paper, monkeypatch):
    real_create, cancelled = paper.create_order, []
    open_order = {"id": "x", "symbol": "BTC/USDT", "status": "open", "filled": 0.002, "amount": 0.004}

    def create(symbol, type, side, amount, price, params):
        if params.get("stopLossPrice"):
            return real_create(symbol, type, side, amount, price, params)
        paper.state["total"]["BTC"] = 0.002
        return dict(open_order)

    monkeypatch.setattr(paper, "create_order", create)
    monkeypatch.setattr(paper, "fetch_order", lambda *a: {**open_order, "status": "canceled" if cancelled else "open"})
    monkeypatch.setattr(paper, "cancel_order", lambda *a: cancelled.append(a))
    result = ex.submit(buy(), PRICES)
    assert result["status"] == "partial" and cancelled
    assert stops(paper) == [(0.002, 49_000.0)]


def test_make_executor_modes(settings, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    market = FakeMarket()
    assert isinstance(make_executor(settings, market).exchange, PaperExchange)
    assert make_executor({**settings, "paper_trading": False}, market).exchange is market  # testnet orders
    monkeypatch.setenv("LIVE_TRADING", "true")
    assert make_executor({**settings, "live_trading": True}, market).exchange is market


def test_no_module_outside_execution_calls_the_order_api():
    order_api = re.compile(r"\.(create_\w*order\w*|cancel_\w*order\w*|edit_order|private_\w*order\w*)\s*\(")
    offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                 if p.relative_to(ROOT).parts[0] not in ("execution", "tests", ".venv")
                 and order_api.search(p.read_text())]
    assert offenders == []
