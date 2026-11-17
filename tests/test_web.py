import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from monitoring import web

TRADE_RUN = {
    "prices": {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0},
    "headlines": {"BTC/USDT": ["Bitcoin <script>alert(1)</script> rallies"], "ETH/USDT": []},
    "account": {"equity": 10_000.0, "positions": {"BTC/USDT": 0.0, "ETH/USDT": 0.0}, "exposure_pct": 0.0,
                "drawdown_pct": 0.0, "daily_pnl_pct": 0.0},
    "halts": [], "issues": [],
    "market": {"BTC/USDT": {"trend": "up", "strength": 0.7, "support": 49_000.0, "resistance": 51_000.0,
                            "summary": "Steady climb <b>bold</b>"}, "ETH/USDT": None},
    "sentiment": {"BTC/USDT": {"score": 0.4, "confidence": 0.6}, "ETH/USDT": {"score": 0.0, "confidence": 0.0}},
    "advice": {"max_size": 0.5, "reduce": [], "reasoning": "Volatility is moderate."},
    "decision": {"action": "BUY", "symbol": "BTC/USDT", "size": 0.5, "stop_loss": 48_000.0, "take_profit": None,
                 "reasoning": "Trend and news agree."},
    "order": {"symbol": "BTC/USDT", "side": "buy", "amount": 0.001, "stop_loss": 48_000.0},
    "result": {"status": "filled", "issues": [], "order": {"filled": 0.001, "average": 50_025.0}},
}
HALTED_RUN = {"prices": {"BTC/USDT": 50_000.0}, "account": {"equity": 9_000.0, "drawdown_pct": 0.1},
              "halts": ["kill switch on: max drawdown hit"], "issues": []}


def write_log(path, runs):
    lines = [json.dumps({"ts": "2026-11-17T10:00:00+00:00", "msg": "llm call", "data": {"schema": "Decision"}})]
    for i, run in enumerate(runs):
        lines.append(json.dumps({"ts": f"2026-11-17T10:{i:02d}:00+00:00", "level": "INFO", "logger": "graph.workflow",
                                 "msg": "run: {...}", "data": run}))
    lines.append('{"msg": "run: broken')
    path.write_text("\n".join(lines) + "\n")


def test_recent_runs_newest_first_limited_and_tolerant(tmp_path):
    log = tmp_path / "bot.jsonl"
    write_log(log, [TRADE_RUN, HALTED_RUN, TRADE_RUN])
    runs = web.recent_runs(log, 2)
    assert [r["ts"] for r in runs] == ["2026-11-17T10:02:00+00:00", "2026-11-17T10:01:00+00:00"]
    assert web.recent_runs(tmp_path / "missing.jsonl", 10) == []


def test_page_shows_every_agent_and_the_execution():
    html = web.page([{"ts": "t1", **TRADE_RUN}])
    for text in ["Market agent", "Sentiment agent", "Risk advisor", "Decision agent", "Risk guard and execution",
                 "51,000.00", "0.4", "Volatility is moderate.", "Trend and news agree.", "filled", "50025.0"]:
        assert text in html, text
    assert "invalid output" in html  # ETH market report was invalid


def test_untrusted_text_is_escaped():
    html = web.page([{"ts": "t1", **TRADE_RUN}])
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>bold</b>" not in html


def test_halted_run_without_agents_renders():
    html = web.page([{"ts": "t1", **HALTED_RUN}])
    assert "HALTED" in html and "max drawdown hit" in html and "Market agent" not in html


def test_empty_log():
    assert "No runs logged yet." in web.page([])


@pytest.fixture
def server(tmp_path, monkeypatch):
    log = tmp_path / "bot.jsonl"
    write_log(log, [TRADE_RUN])
    monkeypatch.setattr(web.Handler, "log_path", log)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_server_serves_the_page_with_no_scripts_allowed(server):
    with urllib.request.urlopen(server + "/?n=abc") as r:
        assert r.status == 200 and "default-src 'none'" in r.headers["Content-Security-Policy"]
        assert "Trend and news agree." in r.read().decode()


def test_server_has_no_other_paths(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(server + "/../state/risk_state.json")
    assert err.value.code == 404
