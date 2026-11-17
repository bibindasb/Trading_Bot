import pytest

from risk.risk_guard import RiskGuard

SETTINGS = {
    "symbols": ["BTC/USDT", "ETH/USDT"],
    "risk": {"max_position_pct": 0.02, "max_total_exposure_pct": 0.20, "max_daily_loss_pct": 0.03,
             "max_drawdown_pct": 0.10, "max_orders_per_hour": 4},
}
PRICES = {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0}
DAY = 86_400
T0 = 1_799_971_200 + 3_600  # 01:00 UTC


def make_guard(tmp_path, **risk):
    settings = {**SETTINGS, "risk": {**SETTINGS["risk"], "kill_switch_file": str(tmp_path / "KILL"), **risk}}
    return RiskGuard(settings, tmp_path / "risk_state.json")


@pytest.fixture
def guard(tmp_path):
    return make_guard(tmp_path)


def buy(amount=0.004, stop=49_000.0, symbol="BTC/USDT"):  # 0.004 BTC = 200 = exactly 2% of 10k
    return {"symbol": symbol, "side": "buy", "amount": amount, "stop_loss": stop}


def sell(amount, symbol="BTC/USDT"):
    return {"symbol": symbol, "side": "sell", "amount": amount}


def check(guard, order, equity=10_000.0, positions=None, prices=PRICES, now=T0):
    return guard.violations(order, equity, positions or {}, prices, now=now)


def rejected(reasons, text):
    return any(text in r for r in reasons)


# --- position size ---

def test_buy_at_exact_size_limit_approved(guard):
    assert check(guard, buy()) == []


def test_buy_over_size_limit_rejected(guard):
    assert rejected(check(guard, buy(amount=0.0041)), "position size")


def test_lying_about_price_cannot_shrink_position(guard):
    assert rejected(check(guard, {**buy(amount=1.0), "price": 1.0}), "position size")


# --- total exposure ---

def test_exposure_at_limit_approved(guard):
    assert check(guard, buy(), positions={"ETH/USDT": 0.9}) == []  # 1800 + 200 = 20%


def test_exposure_over_limit_rejected(guard):
    assert rejected(check(guard, buy(), positions={"ETH/USDT": 0.91}), "total exposure")


def test_positions_outside_allowlist_still_count(guard):
    prices = {**PRICES, "SOL/USDT": 100.0}
    assert rejected(check(guard, buy(), positions={"SOL/USDT": 19.0}, prices=prices), "total exposure")


def test_held_position_without_price_rejected(guard):
    assert rejected(check(guard, buy(), positions={"SOL/USDT": 1.0}), "no price")


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("-inf")])
def test_bad_position_cannot_hide_exposure(guard, bad):
    assert rejected(check(guard, buy(), positions={"ETH/USDT": 2.0, "BTC/USDT": bad}), "invalid position")


# --- mandatory stop loss ---

@pytest.mark.parametrize("stop", [None, 50_000.0, 60_000.0])
def test_buy_needs_stop_below_price(guard, stop):
    assert rejected(check(guard, buy(stop=stop)), "stop loss")


@pytest.mark.parametrize("stop", [0, -1, float("nan"), float("inf"), "low"])
def test_malformed_stop_rejected(guard, stop):
    assert rejected(check(guard, buy(stop=stop)), "risk guard error")


# --- allowlist ---

@pytest.mark.parametrize("symbol", ["DOGE/USDT", "btc/usdt", "BTC/USDT ", ""])
def test_symbol_outside_allowlist_rejected(guard, symbol):
    assert rejected(check(guard, buy(symbol=symbol), prices={**PRICES, symbol: 50_000.0}), "allowlist")


def test_sell_outside_allowlist_rejected(guard):
    prices = {**PRICES, "DOGE/USDT": 0.1}
    assert rejected(check(guard, sell(10, "DOGE/USDT"), positions={"DOGE/USDT": 10}, prices=prices), "allowlist")


# --- sells ---

def test_sell_of_held_amount_approved_without_stop(guard):
    assert check(guard, sell(0.5, "ETH/USDT"), positions={"ETH/USDT": 0.5}) == []


@pytest.mark.parametrize("positions", [{}, {"ETH/USDT": 0.0}, {"ETH/USDT": 0.4}])
def test_sell_more_than_held_rejected(guard, positions):
    assert rejected(check(guard, sell(0.5, "ETH/USDT"), positions=positions), "no shorting")


# --- malformed input fails closed ---

@pytest.mark.parametrize("order", [
    {**buy(), "amount": 0}, {**buy(), "amount": -1}, {**buy(), "amount": float("nan")},
    {**buy(), "amount": float("inf")}, {**buy(), "side": "BUY"}, {**buy(), "side": "short"},
    {"symbol": "BTC/USDT"}, None, "buy BTC",
])
def test_malformed_order_rejected(guard, order):
    assert rejected(check(guard, order), "risk guard error")


