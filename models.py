"""Bounded webhook input and validated analysis output."""

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def iso_timestamp(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("Expected an ISO 8601 timestamp")
    return value


def timezone_required(value):
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def reject_boolean(value):
    if isinstance(value, bool):
        raise ValueError("Expected a number")
    return value


class Alert(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    alert_id: Optional[Annotated[str, Field(min_length=1, max_length=128)]] = None
    ticker: Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.:/!\-]+$")]
    signal: Literal["green", "red"]
    price: Price
    interval: Annotated[str, Field(min_length=1, max_length=10, pattern=r"^[0-9A-Za-z]+$")]
    time: datetime
    open: Optional[Price] = None
    high: Optional[Price] = None
    low: Optional[Price] = None
    close: Optional[Price] = None
    volume: Optional[NonNegative] = None
    supertrend: Optional[Annotated[float, Field(allow_inf_nan=False)]] = None
    adx: Optional[Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]] = None
    highest20: Optional[Price] = None
    lowest20: Optional[Price] = None
    avgVol20: Optional[NonNegative] = None
    avgClose20: Optional[Price] = None
    prevCloses: Optional[Annotated[list[Optional[Price]], Field(min_length=5, max_length=5)]] = None

    @model_validator(mode="before")
    @classmethod
    def use_close_as_price(cls, data):
        if isinstance(data, dict) and "price" not in data and "close" in data:
            return {**data, "price": data["close"]}
        return data

    _iso_time = field_validator("time", mode="before")(iso_timestamp)
    _utc_time = field_validator("time")(timezone_required)
    _numeric = field_validator(
        "price", "open", "high", "low", "close", "volume", "supertrend", "adx",
        "highest20", "lowest20", "avgVol20", "avgClose20", mode="before",
    )(reject_boolean)

    @field_validator("prevCloses", mode="before")
    @classmethod
    def numeric_previous_closes(cls, values):
        if isinstance(values, list):
            for value in values:
                reject_boolean(value)
        return values

    @model_validator(mode="after")
    def consistent_prices(self):
        if self.close is not None and self.close != self.price:
            raise ValueError("price and close must match when both are supplied")
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high must be at least low")
        for value in (self.open, self.close):
            if value is not None:
                if self.high is not None and value > self.high:
                    raise ValueError("OHLC value exceeds high")
                if self.low is not None and value < self.low:
                    raise ValueError("OHLC value is below low")
        if self.highest20 is not None and self.lowest20 is not None and self.highest20 < self.lowest20:
            raise ValueError("highest20 must be at least lowest20")
        return self


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: Annotated[str, Field(max_length=2000, pattern=r"^https?://[^\s]+$")]
    title: Annotated[str, Field(min_length=1, max_length=300)]
    kind: Literal["market_data", "news", "sentiment"]
    as_of: Optional[datetime]

    _utc_time = field_validator("as_of")(lambda value: timezone_required(value) if value else None)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["Buy", "Watch", "Avoid"]
    buy_price: Optional[Price]
    stop_loss: Optional[Price]
    sell_target: Optional[Price]
    time_limit: Optional[Annotated[str, Field(max_length=200)]]
    rationale: Annotated[str, Field(min_length=1, max_length=2000)]
    limitations: Annotated[list[Annotated[str, Field(max_length=500)]], Field(max_length=20)]
    market_data_sufficient: bool
    sources: Annotated[list[Source], Field(max_length=20)]

    @model_validator(mode="after")
    def valid_levels(self):
        if self.decision == "Buy":
            if any(value is None for value in (self.buy_price, self.stop_loss, self.sell_target)) or not self.time_limit:
                raise ValueError("Buy requires price levels and a time limit")
            if not self.stop_loss < self.buy_price < self.sell_target:
                raise ValueError("Expected stop_loss < buy_price < sell_target")
        return self
