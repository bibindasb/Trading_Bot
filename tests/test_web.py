import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from monitoring import web

NOW = datetime(2026, 11, 17, 10, 5, tzinfo=timezone.utc)
SETTINGS = {"live_trading": False, "paper_trading": True, "run_interval_minutes": 15,
            "paper": {"starting_cash": 10_000}}
TRADE_RUN = {
    "ts": "2026-11-17T10:00:10+00:00",
    "prices": {"BTC/USDT": 50_000.0, "ETH/USDT": 2_000.0},
    "headlines": {"BTC/USDT": ["Bitcoin <script>alert(1)</script> rallies"], "ETH/USDT": []},
    "account": {"equity": 10_120.5, "positions": {"BTC/USDT": 0.002, "ETH/USDT": 0.0},
                "position_values": {"BTC/USDT": 100.0, "ETH/USDT": 0.0}},
    "halts": [], "issues": [],
    "market": {"BTC/USDT": {"trend": "up", "strength": 0.7, "support": 49_000.0, "resistance": 51_000.0,
                            "summary": "Steady climb <b>bold</b>"}, "ETH/USDT": None},
    "sentiment": {"BTC/USDT": {"score": -0.55, "confidence": 0.75}, "ETH/USDT": {"score": 0.0, "confidence": 0.0}},
    "advice": {"max_size": 0.5, "reduce": [], "reasoning": "Volatility is moderate. Exposure is low."},
    "decision": {"action": "BUY", "symbol": "BTC/USDT", "size": 0.5, "stop_loss": 48_000.0, "take_profit": None,
                 "reasoning": "Trend and news agree. Buying a small amount."},
    "order": {"symbol": "BTC/USDT", "side": "buy", "amount": 0.002, "stop_loss": 48_000.0},
    "result": {"status": "filled", "issues": [], "order": {"filled": 0.002, "average": 50_025.0}},
}
HALTED_RUN = {"ts": "2026-11-17T09:45:10+00:00", "prices": {"BTC/USDT": 50_000.0},
              "account": {"equity": 9_000.0}, "halts": ["kill switch on: max drawdown hit: equity 9000"],
              "issues": ["BTC/USDT: 0.01 held with no stop loss"]}


def write_log(path, runs):
    lines = [json.dumps({"ts": "2026-11-17T10:00:00+00:00", "msg": "llm call", "data": {"schema": "Decision"}})]
    for run in runs:
        lines.append(json.dumps({"ts": run["ts"], "level": "INFO", "logger": "graph.workflow", "msg": "run: {...}",
                                 "data": {k: v for k, v in run.items() if k != "ts"}}))
    lines.append('{"msg": "run: broken')
    path.write_text("\n".join(lines) + "\n")


def test_recent_runs_newest_first_limited_and_tolerant(tmp_path):
    log = tmp_path / "bot.jsonl"
    write_log(log, [HALTED_RUN, TRADE_RUN, {**TRADE_RUN, "ts": "2026-11-17T10:15:10+00:00"}])
    runs = web.recent_runs(log, 2)
    assert [r["ts"] for r in runs] == ["2026-11-17T10:15:10+00:00", "2026-11-17T10:00:10+00:00"]
    assert web.recent_runs(tmp_path / "missing.jsonl", 10) == []


def test_overview_in_plain_words():
    html = web.page([TRADE_RUN], SETTINGS, NOW)
    assert "Practice mode: pretend money" in html
    assert "$10,120.50" in html and "+$120.50 since start (+1.21%)" in html
    assert "● Running" in html and "4 min ago" in html and "Checks every 15 minutes" in html
    assert "BTC $100.00" in html and "1 trade in the last 1 checks" in html


def test_latest_check_explains_each_agent_simply():
    html = web.page([TRADE_RUN], SETTINGS, NOW)
    for text in ["Buy BTC", "Trend and news agree.", "Full reasoning", "Buying a small amount.",
                 "Bought 0.002 BTC at $50,025.00. Safety exit (stop loss) set at $48,000.00.",
                 "▲ Rising", "Trend is strong", "Floor $49,000.00, ceiling $51,000.00",
                 "😟 Very negative", "Based on 1 headlines, confident", "No news today",
                 "Medium buys allowed", "50%", "Volatility is moderate.", "No valid answer"]:
        assert text in html, text


def test_paused_run_says_why_in_plain_words():
    html = web.page([HALTED_RUN], SETTINGS, NOW)
    assert "⏸ Paused" in html and "Paused: no trading" in html
    assert "fell too far below its best value" in html
    assert "BTC/USDT has no safety exit (stop loss)" in html
    assert "<h3>📈 Price trend" not in html  # agents did not run


