"""Read-only report page, written for people new to trading: what the bot decided, why, and how the
account is doing, in plain words with simple bars. Technical detail sits behind "show more".

It reads the run records the bot writes to logs/bot.jsonl, so it needs nothing from the running bot.
Headlines and model text are untrusted: everything is HTML-escaped and the page runs no scripts.

main.py serves it alongside the bot at http://127.0.0.1:8080 (REPORT_HOST / REPORT_PORT to change).
Standalone: .venv/bin/python -m monitoring.web
"""
import base64
import hmac
import html
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from data.market import mode
from risk.risk_guard import max_buy_value

RUN_MARK = '"msg": "run: '
CSS = """
:root{--bg:#f4f6f8;--card:#fff;--text:#1b1f24;--muted:#5f6b7a;--line:#e2e6eb;--good:#0f7b3f;--good-bg:#e6f4ec;
--bad:#b3261e;--bad-bg:#fbe9e7;--wait:#55606e;--wait-bg:#eef1f4;--warn:#8a4b00;--warn-bg:#fff3e0;--accent:#2f6fde}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#191e24;--text:#e8ebef;--muted:#9aa5b1;--line:#2b323b;
--good:#5ad38d;--good-bg:#17311f;--bad:#ff8a80;--bad-bg:#3a1a18;--wait:#b6c0cc;--wait-bg:#232a32;--warn:#ffc46b;
--warn-bg:#3a2a12;--accent:#7aa7ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px 16px 40px}
h1{font-size:1.6rem;margin:0}h2{font-size:1.15rem;margin:28px 0 10px}h3{font-size:1rem;margin:0 0 10px}
.sub{color:var(--muted);margin:4px 0 0}.live{color:var(--bad);font-weight:700}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.label{color:var(--muted);font-size:.85rem}.big{font-size:1.5rem;font-weight:700;margin:2px 0}
.small{color:var(--muted);font-size:.85rem}
.good{color:var(--good)}.bad{color:var(--bad)}.wait{color:var(--wait)}
.decision{border-radius:12px;padding:16px 18px;margin-bottom:12px;border:1px solid var(--line)}
.decision.BUY{background:var(--good-bg)}.decision.SELL{background:var(--bad-bg)}
.decision.HOLD,.decision.PAUSED{background:var(--wait-bg)}
.decision .big{font-size:1.35rem}
.alert{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:10px 14px;margin:0 0 12px}
.coin{display:flex;justify-content:space-between;gap:8px;font-weight:600;margin-top:10px}
.coin:first-of-type{margin-top:0}
.bar{height:8px;border-radius:4px;background:var(--line);overflow:hidden;margin:6px 0 2px}
.bar i{display:block;height:100%;background:var(--accent)}
.gauge{position:relative;height:8px;border-radius:4px;margin:6px 0 2px;
background:linear-gradient(90deg,var(--bad),var(--line) 50%,var(--good))}
.gauge i{position:absolute;top:-4px;width:4px;height:16px;border-radius:2px;background:var(--text);margin-left:-2px}
details>summary{cursor:pointer;color:var(--accent)}
details.check{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 16px;margin-bottom:8px}
details.check>summary{color:var(--text);display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center}
details.check[open]>summary{margin-bottom:12px}
.pill{border-radius:999px;padding:1px 10px;font-weight:700;font-size:.85rem}
.pill.BUY{background:var(--good-bg);color:var(--good)}.pill.SELL{background:var(--bad-bg);color:var(--bad)}
.pill.HOLD,.pill.PAUSED{background:var(--wait-bg);color:var(--wait)}
ul{margin:6px 0 0;padding-left:20px}dt{font-weight:700;margin-top:8px}dd{margin:0}
nav a{color:var(--accent);margin-right:12px}
"""


# ---------- reading the log ----------

def recent_runs(path: Path, n: int) -> list[dict]:
    """The last n run records, newest first. A missing file or a broken line is skipped, never an error."""
    if not path.exists():
        return []
    runs = deque(maxlen=n)
    with open(path, encoding="utf-8") as f:
        for line in f:
            if RUN_MARK not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:  # e.g. the line the bot is writing right now
                continue
            runs.append({"ts": entry.get("ts"), **(entry.get("data") or {})})
    return list(reversed(runs))


# ---------- plain-language helpers ----------

def e(value) -> str:
    return "–" if value is None else html.escape(str(value))


def usd(value) -> str:
    return "–" if value is None else f"${value:,.2f}"


def coin(symbol: str) -> str:
    return e(symbol.split("/")[0])


