import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

import main
from recommendations import watch
from test_webhook import setup, client, payload
from test_grok_bot import bot, dispatch, post_result
from whatsapp_commands import recommendations


@pytest.fixture
def rich_body(client, payload, bot):
    payload.update(ticker="NASDAQ:NVDA", price=181.25, interval="240")
    return {
        "job_id": dispatch(client, payload), "status": "completed", "ticker": "NVDA", "signal": "green",
        "bias": "bullish", "timeframe": "4h", "alert_price": 181.25,
        "alert_time": payload["time"], "analyzed_at": datetime.now(timezone.utc).isoformat(), "watchlist": True,
        "recommendation": {
            "action": "enter_long", "confidence": "medium", "entry": {"low": 179.5, "high": 182.0},
            "stop_loss": 174.8, "stop_reason": "Below recent swing low and Supertrend line",
            "targets": [{"price": 192.0, "reason": "Prior resistance"}, {"price": 198.5, "reason": "Recent high"}],
            "reward_to_risk": 2.3, "time_limit_days": 10,
            "position_size": {"risk_pct": 1, "shares_note": "Risk 1% of account; size = risk $ / (entry - stop)"}},
        "analysis": {"trend": "Uptrend above 50-day MA", "support": [178.0, 174.5], "resistance": [192.0, 198.5],
            "volume": "1.4x 20-day average", "adx": "27.4, trending", "indicator_context": "Supplied indicator context",
            "news": ["Headline 1", "Headline 2"], "sentiment": {"vix": 16.2, "fear_greed": 58, "ticker": "Mildly positive"}},
        "reasons": ["Reason 1", "Reason 2", "Reason 3"], "main_risk": "Earnings in 6 days",
        "risk_rules_used": {"risk_pct": 1, "min_rr": 2, "max_hold_days": 10, "defaults": True},
        "summary": "Plain-text recommendation", "disclaimer": "Analysis only, not financial advice.",
        "recommendation_text": "Full model-written recommendation.", "notification_text": "NVDA test recommendation."}


def evidence(body):
    body["market_data_sufficient"] = True
    body["sources"] = [{"url": "https://example.test/market-data", "title": "Market data", "kind": "market_data",
                        "as_of": datetime.now(timezone.utc).isoformat()}]
    return body


def get_record(client, body):
    response = client.get(main.RESULTS_PATH + "/" + body["job_id"], headers={"Authorization": "Bearer result-test"})
    assert response.status_code == 200
    return response.json


def test_original_rich_example_is_retained_without_fabricated_evidence(client, rich_body):
    response = post_result(client, rich_body)
    assert response.status_code == 200
    record = get_record(client, rich_body)
    assert record["bot_response"] == rich_body
    assert record["analysis"]["decision"] == "Watch"
    assert record["analysis"]["entry_range"] is None
    assert record["alert"]["ticker"] == "NASDAQ:NVDA"
    assert "timestamped market evidence" in record["analysis"]["rationale"]


def test_range_targets_and_full_report_are_preserved(client, rich_body):
    evidence(rich_body)
    rich_body["recommendation_text"] = "Full model-written report with entry range 179.5–182.0 and targets 192.0, 198.5."
    rich_body["notification_text"] = "NVDA — bullish swing setup."
    assert post_result(client, rich_body).status_code == 200
    record = get_record(client, rich_body)
    assert record["bot_response"] == rich_body
    analysis = record["analysis"]
    assert analysis["decision"] == "Buy"
    assert analysis["entry_range"] == {"low": 179.5, "high": 182.0}
    assert [target["price"] for target in analysis["targets"]] == [192.0, 198.5]
    assert record["bot_response"]["recommendation"]["targets"][1]["price"] == 198.5
    assert analysis["recommendation_text"] == rich_body["recommendation_text"]
    assert "Earnings in 6 days" in analysis["limitations"]
    parsed = urlsplit(record["report_url"])
    page = client.get(parsed.path + "?" + parsed.query)
    assert page.status_code == 200
    assert "Complete bot response" in page.text and "fear_greed" in page.text and "198.5" in page.text
    with main.redis_client() as store:
        reply = "\n".join(recommendations(store, ["NVDA"]))
    assert "Entry range: 179.5–182.0" in reply and "198.5" in reply


