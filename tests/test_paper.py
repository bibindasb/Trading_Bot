import ccxt
import pytest

from execution.paper import PaperExchange
from tests.conftest import T0_MS, FakeMarket

SETTINGS = {"symbols": ["BTC/USDT", "ETH/USDT"], "paper": {"starting_cash": 10_000, "fee_pct": 0.001}}


@pytest.fixture
def market():
    return FakeMarket()


@pytest.fixture
def paper(market, tmp_path):
    return PaperExchange(market, SETTINGS, tmp_path / "paper.json")


def buy_with_stop(paper, market, amount=0.004, stop=49_000.0):
    paper.create_order("BTC/USDT", "market", "buy", amount)
    return paper.create_order("BTC/USDT", "market", "sell", amount, None,
                              {"stopLossPrice": stop, "clientOrderId": "tr-stop"})


def test_buy_fills_at_ask_with_fee(paper):
    order = paper.create_order("BTC/USDT", "market", "buy", 0.004, None, {"clientOrderId": "tr-1"})
    assert order["status"] == "closed" and order["filled"] == 0.004 and order["average"] == 50_010.0
    total = paper.fetch_balance()["total"]
    assert total["BTC"] == 0.004
    assert total["USDT"] == pytest.approx(10_000 - 0.004 * 50_010 * 1.001)


def test_sell_fills_at_bid_with_fee(paper):
    paper.create_order("BTC/USDT", "market", "buy", 0.004)
    paper.create_order("BTC/USDT", "market", "sell", 0.004)
    total = paper.fetch_balance()["total"]
    assert total["BTC"] == 0
    assert total["USDT"] == pytest.approx(10_000 - 0.004 * 50_010 * 1.001 + 0.004 * 49_990 * 0.999)


def test_buy_without_enough_cash_raises(paper):
    with pytest.raises(ccxt.InsufficientFunds):
        paper.create_order("BTC/USDT", "market", "buy", 1.0)
    assert paper.fetch_balance()["total"] == {"USDT": 10_000}


def test_cannot_sell_more_than_held(paper):
    with pytest.raises(ccxt.InsufficientFunds):
        paper.create_order("BTC/USDT", "market", "sell", 0.001)


def test_stop_locks_its_coins(paper, market):
    buy_with_stop(paper, market)
    balance = paper.fetch_balance()
    assert balance["free"]["BTC"] == 0 and balance["used"]["BTC"] == 0.004
    with pytest.raises(ccxt.InsufficientFunds):
        paper.create_order("BTC/USDT", "market", "sell", 0.001)


def test_stop_triggers_on_candle_low(paper, market):
    buy_with_stop(paper, market)
    cash = paper.fetch_balance()["total"]["USDT"]
    market.candles = [[T0_MS + 60_000, 49_500.0, 49_600.0, 49_000.0, 49_100.0, 1.0]]
    market.now += 120_000
    total = paper.fetch_balance()["total"]
    assert total["BTC"] == 0
    assert total["USDT"] == pytest.approx(cash + 0.004 * 49_000 * 0.999)
    assert paper.fetch_order(None, params={"clientOrderId": "tr-stop"})["status"] == "closed"


def test_gap_down_fills_at_open_below_stop(paper, market):
    buy_with_stop(paper, market)
    market.candles = [[T0_MS + 60_000, 48_000.0, 48_100.0, 47_900.0, 48_000.0, 1.0]]
    assert paper.fetch_balance() and paper.fetch_order(None, params={"clientOrderId": "tr-stop"})["average"] == 48_000.0


def test_stop_not_triggered_above_trigger_or_by_older_candles(paper, market):
    buy_with_stop(paper, market)
    market.candles = [[T0_MS - 60_000, 48_000.0, 48_000.0, 47_000.0, 48_000.0, 1.0],  # before the stop existed
                      [T0_MS + 60_000, 49_500.0, 49_600.0, 49_001.0, 49_100.0, 1.0]]
    assert paper.fetch_balance()["total"]["BTC"] == 0.004


def test_state_survives_restart(paper, market, tmp_path):
    buy_with_stop(paper, market)
    again = PaperExchange(market, SETTINGS, tmp_path / "paper.json")
    assert again.fetch_balance()["total"]["BTC"] == 0.004
    assert len(again.fetch_open_orders("BTC/USDT")) == 1


def test_fetch_and_cancel_orders(paper, market):
    stop = buy_with_stop(paper, market)
    assert paper.fetch_order(stop["id"])["clientOrderId"] == "tr-stop"
    with pytest.raises(ccxt.OrderNotFound):
        paper.fetch_order(None, params={"clientOrderId": "tr-missing"})
    paper.cancel_order(stop["id"])
    assert paper.fetch_open_orders() == []
    assert paper.fetch_balance()["free"]["BTC"] == 0.004
    with pytest.raises(ccxt.OrderNotFound):
        paper.cancel_order(stop["id"])
