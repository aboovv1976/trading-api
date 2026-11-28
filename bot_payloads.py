"""Rich external bot payloads and the validated app display fields."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models import (
    AnalysisView,
    EntryRange,
    Price,
    Source,
    TradeTarget,
    timezone_required,
)


class RichFields(BaseModel):
    # Preserve additional bot fields rather than silently discarding useful data.
    model_config = ConfigDict(extra="allow", strict=True)


class TradePlan(RichFields):
    action: Annotated[str, Field(min_length=1, max_length=64)]
    confidence: Annotated[str, Field(max_length=100)] | None = None
    entry: EntryRange | None = None
    stop_loss: Price | None = None
    stop_reason: Annotated[str, Field(max_length=2000)] | None = None
    targets: Annotated[list[TradeTarget], Field(max_length=20)] = Field(default_factory=list)
    reward_to_risk: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None
    time_limit_days: Annotated[int, Field(gt=0, le=3650)] | None = None
    position_size: dict | None = None

    @model_validator(mode="after")
    def valid_long_plan(self):
        if self.action.lower() == "enter_long":
            if not self.entry or self.stop_loss is None or not self.targets or self.time_limit_days is None:
                raise ValueError("enter_long requires entry, stop, targets and time limit")
            if not self.stop_loss < self.entry.low or any(t.price <= self.entry.high for t in self.targets):
                raise ValueError("Long stop must be below entry and targets above entry")
        return self


class RichBotCallback(RichFields):
    job_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["completed"]
    ticker: Annotated[str, Field(min_length=1, max_length=40)]
    signal: Annotated[str, Field(max_length=64)] | None = None
    bias: Annotated[str, Field(max_length=100)] | None = None
    timeframe: Annotated[str, Field(max_length=40)] | None = None
    alert_price: Price | None = None
    alert_time: datetime | None = None
    analyzed_at: datetime | None = None
    watchlist: bool | None = None
    recommendation: TradePlan
    analysis: dict
    reasons: Annotated[list[Annotated[str, Field(max_length=2000)]], Field(max_length=20)] = Field(default_factory=list)
    main_risk: Annotated[str, Field(max_length=2000)] | None = None
    risk_rules_used: dict | None = None
    summary: Annotated[str, Field(min_length=1, max_length=12000)]
    disclaimer: Annotated[str, Field(max_length=2000)] | None = None
    sources: Annotated[list[Source], Field(max_length=20)] = Field(default_factory=list)
    market_data_sufficient: bool = False
    recommendation_text: Annotated[str, Field(min_length=1, max_length=12000)]
    notification_text: Annotated[str, Field(min_length=1, max_length=300)]

    _utc_times = field_validator("alert_time", "analyzed_at")(
        lambda value: timezone_required(value) if value is not None else None
    )


def presentation(body: RichBotCallback) -> AnalysisView:
    plan = body.recommendation
    action = plan.action.lower()
    decision = {"enter_long": "Buy", "avoid": "Avoid", "exit_long": "Avoid"}.get(action, "Watch")
    limitations = [value[:500] for value in (body.main_risk, body.disclaimer) if value]
    if action not in ("enter_long", "avoid", "exit_long", "watch", "wait", "hold", "no_trade"):
        limitations.append("Bot action is shown in the full response; no supported long-entry action was inferred.")
    return AnalysisView(
        decision=decision,
        # Do not invent a midpoint, select a fill, or collapse multiple targets.
        entry_range=plan.entry,
        stop_loss=plan.stop_loss,
        targets=plan.targets,
        time_limit_days=plan.time_limit_days,
        rationale=("; ".join(body.reasons) or body.summary)[:2000],
        limitations=limitations, market_data_sufficient=body.market_data_sufficient,
        sources=body.sources,
        recommendation_text=body.recommendation_text,
        notification_text=body.notification_text,
    )