def ago(ts: str | None, now: datetime) -> str:
    try:
        minutes = int((now - datetime.fromisoformat(ts)).total_seconds() // 60)
    except (TypeError, ValueError):
        return ""
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{minutes} min ago"
    return f"{minutes // 60} h ago" if minutes < 48 * 60 else f"{minutes // 1440} days ago"


def clock(ts: str | None, now: datetime) -> str:
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return e(ts)
    return t.strftime("%H:%M UTC") if t.date() == now.date() else t.strftime("%d %b, %H:%M UTC")


def split_reason(text: str) -> tuple[str, str]:
    """First sentence, and the rest."""
    m = re.match(r"(.+?[.!?])\s+(.*)", text or "", re.S)
    return (m.group(1), m.group(2)) if m else (text or "", "")


TREND = {"up": ("▲", "Rising", "good"), "down": ("▼", "Falling", "bad"), "sideways": ("▬", "Flat", "wait")}


def strength_word(x: float) -> str:
    return "weak" if x < 0.34 else "moderate" if x < 0.67 else "strong"


def mood(score: float) -> tuple[str, str, str]:
    if score <= -0.5:
        return "😟", "Very negative", "bad"
    if score < -0.15:
        return "🙁", "Negative", "bad"
    if score <= 0.15:
        return "😐", "Neutral", "wait"
    return ("🙂", "Positive", "good") if score < 0.5 else ("😀", "Very positive", "good")


def sure_word(confidence: float) -> str:
    return "not sure" if confidence < 0.4 else "fairly sure" if confidence < 0.7 else "confident"


def risk_word(max_size: float) -> str:
    if max_size <= 0:
        return "No new buys allowed"
    return "Small buys only" if max_size < 0.5 else "Medium buys allowed" if max_size < 1 else "Full-size buys allowed"


def plain_halt(reason: str) -> str:
    if "drawdown" in reason:
        return "Paused for safety: the account fell too far below its best value. Review, then delete the kill file."
    if "daily loss" in reason:
        return "Paused until tomorrow (UTC): today's losses reached the daily limit."
    if reason.startswith("kill switch on:"):
        return f"Paused by the kill switch: {reason.split(':', 1)[1].strip()}"
    return f"Paused: {reason}"


def plain_issue(issue: str) -> str:
    if "no stop loss" in issue:
        return f"{issue.split(':')[0]} has no safety exit (stop loss). Check it."
    if "not managed" in issue:
        return f"The account holds a coin the bot does not trade: {issue.split(' in account')[0]}"
    return issue


def action(run: dict) -> str:
    return "PAUSED" if run.get("halts") else (run.get("decision") or {}).get("action") or "HOLD"


def headline(run: dict) -> str:
    act, decision = action(run), run.get("decision") or {}
    symbol = decision.get("symbol") or ""
    return {"PAUSED": "Paused: no trading", "BUY": f"Buy {coin(symbol)}", "SELL": f"Sell {coin(symbol)}"}.get(
        act, "Waited: no trade this time")


def outcome(run: dict) -> str | None:
    result, order = run.get("result") or {}, run.get("order") or {}
    status = result.get("status")
    if status in ("filled", "partial"):
        placed = result.get("order") or {}
        side = "Bought" if order.get("side") == "buy" else "Sold"
        text = f"{side} {e(placed.get('filled'))} {coin(order.get('symbol', ''))} at {usd(placed.get('average'))}"
        if order.get("stop_loss"):
            text += f". Safety exit (stop loss) set at {usd(order['stop_loss'])}"
        return text + "."
    if status == "rejected":
        return "Blocked by the safety rules: " + e("; ".join(result.get("reasons") or []))
    if status in ("error", "unfilled"):
        return "The trade did not go through: " + e(result.get("error") or "nothing was filled")
    return None


# ---------- page pieces ----------

def bar(fraction: float) -> str:
    return f"<div class='bar' aria-hidden='true'><i style='width:{max(0.0, min(1.0, fraction)) * 100:.0f}%'></i></div>"


def gauge(score: float) -> str:
    return f"<div class='gauge' aria-hidden='true'><i style='left:{(max(-1.0, min(1.0, score)) + 1) * 50:.0f}%'></i></div>"


def more(label: str, body: str) -> str:
    return f"<details><summary>{label}</summary>{body}</details>" if body else ""


def check_html(run: dict) -> str:
    """One bot check: the decision, what happened, and the four agents."""
    parts = [f"<div class='alert'>{e(plain_halt(h))}</div>" for h in run.get("halts") or []]
    parts += [f"<div class='alert'>⚠️ {e(plain_issue(i))}</div>" for i in run.get("issues") or []]

    decision = run.get("decision") or {}
    reason = decision.get("reasoning") or ""
    if reason.startswith("invalid"):
        first, rest = "An AI agent did not answer properly, so the bot played safe and waited.", reason
    else:
        first, rest = split_reason(reason)
    result = outcome(run)
    parts.append(f"<div class='decision {e(action(run))}'><div class='label'>Decision</div>"
                 f"<div class='big'>{headline(run)}</div>"
                 + (f"<div>{e(first)}</div>" if first else "")
                 + more("Full reasoning", f"<p>{e(rest)}</p>" if rest else "")
                 + (f"<p><b>{result}</b></p>" if result else "") + "</div>")

    if "market" not in run:  # paused before the agents ran
        return "".join(parts)
    prices = run.get("prices") or {}
    symbols = list(prices)
    no_answer = "<div class='small bad'>No valid answer</div>"

    market = run.get("market") or {}
    rows = []
    for s in symbols:
        m = market.get(s)
        if not m:
            rows.append(f"<div class='coin'><span>{coin(s)}</span></div>{no_answer}")
            continue
        arrow, word, css = TREND.get(m["trend"], ("?", m["trend"], "wait"))
        rows.append(f"<div class='coin'><span>{coin(s)} {usd(prices.get(s))}</span>"
                    f"<span class='{css}'>{arrow} {e(word)}</span></div>{bar(m['strength'])}"
                    f"<div class='small'>Trend is {strength_word(m['strength'])}. "
                    f"Floor {usd(m['support'])}, ceiling {usd(m['resistance'])}</div>"
                    + more("Agent's note", f"<p>{e(m['summary'])}</p>"))
    cards = [f"<div class='card'><h3>📈 Price trend</h3>{''.join(rows)}</div>"]

    sentiment, headlines = run.get("sentiment") or {}, run.get("headlines") or {}
    rows = []
    for s in symbols:
        sr, news = sentiment.get(s), headlines.get(s) or []
        if not sr:
            rows.append(f"<div class='coin'><span>{coin(s)}</span></div>{no_answer}")
            continue
        face, word, css = mood(sr["score"])
        detail = "No news today" if not news else f"Based on {len(news)} headlines, {sure_word(sr['confidence'])}"
        items = "".join(f"<li>{e(h)}</li>" for h in news)
        rows.append(f"<div class='coin'><span>{coin(s)}</span><span class='{css}'>{face} {word}</span></div>"
                    f"{gauge(sr['score'])}<div class='small'>{detail}</div>"
                    + more("Headlines", f"<ul>{items}</ul>" if news else ""))
    cards.append(f"<div class='card'><h3>📰 News mood</h3>{''.join(rows)}</div>")

    advice = run.get("advice")
    if advice:
        first, rest = split_reason(advice["reasoning"])
        reduce = f"<div class='small bad'>Suggests selling some: {e(', '.join(advice['reduce']))}</div>" \
            if advice.get("reduce") else ""
        cards.append(f"<div class='card'><h3>🛡️ Risk check</h3><div class='coin'><span>"
                     f"{risk_word(advice['max_size'])}</span><span>{advice['max_size']:.0%}</span></div>"
                     f"{bar(advice['max_size'])}{reduce}<div class='small'>{e(first)}</div>"
                     + more("More", f"<p>{e(rest)}</p>" if rest else "") + "</div>")
    else:
        cards.append(f"<div class='card'><h3>🛡️ Risk check</h3>{no_answer}</div>")

    acct = run.get("account") or {}
    values = acct.get("position_values") or {}
    held = "".join(f"<div class='coin'><span>{coin(s)} {e(a)}</span><span>{usd(values.get(s))}</span></div>"
                   for s, a in (acct.get("positions") or {}).items() if a)
    cards.append(f"<div class='card'><h3>💰 Account at this time</h3><div class='big'>{usd(acct.get('equity'))}</div>"
                 + (held or "<div class='small'>No coins held: all cash</div>") + "</div>")
    parts.append(f"<div class='grid'>{''.join(cards)}</div>")
    return "".join(parts)


def overview(runs: list[dict], settings: dict, now: datetime) -> str:
    latest = runs[0]
    acct = latest.get("account") or {}
    equity = acct.get("equity")
    start = (settings.get("paper") or {}).get("starting_cash") if mode_of(settings) == "PAPER" else None
    change = ""
    if start and equity is not None:
        diff = equity - start
        css = "good" if diff > 0 else "bad" if diff < 0 else "wait"
        change = f"<div class='small {css}'>{'+' if diff >= 0 else '−'}{usd(abs(diff))} since start ({diff / start:+.2%})</div>"

    if latest.get("halts"):
        status = "<div class='big bad'>⏸ Paused</div><div class='small'>See the alert below</div>"
    else:
        status = "<div class='big good'>● Running</div><div class='small'>No safety limits hit</div>"
    every = settings.get("run_interval_minutes")
    values = acct.get("position_values") or {}
    holding = ", ".join(f"{coin(s)} {usd(values.get(s))}" for s, a in (acct.get("positions") or {}).items() if a)
    trades = sum((r.get("result") or {}).get("status") in ("filled", "partial") for r in runs)
    cards = [
        ("Account value", f"<div class='big'>{usd(equity)}</div>{change}"),
        ("Bot status", status),
        ("Last check", f"<div class='big'>{ago(latest.get('ts'), now) or '–'}</div>"
                       + (f"<div class='small'>Checks every {e(every)} minutes</div>" if every else "")),
        ("Holding now", f"<div class='big'>{holding or 'Nothing'}</div>"
                        f"<div class='small'>{trades} trade{'s' if trades != 1 else ''} in the last {len(runs)} checks</div>"),
    ]
    return "<div class='grid'>" + "".join(
        f"<div class='card'><div class='label'>{label}</div>{body}</div>" for label, body in cards) + "</div>"


GLOSSARY = [
    ("Account value", "Cash plus the current value of any coins held."),
    ("Paper trading", "Practice with pretend money on real prices. Nothing real is bought or sold."),
    ("Price trend", "Whether the price has been going up, down or sideways lately, worked out from price charts."),
    ("Floor and ceiling", "Prices where the coin recently stopped falling (floor, or support) or stopped rising "
                          "(ceiling, or resistance)."),
    ("News mood", "How good or bad today's news headlines look for the coin, from very negative to very positive."),
    ("Risk check", "How big the next buy may be. It goes down when prices swing a lot or the account is losing."),
    ("Stop loss (safety exit)", "An automatic sell if the price drops to a set level, so one bad trade cannot lose "
                                "too much."),
    ("Safety rules", "Fixed limits no AI can override, listed under \"Your money rules\". Any trade that breaks one "
                     "is blocked before it reaches the exchange."),
    ("Exchange minimum", "Binance only accepts orders worth at least about $5, and the stop loss is its own order, "
                         "so it must be worth $5 too. Smaller trades are blocked."),
    ("Paused", "The bot stopped trading because a safety limit was hit or someone turned on the kill switch."),
]


def rules_html(settings: dict, equity: float | None) -> str:
    """The money limits from settings, in dollars for the current account value."""
    risk = settings.get("risk") or {}
    if not risk or not equity:
        return ""
    per_trade, cap = max_buy_value(equity, risk), risk.get("max_order_usdt")
    rows = [
        ("Biggest single trade", usd(per_trade),
         f"{risk['max_position_pct']:.0%} of the account" + (f", never more than {usd(cap)}" if cap is not None else "")),
        ("Most money in coins at once", usd(equity * risk["max_total_exposure_pct"]),
         f"{risk['max_total_exposure_pct']:.0%} of the account"),
        ("Pause for the day after losing", usd(equity * risk["max_daily_loss_pct"]),
         f"{risk['max_daily_loss_pct']:.0%} in one day (UTC)"),
        ("Full stop after falling", usd(equity * risk["max_drawdown_pct"]),
         f"{risk['max_drawdown_pct']:.0%} below the best value; you restart it by hand"),
        ("Trades per hour, at most", e(risk.get("max_orders_per_hour")), "a brake on runaway trading"),
    ]
    warning = ""
    if per_trade < 5:
        warning = ("<div class='alert'>⚠️ The biggest trade allowed is under $5, Binance's minimum order, so the bot "
                   "cannot trade. Raise the account size or the per-trade limit in config/settings.yaml.</div>")
    cards = "".join(f"<div class='card'><div class='label'>{label}</div><div class='big'>{value}</div>"
                    f"<div class='small'>{e(note)}</div></div>" for label, value, note in rows)
    return (f"<h2>Your money rules</h2>{warning}<div class='grid'>{cards}</div>"
            "<p class='small'>Set in config/settings.yaml. Change them there and restart the bot.</p>")


def mode_of(settings: dict) -> str | None:
    try:
        return mode(settings)
    except (KeyError, TypeError):
        return None


def page(runs: list[dict], settings: dict | None = None, now: datetime | None = None) -> str:
    settings, now = settings or {}, now or datetime.now(timezone.utc)
    subtitle = {
        "PAPER": "<p class='sub'>Practice mode: pretend money, real prices. Nothing real is bought or sold.</p>",
        "TESTNET": "<p class='sub'>Test mode: orders go to Binance's test exchange with fake money.</p>",
        "LIVE": "<p class='sub live'>LIVE: this bot is trading real money.</p>",
    }.get(mode_of(settings), "")
    if runs:
        body = (overview(runs, settings, now)
                + f"<h2>Latest check · {clock(runs[0].get('ts'), now)}</h2>" + check_html(runs[0]))
        earlier = "".join(
            f"<details class='check'><summary><b>{clock(r.get('ts'), now)}</b>"
            f"<span class='pill {e(action(r))}'>{headline(r)}</span>"
            + "".join(f"<span>{coin(s)} {TREND.get(m['trend'], ('?',))[0]}</span>"
                      for s, m in (r.get("market") or {}).items() if m)
            + (f"<span class='small'>{outcome(r)}</span>" if outcome(r) else "")
            + f"</summary>{check_html(r)}</details>" for r in runs[1:])
        body += rules_html(settings, (runs[0].get("account") or {}).get("equity"))
        if earlier:
            body += "<h2>Earlier checks</h2><nav>Show the last " + "".join(
                f"<a href='/?n={k}'>{k}</a>" for k in (20, 50, 200)) + "</nav><br>" + earlier
    else:
        body = "<div class='card'>No checks yet. The bot writes one every few minutes once it is running.</div>"
    help_box = "<h2>What do these words mean?</h2><div class='card'><dl>" + "".join(
        f"<dt>{term}</dt><dd>{text}</dd>" for term, text in GLOSSARY) + "</dl></div>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='60'><title>Trading bot</title><style>{CSS}</style></head>"
            f"<body><main><h1>🤖 Trading bot</h1>{subtitle}<br>{body}{help_box}</main></body></html>")


