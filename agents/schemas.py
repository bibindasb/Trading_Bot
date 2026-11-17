"""Pydantic schemas for every agent output. A reply that does not validate is treated as HOLD."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Fraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Text = Annotated[str, Field(max_length=1000)]


class MarketReport(BaseModel):
    trend: Literal["up", "down", "sideways"]
    strength: Fraction
    support: Price
    resistance: Price
    summary: Text


class SentimentReport(BaseModel):
    """Numbers only, so nothing from untrusted news text can reach a later prompt."""
    model_config = ConfigDict(extra="forbid")
    score: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
    confidence: Fraction


class RiskAdvice(BaseModel):
    max_size: Fraction  # cap for the next buy, as a fraction of the maximum position size
    reduce: list[str] = []  # held symbols to trim
    reasoning: Text


class Decision(BaseModel):
    action: Literal["BUY", "SELL", "HOLD"]
    symbol: str | None = None
    size: Fraction = 0.0  # BUY: fraction of the maximum position size. SELL: fraction of the amount held.
    stop_loss: Price | None = None
    take_profit: Price | None = None
    reasoning: Text = ""

    @model_validator(mode="after")
    def _complete(self):
        if self.action != "HOLD" and not (self.symbol and self.size > 0):
            raise ValueError(f"{self.action} needs a symbol and a size above 0")
        if self.action == "BUY" and self.stop_loss is None:
            raise ValueError("BUY needs a stop_loss")
        return self
