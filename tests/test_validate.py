import pytest

import validate
from agents import market_agent
from agents.schemas import Decision, MarketReport, RiskAdvice, SentimentReport
from tests.conftest import FakeMarket
from validate import FAIL, PASS, WARN

SETTINGS = {"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "15m", "live_trading": False, "paper_trading": True,
            "paper": {"starting_cash": 100, "fee_pct": 0.001},
            "risk": {"max_position_pct": 0.15, "max_order_usdt": 15, "max_total_exposure_pct": 0.30,
                     "max_daily_loss_pct": 0.03, "max_drawdown_pct": 0.10, "max_orders_per_hour": 4,
                     "kill_switch_file": "unused"}}
ACCT = validate.account(validate.scenario_settings(SETTINGS))
HELD = validate.account(validate.scenario_settings(SETTINGS), held={"BTC/USDT": 0.00025})


def test_made_up_charts_have_the_intended_shape():
    up, down = market_agent.indicators(validate.candles("up")), market_agent.indicators(validate.candles("down"))
    assert up["ema_20"] > up["ema_50"] and up["return_20"] > 0
    assert down["ema_20"] < down["ema_50"] and down["return_20"] < 0


def test_account_input_matches_a_real_run():
    assert ACCT["buy_budget_usdt"] == 15 and ACCT["min_order_usdt"] == 5.0 and ACCT["allowlist"] == list(validate.PRICES)
    assert HELD["position_values"]["BTC/USDT"] == pytest.approx(15.0)


@pytest.mark.parametrize("rep, expected, strict, status", [
    (None, "up", True, FAIL),
    (MarketReport(trend="up", strength=0.8, support=61_000, resistance=60_000, summary=""), "up", True, FAIL),
    (MarketReport(trend="up", strength=0.8, support=30_000, resistance=61_000, summary=""), "up", True, FAIL),
    (MarketReport(trend="down", strength=0.8, support=59_000, resistance=61_000, summary=""), "up", True, FAIL),
    (MarketReport(trend="up", strength=0.3, support=59_000, resistance=61_000, summary=""), "sideways", False, WARN),
    (MarketReport(trend="up", strength=0.8, support=59_000, resistance=61_000, summary=""), "up", True, PASS),
])
def test_judge_market(rep, expected, strict, status):
    assert validate.judge_market(rep, {"close": 60_000}, expected, strict)[0] == status


@pytest.mark.parametrize("score, expected, status", [
    (0.5, "positive", PASS), (0.1, "positive", WARN), (-0.1, "positive", FAIL),
    (-0.5, "negative", PASS), (-0.1, "negative", WARN), (1.0, "negative", FAIL),
    (0.2, "neutral", PASS), (0.8, "neutral", WARN),
])
def test_judge_sentiment(score, expected, status):
    assert validate.judge_sentiment(SentimentReport(score=score, confidence=0.5), expected)[0] == status


def test_judge_risk():
    assert validate.judge_risk(None, ACCT, "generous")[0] == FAIL
    assert validate.judge_risk(RiskAdvice(max_size=1, reasoning=""), ACCT, "generous")[0] == PASS
    assert validate.judge_risk(RiskAdvice(max_size=0.2, reasoning=""), ACCT, "generous")[0] == WARN
    assert validate.judge_risk(RiskAdvice(max_size=0.2, reasoning=""), HELD, "cautious")[0] == PASS
    assert validate.judge_risk(RiskAdvice(max_size=0.4, reasoning=""), HELD, "cautious")[0] == WARN
    assert validate.judge_risk(RiskAdvice(max_size=0.9, reasoning=""), HELD, "cautious")[0] == FAIL
    assert validate.judge_risk(RiskAdvice(max_size=0.1, reduce=["BTC"], reasoning=""), HELD, "cautious")[0] == PASS
    assert validate.judge_risk(RiskAdvice(max_size=0.1, reduce=["ETH/USDT"], reasoning=""), HELD, "cautious")[0] == FAIL


def buy(stop=58_000.0, size=1.0, symbol="BTC/USDT", tp=None):
    return Decision(action="BUY", symbol=symbol, size=size, stop_loss=stop, take_profit=tp, reasoning="r")


@pytest.mark.parametrize("decision, acct, status", [
    (Decision(action="HOLD", reasoning="invalid decision output"), ACCT, FAIL),
    (buy(stop=61_000), ACCT, FAIL),  # stop above the price
    (buy(stop=20_000), ACCT, FAIL),  # stop far below
    (buy(tp=59_000), ACCT, FAIL),  # take profit below the price
    (buy(symbol="DOGE/USDT"), ACCT, FAIL),
    (Decision(action="SELL", symbol="BTC/USDT", size=1, reasoning="r"), ACCT, FAIL),  # nothing held
    (buy(size=0.2), ACCT, WARN),  # $3, under the $5 minimum
    (buy(symbol="ETH/USDT", stop=1_950.0), ACCT, WARN),  # expected BTC
    (Decision(action="HOLD", reasoning="r"), ACCT, WARN),  # expected a buy
    (buy(), ACCT, PASS),
])
def test_judge_decision_for_a_buy_situation(decision, acct, status):
    assert validate.judge_decision(decision, acct, {"BUY"}, {"SELL"}, "BTC/USDT")[0] == status


def test_judge_decision_forbidden_and_sell():
    assert validate.judge_decision(buy(), ACCT, {"HOLD"}, {"BUY"}, None)[0] == FAIL
    sell = Decision(action="SELL", symbol="BTC/USDT", size=1, reasoning="r")
    assert validate.judge_decision(sell, HELD, {"SELL"}, {"BUY"}, "BTC/USDT")[0] == PASS


class NoAnswer:
    def ask(self, tier, system, user, schema):
        return None


def test_every_situation_fails_without_valid_answers():
    results = list(validate.agent_checks(NoAnswer(), SETTINGS, repeat=1))
    assert len(results) == len(validate.MARKET_CASES) + len(validate.SENTIMENT_CASES) + 2 + 4
    assert {r[2] for r in results} == {FAIL}


def test_money_rules_pass_with_100_dollars_and_15_dollar_trades(tmp_path):
    results = list(validate.money_rules(SETTINGS, FakeMarket(), tmp_path))
    assert [r[2] for r in results] == [PASS] * 4, results


def test_money_rules_catch_trades_too_small_to_place(tmp_path):
    tiny = {**SETTINGS, "paper": {**SETTINGS["paper"], "starting_cash": 10}, "risk": {**SETTINGS["risk"], "max_position_pct": 0.02}}
    results = list(validate.money_rules(tiny, FakeMarket(), tmp_path))
    assert results[0][2] == FAIL and "under the $5 minimum" in results[0][3]