@pytest.mark.parametrize("equity", [0, -100.0, float("nan"), float("inf")])
def test_invalid_equity_rejected(guard, equity):
    assert rejected(check(guard, buy(), equity=equity), "invalid equity")


@pytest.mark.parametrize("prices, text", [
    ({"ETH/USDT": 2_000.0}, "no price"),
    ({**PRICES, "BTC/USDT": 0.0}, "invalid price"),
    ({**PRICES, "BTC/USDT": float("nan")}, "invalid price"),
])
def test_missing_or_bad_price_rejected(guard, prices, text):
    assert rejected(check(guard, buy(), prices=prices), text)


def test_unreadable_state_fails_closed(guard, tmp_path):
    (tmp_path / "risk_state.json").write_text("{not json")
    assert rejected(check(guard, buy()), "risk guard error")
    assert rejected(guard.halts(10_000.0), "risk guard error")


# --- orders per hour ---

def test_orders_per_hour_limit_survives_restart_and_expires(tmp_path):
    guard = make_guard(tmp_path)
    for i in range(4):
        assert check(guard, buy(), now=T0 + i) == []
    assert rejected(check(guard, buy(), now=T0 + 10), "orders in the last hour")
    assert rejected(check(make_guard(tmp_path), buy(), now=T0 + 10), "orders in the last hour")
    assert check(guard, buy(), now=T0 + 3_600) == []


def test_rejected_orders_do_not_use_up_hourly_slots(guard):
    for i in range(10):
        check(guard, buy(amount=1.0), now=T0 + i)
    assert check(guard, buy(), now=T0 + 20) == []


# --- max daily loss ---

def test_daily_loss_halts_until_next_utc_day(guard):
    assert check(guard, buy(amount=0.001), equity=10_000.0, now=T0) == []
    assert check(guard, buy(amount=0.001), equity=9_701.0, now=T0 + 60) == []
    assert rejected(check(guard, buy(amount=0.001), equity=9_700.0, now=T0 + 120), "daily loss")
    assert rejected(check(guard, buy(amount=0.001), equity=10_500.0, now=T0 + 180), "daily loss")  # recovery doesn't resume
    assert rejected(check(guard, sell(0.001), equity=10_500.0, positions={"BTC/USDT": 0.01}, now=T0 + 240),
                    "daily loss")
    assert check(guard, buy(amount=0.001), equity=9_700.0, now=T0 + DAY) == []  # new day, new baseline


# --- max drawdown kill switch ---

def test_drawdown_trips_kill_switch_that_stays_on_until_cleared(tmp_path):
    guard = make_guard(tmp_path, max_daily_loss_pct=0.99)
    kill = tmp_path / "KILL"
    assert check(guard, buy(amount=0.001), equity=10_000.0) == []
    assert check(guard, buy(amount=0.001), equity=9_001.0, now=T0 + DAY) == []
    assert rejected(check(guard, buy(amount=0.001), equity=9_000.0, now=T0 + 2 * DAY), "kill switch")
    assert "drawdown" in kill.read_text()
    assert rejected(check(guard, buy(amount=0.001), equity=11_000.0, now=T0 + 3 * DAY), "kill switch")  # latched
    kill.unlink()
    assert check(guard, buy(amount=0.001), equity=9_000.0, now=T0 + 4 * DAY) == []  # operator cleared it: new peak


def test_hold_runs_track_peak_and_trip_kill_switch(tmp_path):
    guard = make_guard(tmp_path, max_daily_loss_pct=0.99)
    assert guard.halts(12_000.0, now=T0) == []
    assert rejected(guard.halts(10_700.0, now=T0 + DAY), "kill switch")
    assert (tmp_path / "KILL").exists()


# --- manual kill switch ---

def test_manual_kill_file_stops_everything(guard, tmp_path):
    kill = tmp_path / "KILL"
    kill.write_text("maintenance")
    assert rejected(check(guard, buy()), "maintenance")
    assert rejected(check(guard, sell(0.01), positions={"BTC/USDT": 0.01}), "kill switch")
    kill.unlink()
    assert check(guard, buy()) == []


def test_empty_kill_file_counts(guard, tmp_path):
    (tmp_path / "KILL").touch()
    assert rejected(check(guard, buy()), "kill switch on: manual")


# --- fixed dollar cap per trade ---

def test_fixed_cap_rejects_buys_above_it(tmp_path):
    guard = make_guard(tmp_path, max_order_usdt=10)
    assert check(guard, buy(amount=0.0002)) == []  # $10, exactly the cap
    assert rejected(check(guard, buy(amount=0.00021)), "fixed cap of 10")  # $10.50


def test_fixed_cap_does_not_block_sells(tmp_path):
    guard = make_guard(tmp_path, max_order_usdt=10)
    assert check(guard, sell(0.01), positions={"BTC/USDT": 0.01}) == []  # $500 sell