def test_future_bot_fields_are_not_discarded(client, rich_body):
    rich_body["future_metric"] = {"value": "new detail"}
    rich_body["recommendation"]["trailing_stop_rule"] = "After target one"
    rich_body["analysis"]["new_indicator"] = {"description": "new"}
    assert post_result(client, rich_body).status_code == 200
    assert get_record(client, rich_body)["bot_response"] == rich_body


def test_native_retries_ignore_object_key_order_but_conflict_on_changed_data(client, rich_body):
    assert post_result(client, rich_body).status_code == 200
    reordered = json.loads(json.dumps(rich_body, sort_keys=True))
    assert post_result(client, reordered).json["duplicate"] is True
    rich_body["recommendation"]["confidence"] = "high"
    assert post_result(client, rich_body).status_code == 409


@pytest.mark.parametrize("change", [
    lambda b: b.update(ticker="MSFT"),
    lambda b: b["recommendation"]["entry"].update(low=183.0),
    lambda b: b["recommendation"].update(stop_loss=180.0),
    lambda b: b["recommendation"]["targets"][1].update(price=181.0),
    lambda b: b["recommendation"].update(reward_to_risk=-1.0),
    lambda b: b.update(summary=""),
])
def test_invalid_fields_are_retained_as_error_results(client, rich_body, change):
    change(rich_body)
    response = post_result(client, rich_body)
    assert response.status_code == 200 and response.json["status"] == "error"
    with main.redis_client() as store:
        assert not store.exists("tv:{" + rich_body["job_id"] + "}:bot_pending")
    record = get_record(client, rich_body)
    assert record["status"] == "error" and record["analysis"] is None
    assert record["bot_response"] == rich_body


def test_report_escapes_rich_payload_text(client, rich_body):
    rich_body["analysis"]["trend"] = '<script>alert("untrusted")</script>'
    post_result(client, rich_body)
    parsed = urlsplit(get_record(client, rich_body)["report_url"])
    page = client.get(parsed.path + "?" + parsed.query)
    assert page.status_code == 200 and '<script>alert("untrusted")</script>' not in page.text
    assert "&lt;script&gt;" in page.text


def test_compact_callback_is_recorded_as_error_not_interpreted_as_recommendation(client, rich_body):
    compact = {"job_id": rich_body["job_id"], "analysis": watch("Test").model_dump(mode="json")}
    response = post_result(client, compact)
    assert response.status_code == 200 and response.json["status"] == "error"
    record = get_record(client, rich_body)
    assert record["status"] == "error" and record["analysis"] is None
    assert record["bot_response"] == compact
    assert post_result(client, rich_body).status_code == 200
    assert get_record(client, rich_body)["status"] == "completed"


def test_unsupported_action_is_preserved_without_inventing_buy(client, rich_body):
    evidence(rich_body)
    rich_body["recommendation"]["action"] = "enter_short"
    assert post_result(client, rich_body).status_code == 200
    record = get_record(client, rich_body)
    assert record["bot_response"]["recommendation"]["action"] == "enter_short"
    assert record["analysis"]["decision"] == "Watch"


@pytest.mark.parametrize("field", ["recommendation_text", "notification_text"])
def test_bot_must_supply_both_text_fields(client, rich_body, field):
    del rich_body[field]
    response = post_result(client, rich_body)
    assert response.status_code == 200 and response.json["status"] == "error"
    record = get_record(client, rich_body)
    assert record["analysis"] is None and record["report_url"] is None
    assert record["bot_response"] == rich_body