def test_invalid_agent_output_is_explained():
    run = {**TRADE_RUN, "decision": {"action": "HOLD", "reasoning": "invalid agent output: market ETH/USDT"},
           "order": None, "result": None}
    html = web.page([run], SETTINGS, NOW)
    assert "Waited: no trade this time" in html and "did not answer properly" in html


def test_earlier_checks_are_one_line_each():
    html = web.page([TRADE_RUN, HALTED_RUN], SETTINGS, NOW)
    assert "Earlier checks" in html and "09:45 UTC" in html and html.count("<details class='check'>") == 1


def test_untrusted_text_is_escaped():
    html = web.page([TRADE_RUN], SETTINGS, NOW)
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<b>bold</b>" not in html


def test_live_mode_warns_and_empty_log():
    assert "LIVE: this bot is trading real money." not in web.page([], SETTINGS, NOW)
    assert "No checks yet." in web.page([], {}, NOW)


@pytest.mark.parametrize("score, word", [(-0.8, "Very negative"), (-0.3, "Negative"), (0.0, "Neutral"),
                                         (0.3, "Positive"), (0.9, "Very positive")])
def test_news_mood_words(score, word):
    assert web.mood(score)[1] == word


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


RISK = {"max_position_pct": 0.02, "max_order_usdt": None, "max_total_exposure_pct": 0.2, "max_daily_loss_pct": 0.03,
        "max_drawdown_pct": 0.1, "max_orders_per_hour": 4}


def test_money_rules_in_dollars():
    html = web.page([{**TRADE_RUN, "account": {"equity": 10_000.0}}], {**SETTINGS, "risk": RISK}, NOW)
    assert "Your money rules" in html
    for text in ["$200.00", "2% of the account", "$2,000.00", "$300.00", "$1,000.00"]:
        assert text in html, text
    assert "cannot trade" not in html


def test_fixed_cap_and_too_small_account_are_explained():
    capped = web.page([{**TRADE_RUN, "account": {"equity": 10_000.0}}],
                      {**SETTINGS, "risk": {**RISK, "max_order_usdt": 10}}, NOW)
    assert "$10.00" in capped and "never more than $10.00" in capped
    tiny = web.page([{**TRADE_RUN, "account": {"equity": 10.0}}], {**SETTINGS, "risk": RISK}, NOW)
    assert "cannot trade" in tiny  # 2% of $10 is $0.20, under Binance's $5 minimum


# --- login and network exposure ---

def get(url, user=None, password=None):
    import base64

    request = urllib.request.Request(url)
    if password is not None:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("WWW-Authenticate", "")


def test_password_protects_the_page(server, monkeypatch):
    monkeypatch.setenv("REPORT_PASSWORD", "correct horse battery")
    status, challenge = get(server + "/")
    assert status == 401 and challenge.startswith("Basic")  # the browser shows its login box
    assert get(server + "/", "bot", "wrong password")[0] == 401
    assert get(server + "/", "anyone", "correct horse battery")[0] == 200  # any username works
    assert get(server + "/../state/x", "bot", "nope")[0] == 401  # nothing at all without the password


def test_garbled_login_header_is_refused(server, monkeypatch):
    monkeypatch.setenv("REPORT_PASSWORD", "correct horse battery")
    request = urllib.request.Request(server + "/", headers={"Authorization": "Basic %%%not-base64"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(request)
    assert err.value.code == 401


def test_no_password_needed_on_this_machine_only(monkeypatch):
    for name in ("REPORT_BIND", "REPORT_HOST", "REPORT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert web.exposure_problem() is None
    monkeypatch.setenv("REPORT_BIND", "127.0.0.1")
    monkeypatch.setenv("REPORT_HOST", "0.0.0.0")  # inside Docker, published to this machine only
    assert web.exposure_problem() is None


@pytest.mark.parametrize("env", [{"REPORT_BIND": "0.0.0.0"}, {"REPORT_BIND": "0.0.0.0", "REPORT_PASSWORD": "short"},
                                 {"REPORT_HOST": "0.0.0.0"}])
def test_network_exposure_needs_a_strong_password(monkeypatch, env):
    for name in ("REPORT_BIND", "REPORT_HOST", "REPORT_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert "REPORT_PASSWORD" in web.exposure_problem()
    with pytest.raises(ValueError):
        web.make_server()
    monkeypatch.setenv("REPORT_PASSWORD", "a long enough password")
    assert web.exposure_problem() is None
