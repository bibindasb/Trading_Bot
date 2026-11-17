"""Hard risk limits. Deterministic code, no AI. Every order must be approved here before execution.

Fails closed: bad input, a missing price, unreadable state or any other error rejects the order.
"""
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class Order(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    amount: Positive  # base units; its value comes from market prices, never from the order
    stop_loss: Positive | None = None
    take_profit: Positive | None = None


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


class RiskGuard:
    def __init__(self, settings: dict, state_path: str | Path = "state/risk_state.json", clock=time.time):
        self.limits = settings["risk"]
        self.allowlist = set(settings["symbols"])
        self.kill_file = Path(self.limits["kill_switch_file"])
        self.state_path = Path(state_path)
        self.clock = clock  # seconds since epoch; the backtest replays its own

    def halts(self, equity: float, now: float | None = None) -> list[str]:
        """Record equity and return why all trading is stopped (empty = not stopped). Call every run, even on HOLD."""
        try:
            state = self._load()
            reasons = self._halts(state, equity, self.clock() if now is None else now)
            self._save(state)
            return reasons
        except Exception as e:
            return [f"risk guard error: {e!r}"]

    def levels(self) -> dict:
        """Peak and start-of-day equity the limits are measured from; empty if the state can't be read."""
        try:
            state = self._load()
        except Exception:
            return {}
        return {k: state.get(k) for k in ("peak_equity", "day_start_equity")}

    def violations(self, order, equity: float, positions: dict[str, float], prices: dict[str, float],
                   now: float | None = None) -> list[str]:
        """Why this order is rejected; an empty list means approved.

        order: Order or dict. positions: symbol -> base amount held. prices: symbol -> current market price.
        """
        try:
            now = self.clock() if now is None else now
            # Validate every input before touching state, so a bad call cannot half-update it.
            order = Order.model_validate(order)
            _require(all(math.isfinite(a) and a >= 0 for a in positions.values()), "invalid position amount")
            _require(all(math.isfinite(p) and p > 0 for p in prices.values()), "invalid price")
            missing = ({order.symbol} | {s for s, a in positions.items() if a}) - set(prices)
            _require(not missing, f"no price for {sorted(missing)}")

            state = self._load()
            reasons = self._halts(state, equity, now) + self._order_rules(order, equity, positions, prices)
            recent = [t for t in state.get("order_times", []) if now - t < 3600]
            if len(recent) >= self.limits["max_orders_per_hour"]:
                reasons.append(f"{len(recent)} orders in the last hour, max {self.limits['max_orders_per_hour']}")
            if not reasons:
                recent.append(now)  # approvals use up the hourly budget, even if placement later fails
            state["order_times"] = recent
            self._save(state)
            return reasons
        except Exception as e:
            return [f"risk guard error: {e!r}"]

    def _halts(self, state: dict, equity: float, now: float) -> list[str]:
        _require(math.isfinite(equity) and equity > 0, f"invalid equity {equity!r}")
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if state.get("day") != day:
            state["day"], state["day_start_equity"] = day, equity
        if state.get("killed") and not self.kill_file.exists():
            state["killed"] = False
            state["peak_equity"] = equity  # operator cleared the drawdown kill: measure from here
        state["peak_equity"] = max(state.get("peak_equity", equity), equity)

        if equity <= state["peak_equity"] * (1 - self.limits["max_drawdown_pct"]) and not self.kill_file.exists():
            self.kill_file.parent.mkdir(parents=True, exist_ok=True)
            self.kill_file.write_text(f"max drawdown hit: equity {equity} vs peak {state['peak_equity']}\n")
            state["killed"] = True
        if equity <= state["day_start_equity"] * (1 - self.limits["max_daily_loss_pct"]):
            state["halted_day"] = day

        reasons = []
        if self.kill_file.exists():
            reasons.append(f"kill switch on: {self.kill_file.read_text().strip() or 'manual'}")
        if state.get("halted_day") == day:
            reasons.append(f"max daily loss hit, no trading until {day} ends (UTC)")
        return reasons

    def _order_rules(self, order: Order, equity: float, positions: dict, prices: dict) -> list[str]:
        reasons = []
        if order.symbol not in self.allowlist:
            reasons.append(f"{order.symbol!r} not in allowlist")
        if order.side == "sell":  # spot sells only shrink a position, so size, exposure and stop rules are for buys
            held = positions.get(order.symbol, 0)
            if order.amount > held:
                reasons.append(f"sell of {order.amount} exceeds {held} held (no shorting)")
            return reasons

        price = prices[order.symbol]
        notional = order.amount * price
        if notional > equity * self.limits["max_position_pct"]:
            reasons.append(f"position size {notional:.2f} over {self.limits['max_position_pct']:.0%} of equity")
        cap = self.limits.get("max_order_usdt")
        if cap is not None and notional > cap:
            reasons.append(f"order value {notional:.2f} over the fixed cap of {cap} per trade")
        exposure = sum(amount * prices[s] for s, amount in positions.items() if amount)
        if exposure + notional > equity * self.limits["max_total_exposure_pct"]:
            reasons.append(f"total exposure {exposure + notional:.2f} over "
                           f"{self.limits['max_total_exposure_pct']:.0%} of equity")
        if order.stop_loss is None or order.stop_loss >= price:
            reasons.append(f"buy needs a stop loss below the current price {price}")
        return reasons

    def _load(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def _save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_path)  # atomic, so a crash never leaves half-written state
