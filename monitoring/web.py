"""Read-only report page: recent runs with each agent's report, the decision and what the executor did.

It reads the run records the bot writes to logs/bot.jsonl, so it needs nothing from the running bot.
Headlines and model text are untrusted: everything is HTML-escaped and the page runs no scripts.

main.py serves it alongside the bot at http://127.0.0.1:8080 (REPORT_HOST / REPORT_PORT to change).
Standalone: .venv/bin/python -m monitoring.web
"""
import html
import json
import os
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

RUN_MARK = '"msg": "run: '
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#5b6472;--line:#dde1e7;--buy:#0a7a3d;--sell:#b42318;
--hold:#5b6472;--warn-bg:#fff4e5;--warn:#8a4b00}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f26;--text:#e6e9ee;--muted:#9aa4b2;
--line:#2c333d;--buy:#4ade80;--sell:#f87171;--hold:#9aa4b2;--warn-bg:#3a2a12;--warn:#fbbf6a}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:1.4rem;margin:0 0 4px}h3{font-size:1rem;margin:16px 0 6px}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0 20px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:140px}
.stat b{display:block;font-size:1.2rem}.muted{color:var(--muted)}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:0 0 10px;padding:0 14px}
summary{cursor:pointer;padding:12px 0;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.badge{font-weight:700;border-radius:4px;padding:1px 8px;border:1px solid currentColor}
.BUY{color:var(--buy)}.SELL{color:var(--sell)}.HOLD,.HALTED{color:var(--hold)}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:6px;padding:8px 12px;margin:10px 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;margin-bottom:8px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:.85rem}
nav a{color:inherit;margin-right:10px}
"""


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


def e(value) -> str:
    return "–" if value is None else html.escape(str(value))


def pct(value) -> str:
    return "–" if value is None else f"{value:.2%}"


def money(value) -> str:
    return "–" if value is None else f"{value:,.2f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Cells must already be escaped."""
    head = "".join(f"<th scope='col'>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def when(ts) -> str:
    return e(f"{ts[:16].replace('T', ' ')} UTC" if ts else None)  # log times are UTC ISO strings


def action_of(run: dict) -> str:
    return "HALTED" if run.get("halts") else (run.get("decision") or {}).get("action") or "?"


def render_run(run: dict, expanded: bool) -> str:
    symbols = list(run.get("prices") or {})
    acct = run.get("account") or {}
    decision, result, order = run.get("decision") or {}, run.get("result") or {}, run.get("order") or {}
    action = action_of(run)
    title = f"{e(decision.get('symbol'))}" if decision.get("symbol") else ""
    parts = [f"<summary><span class='badge {e(action)}'>{e(action)}</span> {title}"
             f"<span class='muted'>{when(run.get('ts'))}</span>"
             + (f"<span>order {e(result.get('status'))}</span>" if result else "") + "</summary>"]

    warnings = [f"Halted: {e(h)}" for h in run.get("halts") or []] + [f"Warning: {e(i)}" for i in run.get("issues") or []]
    if warnings:
        parts.append("<div class='warn'>" + "<br>".join(warnings) + "</div>")

    held = ", ".join(f"{e(s)} {e(a)}" for s, a in (acct.get("positions") or {}).items() if a) or "none"
    parts.append("<h3>Account</h3>" + table(
        ["Equity", "Exposure", "Drawdown", "Today", "Held"],
        [[money(acct.get("equity")), pct(acct.get("exposure_pct")), pct(acct.get("drawdown_pct")),
          pct(acct.get("daily_pnl_pct")), held]]))

    if "market" in run:  # absent when the run was halted before the agents
        market, sentiment = run.get("market") or {}, run.get("sentiment") or {}
        invalid = "<span class='warn'>invalid output</span>"
        rows = []
        for s in symbols:
            m = market.get(s)
            rows.append([e(s), money((run.get("prices") or {}).get(s))] + (
                [e(m["trend"]), e(m["strength"]), money(m["support"]), money(m["resistance"]), e(m["summary"])]
                if m else [invalid, "", "", "", ""]))
        parts.append("<h3>Market agent</h3>" + table(
            ["Symbol", "Price", "Trend", "Strength", "Support", "Resistance", "Summary"], rows))

        rows = []
        for s in symbols:
            sr, news = sentiment.get(s), (run.get("headlines") or {}).get(s) or []
            items = "".join(f"<li>{e(h)}</li>" for h in news)
            headlines = f"<details><summary>{len(news)} headlines</summary><ul>{items}</ul></details>" if news \
                else "no headlines"
            rows.append([e(s)] + ([e(sr["score"]), e(sr["confidence"])] if sr else [invalid, ""]) + [headlines])
        parts.append("<h3>Sentiment agent</h3>" + table(["Symbol", "Score (-1 to 1)", "Confidence", "News"], rows))

        advice = run.get("advice")
        parts.append("<h3>Risk advisor</h3>" + (table(
            ["Max size", "Reduce", "Reasoning"],
            [[e(advice["max_size"]), e(", ".join(advice.get("reduce") or []) or "none"), e(advice["reasoning"])]])
            if advice else invalid))

        parts.append("<h3>Decision agent</h3>" + table(
            ["Action", "Symbol", "Size", "Stop loss", "Take profit", "Reasoning"],
            [[e(decision.get("action")), e(decision.get("symbol")), e(decision.get("size")),
              e(decision.get("stop_loss")), e(decision.get("take_profit")), e(decision.get("reasoning"))]]))

    if order or result:
        placed = result.get("order") or {}
        notes = "; ".join(result.get("reasons") or []) or result.get("error") or "; ".join(result.get("issues") or [])
        parts.append("<h3>Risk guard and execution</h3>" + table(
            ["Order", "Status", "Filled", "Price", "Notes"],
            [[e(f"{order.get('side', '')} {order.get('amount') or 0:.6g} {order.get('symbol', '')}"),
              e(result.get("status")), e(placed.get("filled")), e(placed.get("average")), e(notes or None)]]))

    return f"<details{' open' if expanded else ''}>{''.join(parts)}</details>"


def page(runs: list[dict]) -> str:
    latest = runs[0] if runs else {}
    acct = latest.get("account") or {}
    trades = sum((r.get("result") or {}).get("status") in ("filled", "partial") for r in runs)
    stats = "".join(f"<div class='stat'><span class='muted'>{label}</span><b>{value}</b></div>" for label, value in [
        ("Equity", money(acct.get("equity"))), ("Drawdown", pct(acct.get("drawdown_pct"))),
        ("Today", pct(acct.get("daily_pnl_pct"))), ("Status", "HALTED" if latest.get("halts") else "active"),
        (f"Trades in last {len(runs)} runs", trades), ("Last run", when(latest.get("ts")))])
    nav = "".join(f"<a href='/?n={k}'>last {k}</a>" for k in (20, 50, 200))
    body = "".join(render_run(r, i == 0) for i, r in enumerate(runs)) or "<p>No runs logged yet.</p>"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv='refresh' content='60'><title>Trading bot report</title><style>{CSS}</style></head>"
            f"<body><main><h1>Trading bot report</h1><nav>{nav}</nav><div class='stats'>{stats}</div>"
            f"{body}</main></body></html>")


class Handler(BaseHTTPRequestHandler):
    log_path = Path(os.getenv("REPORT_LOG", "logs/bot.jsonl"))

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        try:
            n = min(max(int(parse_qs(url.query).get("n", ["50"])[0]), 1), 500)
        except ValueError:
            n = 50
        body = page(recent_runs(self.log_path, n)).encode()
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
    return ThreadingHTTPServer((os.getenv("REPORT_HOST", "127.0.0.1"), int(os.getenv("REPORT_PORT", "8080"))), Handler)


def main():  # standalone, for reading logs without the bot running
    server = make_server()
    print(f"report page on http://{server.server_address[0]}:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
