import openai
import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from llm import client as client_module
from llm.client import LLMClient


class Score(BaseModel):
    score: float = Field(ge=-1, le=1)


SETTINGS = {"llm": {"timeout_seconds": 30, "max_retries": 2, "providers": [
    {"name": "primary", "base_url": "https://primary.test/v1", "api_key_env": "PRIMARY_KEY",
     "models": {"fast": "a/fast", "reasoning": "a/think"}},
    {"name": "backup", "base_url": "https://backup.test/v1", "api_key_env": "BACKUP_KEY",
     "models": {"fast": "b/fast", "reasoning": "b/think"}},
]}}


class Calls(list):
    replies: dict  # base_url -> reply string or exception to raise


def ask(settings=SETTINGS, tier="fast"):
    return LLMClient(settings).ask(tier, "Score the news.", "BTC up", Score)


@pytest.fixture
def calls(monkeypatch):
    """Replaces ChatOpenAI and records every request."""
    calls = Calls()
    calls.replies = {}

    class FakeChat:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def invoke(self, messages):
            calls.append({**self.kwargs, "messages": messages})
            reply = calls.replies[self.kwargs["base_url"]]
            if isinstance(reply, Exception):
                raise reply
            return AIMessage(content=reply)

    monkeypatch.setattr(client_module, "ChatOpenAI", FakeChat)
    monkeypatch.setenv("PRIMARY_KEY", "k1")
    monkeypatch.setenv("BACKUP_KEY", "k2")
    return calls


def test_valid_reply_from_primary(calls):
    calls.replies["https://primary.test/v1"] = '{"score": 0.4}'
    assert ask() == Score(score=0.4)
    assert len(calls) == 1
    call = calls[0]
    assert (call["model"], call["api_key"], call["timeout"], call["max_retries"]) == ("a/fast", "k1", 30, 2)
    assert '"score"' in call["messages"][0].content  # schema is in the system prompt


def test_reasoning_tier_uses_reasoning_model(calls):
    calls.replies["https://primary.test/v1"] = '{"score": 0}'
    ask(tier="reasoning")
    assert calls[0]["model"] == "a/think"


@pytest.mark.parametrize("reply", ['```json\n{"score": -0.2}\n```', 'Here you go: {"score": -0.2}. Done.'])
def test_json_inside_fences_or_prose_is_accepted(calls, reply):
    calls.replies["https://primary.test/v1"] = reply
    assert ask() == Score(score=-0.2)


@pytest.mark.parametrize("bad", [
    "not json", '{"score": 5}', '{"score": "high"}', "{}", "", '{"score": 0.1',
    openai.APITimeoutError(request=None), RuntimeError("boom"),
])
def test_bad_primary_falls_back_to_backup(calls, bad):
    calls.replies.update({"https://primary.test/v1": bad, "https://backup.test/v1": '{"score": 0.3}'})
    assert ask() == Score(score=0.3)
    assert [c["model"] for c in calls] == ["a/fast", "b/fast"]
    assert calls[1]["api_key"] == "k2"


def test_all_providers_failing_returns_none(calls):
    calls.replies.update({"https://primary.test/v1": "nope", "https://backup.test/v1": RuntimeError("down")})
    assert ask() is None


def test_non_string_content_is_invalid(calls):
    calls.replies.update({"https://primary.test/v1": [{"type": "text", "text": '{"score": 1}'}],
                          "https://backup.test/v1": "nope"})
    assert ask() is None


@pytest.mark.parametrize("missing", ["base_url", "model", "key"])
def test_unconfigured_provider_is_skipped(calls, monkeypatch, missing):
    primary = dict(SETTINGS["llm"]["providers"][0])
    if missing == "base_url":
        primary["base_url"] = ""
    elif missing == "model":
        primary["models"] = {"fast": "", "reasoning": "a/think"}
    else:
        monkeypatch.delenv("PRIMARY_KEY")
    settings = {"llm": {**SETTINGS["llm"], "providers": [primary, SETTINGS["llm"]["providers"][1]]}}
    calls.replies["https://backup.test/v1"] = '{"score": 0.1}'
    assert ask(settings) == Score(score=0.1)
    assert [c["model"] for c in calls] == ["b/fast"]


def test_nothing_configured_returns_none(calls, monkeypatch):
    monkeypatch.delenv("PRIMARY_KEY")
    monkeypatch.delenv("BACKUP_KEY")
    assert ask() is None and calls == []
