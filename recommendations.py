"""Watch notices for insufficient market evidence; no model requests."""

from models import AnalysisView

LIMITATIONS = [
    "Web-sourced market data may be delayed or incomplete; no direct exchange feed is connected.",
    "Source content and timestamps are assessed by the model, not independently verified.",
]


def watch(reason, sources=None, limitations=None):
    return AnalysisView(
        decision="Watch", entry_range=None, stop_loss=None, targets=[],
        time_limit_days=None, rationale=reason,
        limitations=list(dict.fromkeys(LIMITATIONS + (limitations or [])))[:20],
        market_data_sufficient=False, sources=sources or [],
        # Validation notice only; the original bot report is retained separately.
        recommendation_text="WATCH\n\n" + reason,
        notification_text=("Watch: " + reason)[:300],
    )
