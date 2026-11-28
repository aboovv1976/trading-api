import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest

import main
import grok_bot
from models import Alert
from test_webhook import setup, client, payload, rich_payload, enriched_payload, queued, WORKER, AUTH


@pytest.fixture
def bot(monkeypatch):
    for name, value in {"GROK_CALLBACK_TOKEN": "callback-test",
                        "GROK_WEBHOOK_URL": "https://bot.example/webhook", "GROK_WEBHOOK_KEY": "bot-test"}.items():
        monkeypatch.setenv(name, value)
    original = httpx.Client
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"accepted": True})
    monkeypatch.setattr(grok_bot.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr(main, "dispatch_grok_bot", grok_bot.dispatch)
    return sent


def dispatch(client, payload):
    response = client.post(WORKER, headers=AUTH, json=queued(payload))
    assert response.status_code == 202
    assert response.json["status"] == "awaiting_callback"
    return response.json["job_id"]


def callback(job_id):
    return {
        "job_id": job_id, "status": "completed", "ticker": "AAPL",
        "recommendation": {
            "action": "enter_long", "confidence": "medium",
            "entry": {"low": 199.0, "high": 201.0}, "stop_loss": 190.0,
            "targets": [{"price": 220.0, "reason": "Test resistance"}],
            "time_limit_days": 5,
        },
        "analysis": {"trend": "Test research indicates strength."},
        "reasons": ["Test research indicates strength."],
        "market_data_sufficient": True,
        "sources": [{"url": "https://exchange.example/prices", "title": "Market data",
                     "kind": "market_data", "as_of": datetime.now(timezone.utc).isoformat()}],
        "summary": "AAPL Buy, entry 199–201, stop 190, target 220.",
        "recommendation_text": "RECOMMENDATION\nBuy. Entry 199–201, stop 190, target 220.",
        "notification_text": "AAPL Buy, entry 199–201, stop 190, target 220.",
    }


def post_result(client, body, token="callback-test"):
    return client.post(grok_bot.CALLBACK_PATH, headers={"Authorization": "Bearer " + token}, json=body)


def test_dispatch_forwards_validated_alert_without_credentials(client, payload, bot):
    job = dispatch(client, payload)
    assert len(bot) == 1
    request = bot[0]
    assert request.headers["Authorization"] == "Bearer bot-test"
    body = json.loads(request.content)
    assert body["job_id"] == job and body["ticker"] == "NASDAQ:AAPL"
    assert "secret" not in body and "callback-test" not in request.content.decode()
    assert not hasattr(main, "analyze")
    assert client.post(WORKER, headers=AUTH, json=queued(payload)).status_code == 202
    assert len(bot) == 1


def test_callback_stores_result_available_through_existing_api(client, payload, bot):
    job = dispatch(client, payload)
    response = post_result(client, callback(job))
    assert response.status_code == 200 and not response.json["duplicate"]
    result = client.get(main.RESULTS_PATH + "/" + job, headers={"Authorization": "Bearer result-test"})
    assert result.status_code == 200
    assert result.json["analysis_backend"] == "grok_bot"
    assert result.json["analysis"]["decision"] == "Buy"
    assert grok_bot.PROVENANCE in result.json["analysis"]["limitations"]
    assert result.json["report_url"].startswith("https://example.com/")
    with main.redis_client() as store:
        assert 604798 <= store.ttl("tv:{" + job + "}:result") <= 604800
        assert not store.exists("tv:{" + job + "}:bot_pending")
    assert not hasattr(main, "analyze")


def test_callback_retry_is_idempotent_and_different_result_conflicts(client, payload, bot):
    job = dispatch(client, payload)
    body = callback(job)
    assert post_result(client, body).status_code == 200
    with main.redis_client() as store:
        key = "tv:{" + job + "}:result"
        saved = store.get(key)
        store.expire(key, 100)
    assert post_result(client, body).json["duplicate"] is True
    body["analysis"]["rationale"] = "Different research"
    assert post_result(client, body).status_code == 409
    with main.redis_client() as store:
        assert store.get(key) == saved and store.ttl(key) <= 100


def test_unknown_job_not_created(client, bot):
    assert post_result(client, callback("f" * 64)).status_code == 404


@pytest.mark.parametrize("token", ["wrong", "worker-test", "result-test"])
def test_callback_requires_dedicated_token(client, bot, token):
    assert post_result(client, callback("f" * 64), token).status_code == 401


@pytest.mark.parametrize("change", [
    lambda b: b.update(job_id="invalid"),
    lambda b: b.update(ticker=None),
    lambda b: b.update(status="invalid"),
    lambda b: b["recommendation"].update(stop_loss=220.0),
    lambda b: b.update(notification_text="x" * 301),
    lambda b: b["recommendation"]["entry"].update(low=True),
])
def test_unexpected_callback_is_stored_as_error(client, payload, bot, change):
    job = dispatch(client, payload)
    body = callback(job)
    change(body)
    response = post_result(client, body)
    if body["job_id"] != job:
        assert response.status_code == 400
        return
    assert response.status_code == 200 and response.json["status"] == "error"
    with main.redis_client() as store:
        record = json.loads(store.get("tv:{" + job + "}:result"))
        assert record["status"] == "error" and record["analysis"] is None
        assert record["bot_response"] == body and record["validation_errors"]


@pytest.mark.parametrize("problem", ["missing", "stale", "insufficient"])
def test_missing_market_evidence_downgrades_to_watch(client, payload, bot, problem):
    body = callback(dispatch(client, payload))
    if problem == "missing":
        body["sources"] = []
    elif problem == "stale":
        body["sources"][0]["as_of"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    else:
        body["market_data_sufficient"] = False
    assert post_result(client, body).status_code == 200
    with main.redis_client() as store:
        analysis = json.loads(store.get("tv:{" + body["job_id"] + "}:result"))["analysis"]
    assert analysis["decision"] == "Watch" and analysis["entry_range"] is None
    assert analysis["recommendation_text"].startswith("WATCH")


def test_bot_failure_keeps_pending_for_retry_without_running_local_analysis(client, payload, bot, monkeypatch):
    http = MagicMock()
    http.__enter__.return_value = http
    http.post.side_effect = httpx.ReadTimeout("private error")
    monkeypatch.setattr(grok_bot.httpx, "Client", lambda **kwargs: http)
    assert client.post(WORKER, headers=AUTH, json=queued(payload)).status_code == 503
    job = main.job_id_for(Alert.model_validate(queued(payload)))
    with main.redis_client() as store:
        assert store.exists("tv:{" + job + "}:bot_pending")
        assert not store.exists("tv:{" + job + "}:bot_sent")
        assert not store.exists("tv:{" + job + "}:lock")
    assert not hasattr(main, "analyze")




def test_pending_alert_identity_conflict(client, payload, bot):
    payload["alert_id"] = "shared-id"
    dispatch(client, payload)
    payload["price"] = "201"
    assert client.post(WORKER, headers=AUTH, json=queued(payload)).status_code == 409
    assert len(bot) == 1


def test_callback_storage_failure_does_not_acknowledge(client, payload, bot, monkeypatch, caplog):
    body = callback(dispatch(client, payload))
    def fail():
        raise ConnectionError("secret-connection-data")
    monkeypatch.setattr(main, "redis_client", fail)
    assert post_result(client, body).status_code == 503
    assert "secret-connection-data" not in caplog.text


def test_missing_callback_config_is_unavailable(client, bot, monkeypatch):
    monkeypatch.delenv("GROK_CALLBACK_TOKEN")
    assert post_result(client, callback("f" * 64)).status_code == 503


def test_callback_can_finish_before_dispatch_acknowledgement(client, payload, bot, monkeypatch):
    http = MagicMock()
    http.__enter__.return_value = http
    def immediate(url, *, headers, json):
        assert post_result(client, callback(json["job_id"])).status_code == 200
        return httpx.Response(200, request=httpx.Request("POST", url))
    http.post.side_effect = immediate
    monkeypatch.setattr(grok_bot.httpx, "Client", lambda **kwargs: http)
    job = dispatch(client, payload)
    with main.redis_client() as store:
        assert store.exists("tv:{" + job + "}:result")
    assert client.post(WORKER, headers=AUTH, json=queued(payload)).json["duplicate"] is True
    assert not hasattr(main, "analyze")


def test_expired_pending_job_rejected(client, payload, bot):
    job = dispatch(client, payload)
    with main.redis_client() as store:
        store.delete("tv:{" + job + "}:bot_pending")
    assert post_result(client, callback(job)).status_code == 404


def test_cannot_replace_preexisting_result_with_bot_callback(client, payload, bot):
    alert = Alert.model_validate(queued(payload))
    job_id = main.job_id_for(alert)
    with main.redis_client() as store:
        store.set("tv:{" + job_id + "}:result", json.dumps({"status": "completed", "fingerprint": main.fingerprint(alert),
                  "analysis": {"decision": "Watch"}}), ex=604800)
    assert client.post(WORKER, headers=AUTH, json=queued(payload)).json["duplicate"] is True
    assert post_result(client, callback(job_id)).status_code == 409
    assert not bot


def test_bot_dispatch_does_not_require_direct_api_credentials(client, payload, bot, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    dispatch(client, payload)
    assert len(bot) == 1
    assert not hasattr(main, "analyze")


def test_rich_indicator_context_reaches_external_bot(client, enriched_payload, bot):
    job_id = dispatch(client, enriched_payload)
    forwarded = json.loads(bot[0].content)
    assert forwarded["job_id"] == job_id
    assert forwarded["adx"] == enriched_payload["adx"]
    assert forwarded["supertrend"] == enriched_payload["supertrend"]
    assert forwarded["prevCloses"] == enriched_payload["prevCloses"]
    assert forwarded["indicator_settings"] == enriched_payload["indicator_settings"]
    assert forwarded["currency"] == "USD"
    assert forwarded["bar_confirmed"] is True
    assert "secret" not in forwarded


def test_bot_rejection_is_terminal_and_visible_in_results(client, payload, bot):
    job = dispatch(client, payload)
    body = {"job_id": job, "status": "rejected", "reason": "Missing ticker"}
    response = post_result(client, body)
    assert response.status_code == 200 and response.json["status"] == "rejected"
    headers = {"Authorization": "Bearer result-test"}
    record = client.get(main.RESULTS_PATH + "/" + job, headers=headers).json
    assert record["status"] == "rejected" and record["reason"] == "Missing ticker"
    assert record["analysis"] is None and record["report_url"] is None
    assert record["alert"]["ticker"] == "NASDAQ:AAPL"
    listing = client.get(main.RESULTS_PATH, headers=headers).json["results"]
    assert listing[0]["status"] == "rejected"
    duplicate = client.post(WORKER, headers=AUTH, json=queued(payload))
    assert duplicate.status_code == 200 and duplicate.json["status"] == "rejected"
    assert len(bot) == 1
    with main.redis_client() as store:
        assert not store.exists("tv:{" + job + "}:bot_pending")
        assert 604798 <= store.ttl("tv:{" + job + "}:result") <= 604800
    assert client.get(main.RESULTS_PATH + "/" + job + "?format=text", headers=headers).status_code == 409


def test_rejection_retries_are_idempotent_and_cannot_be_replaced(client, payload, bot):
    job = dispatch(client, payload)
    body = {"job_id": job, "status": "rejected", "reason": "Research unavailable"}
    assert post_result(client, body).status_code == 200
    assert post_result(client, body).json["duplicate"] is True
    assert post_result(client, callback(job)).status_code == 409
    body["reason"] = "Changed reason"
    assert post_result(client, body).status_code == 409


@pytest.mark.parametrize("change", [
    {"reason": ""}, {"reason": "   "}, {"reason": "x" * 2001}, {"reason": None},
    {"job_id": "abc-123"}, {"status": "failed"}, {"analysis": {}},
])
def test_unexpected_rejection_is_stored_as_error(client, payload, bot, change):
    job = dispatch(client, payload)
    body = {"job_id": job, "status": "rejected", "reason": "Research unavailable", **change}
    response = post_result(client, body)
    if body["job_id"] != job:
        assert response.status_code == 400
        with main.redis_client() as store:
            assert store.exists("tv:{" + job + "}:bot_pending")
        return
    assert response.status_code == 200 and response.json["status"] == "error"
    with main.redis_client() as store:
        assert not store.exists("tv:{" + job + "}:bot_pending")
        assert json.loads(store.get("tv:{" + job + "}:result"))["status"] == "error"


def test_unknown_rejected_job_is_not_created(client, bot):
    assert post_result(client, {"job_id": "f" * 64, "status": "rejected", "reason": "Missing ticker"}).status_code == 404


def test_error_result_is_visible_and_duplicate_does_not_restart_job(client, payload, bot):
    job = dispatch(client, payload)
    body = {"job_id": job, "unexpected": {"details": "Unsupported reply shape"}}
    response = post_result(client, body)
    assert response.status_code == 200 and response.json["status"] == "error"
    headers = {"Authorization": "Bearer result-test"}
    record = client.get(main.RESULTS_PATH + "/" + job, headers=headers).json
    assert record["status"] == "error" and record["error_type"] == "unexpected_bot_response"
    assert record["reason"] and record["validation_errors"]
    assert record["bot_response"] == body and record["report_url"] is None
    assert client.get(main.RESULTS_PATH, headers=headers).json["results"][0]["status"] == "error"
    with main.redis_client() as store:
        key = "tv:{" + job + "}:result"
        store.expire(key, 100)
    assert post_result(client, body).json["duplicate"] is True
    with main.redis_client() as store:
        assert store.ttl(key) <= 100
    duplicate = client.post(WORKER, headers=AUTH, json=queued(payload))
    assert duplicate.status_code == 200 and duplicate.json["status"] == "error"
    assert len(bot) == 1


def test_invalid_json_or_missing_job_id_cannot_be_associated(client, bot):
    headers = {"Authorization": "Bearer callback-test", "Content-Type": "application/json"}
    assert client.post(grok_bot.CALLBACK_PATH, headers=headers, data="not JSON").status_code == 400
    assert post_result(client, {"unexpected": "no job ID"}).status_code == 400
    with main.redis_client() as store:
        assert not list(store.scan_iter(match="tv:*:result"))


def test_interpretation_exception_is_recorded_without_leaking_exception_text(client, payload, bot, monkeypatch):
    job = dispatch(client, payload)
    def fail(_):
        raise RuntimeError("private exception details")
    monkeypatch.setattr(grok_bot, "presentation", fail)
    response = post_result(client, callback(job))
    assert response.status_code == 200 and response.json["status"] == "error"
    record = client.get(main.RESULTS_PATH + "/" + job, headers={"Authorization": "Bearer result-test"}).json
    assert record["error_type"] == "unexpected_bot_response"
    assert record["validation_errors"] == [{"field": "recommendation", "type": "RuntimeError"}]
    assert "private exception details" not in json.dumps(record)
