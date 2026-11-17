"""OpenAI-compatible LLM client (OpenRouter, FastRouter) with timeouts, retries and provider fallback.

Every reply must be a JSON object that validates against the caller's Pydantic schema. Anything else
counts as a failure: the next provider is tried, and if all fail the caller gets None (which means HOLD).
"""
import json
import logging
import os

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

log = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, settings: dict):
        self.cfg = settings["llm"]

    def ask(self, tier: str, system: str, user: str, schema: type[BaseModel]) -> BaseModel | None:
        """tier: 'fast' or 'reasoning'. Returns the validated reply, or None if every provider failed."""
        system = (f"{system}\n\nReply with only a JSON object matching this JSON schema:\n"
                  f"{json.dumps(schema.model_json_schema())}")
        for p in self.cfg["providers"]:
            model, key = p["models"].get(tier), os.getenv(p["api_key_env"])
            if not (p["base_url"] and model and key):
                log.info("llm: skipping %s for %s, base_url, model or %s not set", p["name"], tier, p["api_key_env"])
                continue
            try:
                chat = ChatOpenAI(model=model, base_url=p["base_url"], api_key=key,
                                  timeout=self.cfg["timeout_seconds"], max_retries=self.cfg["max_retries"])
                text = chat.invoke([SystemMessage(system), HumanMessage(user)]).content
                text = text if isinstance(text, str) else ""
                # Models often wrap JSON in code fences or a sentence; keep only the outermost object.
                return schema.model_validate_json(text[text.find("{"):text.rfind("}") + 1])
            except Exception as e:
                log.warning("llm: %s %s failed: %r", p["name"], model, e)
        return None
