"""Order placement. The only module that calls the exchange order API, and only for orders the risk guard approves.

Every coin held is covered by one stop-loss order; the stop placed with a buy protects a position the guard
already approved, so it is not checked again.
"""
import time
import uuid

import ccxt

from data.market import is_live
from execution.paper import PaperExchange, min_order_value
from risk.risk_guard import Order, RiskGuard

RETRIES = 3
PREFIX = "tr-"  # marks orders this bot placed


def make_executor(settings: dict, market) -> "Executor":
    """market: the data-layer exchange. Paper mode simulates fills on its prices; otherwise orders go to it."""
    exchange = market if is_live(settings) or not settings["paper_trading"] else PaperExchange(market, settings)
    return Executor(exchange, RiskGuard(settings), settings)


class Executor:
    def __init__(self, exchange, guard: RiskGuard, settings: dict):
        self.exchange, self.guard = exchange, guard
        self.symbols = settings["symbols"]
        quotes = {s.split("/")[1] for s in self.symbols}
        if len(quotes) != 1:
            raise ValueError(f"all symbols must share one quote currency, got {quotes}")
        self.quote = quotes.pop()
        self._call(exchange.load_markets)

    def account(self, prices: dict) -> tuple[float, dict]:
        """Equity in the quote currency and base amounts held. Only the quote and allowlisted coins count."""
        total = self._call(self.exchange.fetch_balance)["total"]
        positions = {s: total.get(s.split("/")[0]) or 0.0 for s in self.symbols}
        equity = (total.get(self.quote) or 0.0) + sum(a * prices[s] for s, a in positions.items() if a)
        return equity, positions

    def submit(self, order, prices: dict) -> dict:
        """Check the order with the risk guard and place it only if approved. Never raises."""
        try:
            order = Order.model_validate(order)
            # The guard checks the amount actually sent, after rounding to the exchange's precision.
            sized = {**order.model_dump(), "amount": self._tradeable(order.symbol, order.amount)}
            reasons = self._below_minimum(order, sized["amount"], prices)
            if not reasons:
                equity, positions = self.account(prices)
                reasons = self.guard.violations(sized, equity, positions, prices)
        except Exception as e:
            reasons = [f"executor error before placing: {e!r}"]
        if reasons:
            return {"status": "rejected", "reasons": reasons}

        order = Order.model_validate(sized)
        stop = order.stop_loss
        result = {"status": "error"}
        try:
            stops = self._stops(order.symbol)
            if order.side == "sell":
                stop = max((o["triggerPrice"] for o in stops), default=None)
                self._cancel(stops)  # spot stops lock the coins being sold
            placed = self._settle(self._place(order.symbol, order.side, order.amount))
            filled = placed.get("filled") or 0.0
            result = {"status": "filled" if filled >= order.amount else "partial" if filled else "unfilled",
                      "order": placed}
            if order.side == "buy":  # one stop per coin covering everything held; keep the tighter one
                stop = max([stop] + [o["triggerPrice"] for o in stops])
                self._cancel(stops)
        except Exception as e:
            result["error"] = repr(e)
        result["issues"] = self._protect(order.symbol, stop)
        return result

    def reconcile(self) -> list[str]:
        """Compare the exchange with what the bot expects; return problems to alert on. Run on startup and each run."""
        try:
            balance = self._call(self.exchange.fetch_balance)
        except Exception as e:
            return [f"reconcile failed: {e!r}"]
        managed = {self.quote} | {s.split("/")[0] for s in self.symbols}
        issues = [f"{asset} {amount} in account is not managed by the bot"
                  for asset, amount in balance["total"].items() if amount and asset not in managed]
        for symbol in self.symbols:
            free = balance["free"].get(symbol.split("/")[0]) or 0.0  # coins under a stop order are not free
            if self._tradeable(symbol, free):
                issues.append(f"{symbol}: {free} held with no stop loss")
        return issues

    def _below_minimum(self, order: Order, amount: float, prices: dict) -> list[str]:
        """The exchange rejects orders under a minimum value. A buy's stop loss is its own order, so it must
        clear the minimum too, or the position would be left without one."""
        minimum = min_order_value(self.exchange, order.symbol)
        values = {"order": amount * prices[order.symbol]}
        if order.side == "buy" and order.stop_loss:
            values["stop loss"] = amount * order.stop_loss
        return [f"{what} value {value:.2f} is below the exchange minimum of {minimum}"
                for what, value in values.items() if value < minimum]

    def _protect(self, symbol: str, stop: float | None) -> list[str]:
        """Put a stop on every coin of symbol not already under one; return problems."""
        try:
            free = self._call(self.exchange.fetch_balance)["free"].get(symbol.split("/")[0]) or 0.0
            amount = self._tradeable(symbol, free)
            if not amount:
                return []
            if stop is None:
                return [f"{symbol}: {free} held with no stop loss"]
            self._place(symbol, "sell", amount, {"stopLossPrice": stop})
            return []
        except Exception as e:
            return [f"{symbol}: could not place stop loss: {e!r}"]

    def _stops(self, symbol: str) -> list[dict]:
        return [o for o in self._call(self.exchange.fetch_open_orders, symbol)
                if o.get("triggerPrice") and (o.get("clientOrderId") or "").startswith(PREFIX)]

    def _cancel(self, orders: list[dict]) -> None:
        for o in orders:
            self._call(self.exchange.cancel_order, o["id"], o["symbol"])

    def _place(self, symbol: str, side: str, amount: float, params: dict | None = None) -> dict:
        cid = PREFIX + uuid.uuid4().hex[:24]
        params = {**(params or {}), "clientOrderId": cid}
        for attempt in range(RETRIES):
            try:
                return self.exchange.create_order(symbol, "market", side, amount, None, params)
            except ccxt.NetworkError:
                time.sleep(2 ** attempt)
                try:  # the request may have reached the exchange before the connection failed
                    return self._call(self.exchange.fetch_order, None, symbol, {"clientOrderId": cid})
                except ccxt.OrderNotFound:
                    continue  # it never arrived, so resending cannot double the order
        raise ccxt.NetworkError(f"order {cid} not placed after {RETRIES} attempts")

    def _settle(self, placed: dict) -> dict:
        """Market orders normally finish at once; cancel any remainder still open so nothing lingers."""
        for _ in range(RETRIES):
            if placed.get("status") != "open":
                return placed
            time.sleep(1)
            placed = self._call(self.exchange.fetch_order, placed["id"], placed["symbol"])
        self._call(self.exchange.cancel_order, placed["id"], placed["symbol"])
        return self._call(self.exchange.fetch_order, placed["id"], placed["symbol"])

    def _tradeable(self, symbol: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except ccxt.InvalidOrder:
            return 0.0  # below the exchange's smallest amount

    def _call(self, fn, *args):
        """Retry network errors and rate limits with backoff; other errors are final."""
        for attempt in range(RETRIES):
            try:
                return fn(*args)
            except ccxt.NetworkError:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(2 ** attempt)
