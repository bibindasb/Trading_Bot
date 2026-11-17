from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text())


def test_defaults_to_paper_trading():
    assert settings["live_trading"] is False
    assert "LIVE_TRADING=false" in (ROOT / ".env.example").read_text()


def test_risk_limits_are_sane_fractions():
    risk = settings["risk"]
    for key in ("max_position_pct", "max_total_exposure_pct", "max_daily_loss_pct", "max_drawdown_pct"):
        assert 0 < risk[key] < 1, key
    assert risk["max_position_pct"] <= risk["max_total_exposure_pct"]
    assert risk["max_orders_per_hour"] > 0
    assert settings["symbols"]


def test_env_is_gitignored_and_example_has_no_secrets():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            assert line.split("=", 1)[1] in ("", "false"), line


def test_dependencies_import():
    import apscheduler, ccxt, dotenv, httpx, langchain_openai, langgraph, pandas, pydantic, ta  # noqa: F401