# ---------- server ----------

def load_settings() -> dict:
    try:
        return yaml.safe_load(open("config/settings.yaml")) or {}
    except (OSError, yaml.YAMLError):
        return {}


LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MIN_PASSWORD = 12


def exposure_problem() -> str | None:
    """The page has no other protection, so opening it beyond this machine needs a strong password.

    REPORT_BIND is where Docker publishes the page on the server; REPORT_HOST where a local run listens."""
    where = os.getenv("REPORT_BIND") or os.getenv("REPORT_HOST") or "127.0.0.1"
    if where in LOOPBACK or len(os.getenv("REPORT_PASSWORD") or "") >= MIN_PASSWORD:
        return None
    return (f"the dashboard would be open to the network ({where}) without a password: set REPORT_PASSWORD "
            f"in .env to at least {MIN_PASSWORD} characters")


class Handler(BaseHTTPRequestHandler):
    log_path = Path(os.getenv("REPORT_LOG", "logs/bot.jsonl"))

    def authorized(self) -> bool:
        """HTTP Basic login when REPORT_PASSWORD is set: any username, that password."""
        password = os.getenv("REPORT_PASSWORD")
        if not password:
            return True
        scheme, _, encoded = self.headers.get("Authorization", "").partition(" ")
        try:
            given = base64.b64decode(encoded, validate=True).decode().partition(":")[2]
        except ValueError:  # covers bad base64 and bad UTF-8
            return False
        return scheme == "Basic" and hmac.compare_digest(given.encode(), password.encode())

    def do_GET(self):
        if not self.authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Trading bot", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        try:
            n = min(max(int(parse_qs(url.query).get("n", ["20"])[0]), 1), 500)
        except ValueError:
            n = 20
        body = page(recent_runs(self.log_path, n), load_settings()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # Defense in depth on top of escaping: no scripts, no outside resources.
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # page views are not worth logging


def make_server() -> ThreadingHTTPServer:
    problem = exposure_problem()
    if problem:
        raise ValueError(problem)
    return ThreadingHTTPServer((os.getenv("REPORT_HOST", "127.0.0.1"), int(os.getenv("REPORT_PORT", "8080"))), Handler)


def main():  # standalone, for reading logs without the bot running
    server = make_server()
    print(f"report page on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
