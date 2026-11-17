"""Paper exchange: simulated fills on market prices, standing in for the ccxt exchange in paper mode.

Implements only the ccxt calls the executor uses. Market orders fill at the ask (buys) or bid (sells) minus a fee.
Stop-loss orders lock their coins like on a real exchange and trigger on 1-minute candle lows.
"""
import json
import os
import uuid
from pathlib import Path

import ccxt


def min_order_value(exchange, symbol: str) -> float:
    """The smallest order value the exchange accepts (Binance: 5 USDT); 0 when it publishes none."""
    try:
        return float(exchange.market(symbol)["limits"]["cost"]["min"] or 0)
    except Exception:
        return 0.0


class PaperExchange:
    def __init__(self, market, settings: dict, state_path: str | Path = "state/paper.json"):
        self.data = market  # real ccxt exchange, used only for prices and market rules
        self.fee = settings["paper"]["fee_pct"]
        self.path = Path(state_path)
        quote = settings["symbols"][0].split("/")[1]
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "total": {quote: float(settings["paper"]["starting_cash"])}, "orders": []}

    def load_markets(self):
        return self.data.load_markets()

    def amount_to_precision(self, symbol, amount):
        return self.data.amount_to_precision(symbol, amount)

    def market(self, symbol):
        return self.data.market(symbol)

    def fetch_balance(self):
        self._trigger_stops()
        total = dict(self.state["total"])
        used = {}
        for o in self._open():
            base = o["symbol"].split("/")[0]
            used[base] = used.get(base, 0.0) + o["amount"]
        return {"total": total, "used": used, "free": {a: total[a] - used.get(a, 0.0) for a in total}}

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        if type != "market":
            raise ccxt.InvalidOrder(f"paper exchange only supports market orders, got {type}")
        order = {"id": uuid.uuid4().hex, "clientOrderId": params.get("clientOrderId"), "symbol": symbol,
                 "type": type, "side": side, "amount": amount, "filled": 0.0, "remaining": amount,
                 "status": "open", "triggerPrice": params.get("stopLossPrice"),
                 "timestamp": self.data.milliseconds()}
        if side == "sell" and amount > (self.fetch_balance()["free"].get(symbol.split("/")[0]) or 0.0):
            raise ccxt.InsufficientFunds(f"paper: not enough free {symbol} to sell {amount}")
        minimum = min_order_value(self.data, symbol)
        if order["triggerPrice"] is None:
            ticker = self.data.fetch_ticker(symbol)
            price = ticker["ask" if side == "buy" else "bid"] or ticker["last"]
            if amount * price < minimum:  # like Binance, which rejects small orders
                raise ccxt.InvalidOrder(f"paper: order value {amount * price:.2f} below the minimum {minimum}")
            self._fill(order, price)
        elif side != "sell":
            raise ccxt.InvalidOrder("paper exchange only supports sell stop losses")
        elif amount * order["triggerPrice"] < minimum:
            raise ccxt.InvalidOrder(f"paper: stop order value {amount * order['triggerPrice']:.2f} below the minimum {minimum}")
        self.state["orders"].append(order)
        self._save()
        return dict(order)

    def fetch_order(self, id, symbol=None, params=None):
        cid = (params or {}).get("clientOrderId")
        for o in self.state["orders"]:
            if o["id"] == id or (cid and o["clientOrderId"] == cid):
                return dict(o)
        raise ccxt.OrderNotFound(f"paper: no order {id or cid}")

    def fetch_open_orders(self, symbol=None):
        return [dict(o) for o in self._open() if symbol in (None, o["symbol"])]

    def cancel_order(self, id, symbol=None):
        for o in self._open():
            if o["id"] == id:
                o["status"] = "canceled"
                self._save()
                return dict(o)
        raise ccxt.OrderNotFound(f"paper: no open order {id}")

    def _open(self):
        return [o for o in self.state["orders"] if o["status"] == "open"]

    def _fill(self, order, price):
        base, quote = order["symbol"].split("/")
        total, cost = self.state["total"], order["amount"] * price
        if order["side"] == "buy":
            if (total.get(quote) or 0.0) < cost * (1 + self.fee):
                raise ccxt.InsufficientFunds(f"paper: not enough {quote} for {cost:.2f}")
            total[quote] -= cost * (1 + self.fee)
            total[base] = (total.get(base) or 0.0) + order["amount"]
        else:
            total[base] -= order["amount"]
            total[quote] = (total.get(quote) or 0.0) + cost * (1 - self.fee)
        order.update(filled=order["amount"], remaining=0.0, status="closed", average=price, cost=cost,
                     lastTradeTimestamp=self.data.milliseconds())

    def _trigger_stops(self):
        now = self.data.milliseconds()
        for o in self._open():
            # Re-read one candle back so no minute is skipped, but never candles from before the stop existed.
            since = max(o["timestamp"], o.get("checked", o["timestamp"]) - 60_000)
            # shortcut: 1000 one-minute candles cover ~16h offline; a longer outage misses older wicks.
            for c in self.data.fetch_ohlcv(o["symbol"], "1m", since=since, limit=1000):
                if c[3] <= o["triggerPrice"]:
                    self._fill(o, min(o["triggerPrice"], c[1]))  # a gap down fills at the open, below the stop
                    break
            o["checked"] = now
        self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.path)  # atomic, so a crash never leaves a half-written ledger
