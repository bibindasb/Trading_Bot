"""Structured logging: JSON lines to a rotating file, short plain lines to the console.

Pass structured fields with extra={"data": {...}}; they land under "data" in the JSON line.
"""
import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# These log full request URLs at INFO, and the Telegram URL contains the bot token.
QUIET = ("httpx", "httpcore", "openai")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                 "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if hasattr(record, "data"):
            entry["data"] = record.data
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(path: str | Path = "logs/bot.jsonl", level: int = logging.INFO) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.handlers.clear()  # safe to call twice without doubling every line
    root.setLevel(level)
    file = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=10, encoding="utf-8")
    file.setFormatter(JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(file)
    root.addHandler(console)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
