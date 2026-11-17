"""Telegram alerts for trades, rejected orders, errors, limit hits and the kill switch.

Ongoing conditions (a halt, an unprotected position, a failing run) are sent once when they start and once
when they clear, not on every 15-minute run. Alerting never raises: a Telegram outage must not stop trading
safeguards. Without TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, alerts are only logged.
"""
import logging
import os

import httpx

log = logging.getLogger(__name__)
MAX_LENGTH = 4096  # Telegram's message limit


class Alerter:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "") if token is None else token
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "") if chat_id is None else chat_id
        self.active: set[str] = set()  # ongoing conditions already alerted
        self.halted = self.failed = False

    def send(self, text: str) -> bool:
        log.info("alert", extra={"data": {"text": text}})
        if not (self.token and self.chat_id):
            return False
        try:
            r = httpx.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                           json={"chat_id": self.chat_id, "text": text[:MAX_LENGTH]}, timeout=10)
            r.raise_for_status()
            return True
        except Exception as e:
            log.error("alert failed: %s", repr(e).replace(self.token, "***"))  # error text can hold the URL
            return False

    def run_finished(self, state: dict) -> str | None:
        """Alert on what this run did and on conditions that started or cleared. Returns the text sent."""
        halts, issues = state.get("halts") or [], state.get("issues") or []
        lines = [f"HALTED: {h}" for h in halts if h not in self.active]
        if self.halted and not halts:
            lines.append("RESUMED: trading is no longer halted")
        lines += [f"WARNING: {i}" for i in issues if i not in self.active]
        if self.failed:
            lines.append("RECOVERED: runs are succeeding again")
        self.active, self.halted, self.failed = set(halts) | set(issues), bool(halts), False

        result = state.get("result") or {}
        status = result.get("status")
        if status in ("filled", "partial"):
            placed, stop = result["order"], (state.get("order") or {}).get("stop_loss")
            reason = (state.get("decision") or {}).get("reasoning", "")[:300]
            lines.append(f"TRADE ({status}): {placed['side'].upper()} {placed.get('filled')} {placed['symbol']} "
                         f"at {placed.get('average')}" + (f", stop {stop}" if stop else "") + f". {reason}")
        elif status == "rejected":
            lines.append("REJECTED by risk guard: " + "; ".join(result["reasons"]))
        elif status in ("error", "unfilled"):
            lines.append(f"ORDER {status.upper()}: {result.get('error', 'nothing filled')}")
        lines += [f"WARNING: {i}" for i in result.get("issues", [])]

        if not lines:
            return None
        text = "\n".join(lines)
        self.send(text)
        return text

    def run_failed(self, error: Exception) -> None:
        text = f"ERROR: run failed: {error!r}"
        if text not in self.active:
            self.send(text)
        self.active.add(text)
        self.failed = True
