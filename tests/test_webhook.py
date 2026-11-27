import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import fakeredis
import httpx
import pytest
from pydantic import ValidationError

import analysis
import main
from models import Alert, Recommendation

WEBHOOK = "/api/v1/tv-alert-x7k9m2p"
WORKER = "/api/v1/analyze-e8f8323f5eecdfce"
AUTH = {"Authorization": "Bearer worker-test"}
PUBLISH = main.publish


@pytest.fixture
def payload():
    return {
        "secret": "tv-test", "ticker": "NASDAQ:AAPL", "signal": "green",
        "price": "200.50", "interval": "60",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    values = {
        "TV_WEBHOOK_SECRET": "tv-test", "QSTASH_TOKEN": "queue-test",
        "QSTASH_URL": "https://qstash-us-east-1.upstash.io",
        "WORKER_TOKEN": "worker-test", "WORKER_URL": "https://example.com" + WORKER,
        "RESULT_API_TOKEN": "result-test", "REDIS_URL": "redis://unused",
        "XAI_API_KEY": "xai-test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(main, "redis_client", lambda: fakeredis.FakeRedis(server=server, decode_responses=True))
    monkeypatch.setattr(main, "publish", MagicMock())
    monkeypatch.setattr(main, "analyze", MagicMock(return_value=analysis.watch("Mock analysis")))
    monkeypatch.delenv("XAI_MODEL", raising=False)
    # Tests must never make a paid model request accidentally.
    monkeypatch.setattr(analysis, "OpenAI", MagicMock(side_effect=AssertionError("Unexpected model call")))
    main.app.config.update(TESTING=True)


@pytest.fixture
def client():
    return main.app.test_client()


def queued(payload):
    return {key: value for key, value in payload.items() if key != "secret"}


def test_accepts_only_after_queue_publish(client, payload):
    response = client.post(WEBHOOK, json=payload)
    assert response.status_code == 202
    alert, job_id = main.publish.call_args.args
    assert "secret" not in alert.model_dump()
    assert response.json["job_id"] == job_id
    assert alert.price == 200.5
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("data", [None, [], [1], "hello", 123, True])
def test_rejects_nonobjects(client, data):
    assert client.post(WEBHOOK, data=json.dumps(data), content_type="application/json").status_code == 400
    main.publish.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"ticker": ""}, {"ticker": "x" * 41}, {"signal": "buy"},
    {"price": None}, {"price": True}, {"price": "NaN"}, {"price": "Infinity"},
    {"price": -1}, {"interval": ""}, {"time": "2026-11-26T12:00:00"},
    {"time": 123}, {"unexpected": "field"}, {"candles": [{"close": 1}]},
])
def test_invalid_fields(client, payload, changes):
    payload.update(changes)
    assert client.post(WEBHOOK, json=payload).status_code == 400
    main.publish.assert_not_called()


@pytest.mark.parametrize("secret", [None, [], "wrong", 123])
def test_authentication(client, payload, secret):
    payload["secret"] = secret
    assert client.post(WEBHOOK, json=payload).status_code == 401
    main.publish.assert_not_called()


@pytest.mark.parametrize("hours", [-25, 1])
def test_rejects_stale_or_future_alert(client, payload, hours):
    payload["time"] = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    assert client.post(WEBHOOK, json=payload).status_code == 400


def test_queue_failure_is_retryable_and_redacted(client, payload):
    main.publish.side_effect = RuntimeError("private credentials")
    response = client.post(WEBHOOK, json=payload)
    assert response.status_code == 503
    assert "private credentials" not in response.text


def test_oversized_body(client):
    assert client.post(WEBHOOK, data="x" * (128 * 1024 + 1), content_type="application/json").status_code == 413


def test_missing_config_does_not_break_health(client, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY")
    monkeypatch.delenv("TV_WEBHOOK_SECRET")
    assert client.get("/").status_code == 200
    assert client.post(WEBHOOK, json={}).status_code == 503
    assert client.post(WORKER, json={}, headers=AUTH).status_code == 503


def test_worker_requires_own_token(client, payload):
    assert client.post(WORKER, json=queued(payload)).status_code == 401
    assert client.post(WORKER, json=queued(payload), headers={"Authorization": "Bearer tv-test"}).status_code == 401


def test_worker_stores_result_and_suppresses_duplicates(client, payload, monkeypatch):
    run = MagicMock(return_value=analysis.watch("Insufficient data"))
    monkeypatch.setattr(main, "analyze", run)
    first = client.post(WORKER, json=queued(payload), headers=AUTH)
    second = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert first.status_code == second.status_code == 200
    assert second.json["duplicate"] is True
    run.assert_called_once()
    url = "/api/v1/results/" + first.json["job_id"]
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer result-test"})
    assert response.status_code == 200
    assert response.json["analysis"]["decision"] == "Watch"
    assert "secret" not in response.json["alert"]
    with main.redis_client() as store:
        assert 0 < store.ttl("tv:{" + first.json["job_id"] + "}:result") <= main.RESULT_TTL


def test_duplicate_alert_id_cannot_overwrite_result(client, payload):
    payload["alert_id"] = "unique-strategy-event"
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 200
    payload["price"] = 201
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 409


def test_busy_worker_is_retried(client, payload):
    alert = Alert.model_validate(queued(payload))
    with main.redis_client() as store:
        store.set("tv:{" + main.job_id_for(alert) + "}:lock", "other-worker", ex=120)
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 503


def test_worker_failure_releases_lock_for_retry(client, payload, monkeypatch):
    run = MagicMock(side_effect=RuntimeError("sensitive provider response"))
    monkeypatch.setattr(main, "analyze", run)
    response = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert response.status_code == 503
    assert "sensitive" not in response.text
    run.side_effect = None
    run.return_value = analysis.watch("No candles")
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 200


def test_expired_worker_cannot_commit_or_release_new_lock(client, payload, monkeypatch):
    alert = Alert.model_validate(queued(payload))
    prefix = "tv:{" + main.job_id_for(alert) + "}:"

    def lose_lock(_):
        with main.redis_client() as store:
            store.set(prefix + "lock", "new-owner", ex=120)
        return analysis.watch("No candles")

    monkeypatch.setattr(main, "analyze", lose_lock)
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 503
    with main.redis_client() as store:
        assert store.get(prefix + "result") is None
        assert store.get(prefix + "lock") == "new-owner"


def model_response(payload, **changes):
    recommendation = analysis.watch("Insufficient market history").model_dump(mode="json")
    recommendation.update(changes)
    return SimpleNamespace(
        status="completed", output_text=json.dumps(recommendation), citations=[],
        output=[SimpleNamespace(type="web_search_call", status="completed")],
    )


def install_model(monkeypatch, response):
    sdk = MagicMock()
    sdk.__enter__.return_value = sdk
    sdk.responses.create.return_value = response
    factory = MagicMock(return_value=sdk)
    monkeypatch.setattr(analysis, "OpenAI", factory)
    return sdk, factory


def test_without_candles_grok_is_asked_to_retrieve_data(payload, monkeypatch):
    sdk, _ = install_model(monkeypatch, model_response(payload))
    analysis.analyze(Alert.model_validate(queued(payload)))
    options = sdk.responses.create.call_args.kwargs
    assert options["tools"] == [{"type": "web_search"}]
    assert options["model"] == analysis.DEFAULT_MODEL
    assert "candles" not in json.loads(options["input"])["alert"]
    assert "retrieve market evidence" in options["instructions"]


def test_model_uses_schema_and_validates_response(payload, monkeypatch):
    response = model_response(payload)
    sdk, factory = install_model(monkeypatch, response)
    result = analysis.analyze(Alert.model_validate(queued(payload)))
    assert result.decision == "Watch"
    assert factory.call_args.kwargs["max_retries"] == 0
    assert sdk.responses.create.call_args.kwargs["text"]["format"]["type"] == "json_schema"
    response.output_text = '{"decision": "Buy"}'
    with pytest.raises(ValidationError):
        analysis.analyze(Alert.model_validate(queued(payload)))


def test_buy_requires_consistent_levels():
    result = analysis.watch("Test").model_dump()
    result.update(decision="Buy", buy_price=200, stop_loss=210, sell_target=220, time_limit="5 days")
    with pytest.raises(ValidationError):
        Recommendation.model_validate(result)


def test_job_hash_normalizes_price_and_timezone(payload):
    first = Alert.model_validate(queued(payload))
    payload["price"] = 200.5
    payload["time"] = first.time.astimezone(timezone(timedelta(hours=-5))).isoformat()
    assert main.job_id_for(first) == main.job_id_for(Alert.model_validate(queued(payload)))


def test_publish_transport_contract(payload, monkeypatch):
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={"messageId": "message-test"})

    real_client = httpx.Client
    monkeypatch.setattr(main.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    alert = Alert.model_validate(queued(payload))
    PUBLISH(alert, main.job_id_for(alert))
    request = sent[0]
    assert str(request.url) == "https://qstash-us-east-1.upstash.io/v2/publish/https://example.com" + WORKER
    assert request.headers["Authorization"] == "Bearer queue-test"
    assert request.headers["Upstash-Forward-Authorization"] == "Bearer worker-test"
    assert request.headers["Upstash-Deduplication-Id"] == main.fingerprint(alert)
    assert request.headers["Upstash-Retries"] == "5"
    assert "secret" not in json.loads(request.content)
    assert all(value == 0.5 for value in request.extensions["timeout"].values())


def test_publish_does_not_follow_redirects_or_ack_failed_publish(client, payload, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example.com"})

    real_client = httpx.Client
    monkeypatch.setattr(main, "publish", PUBLISH)
    monkeypatch.setattr(main.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    assert client.post(WEBHOOK, json=payload).status_code == 503
    assert len(calls) == 1


def test_storage_failure_does_not_acknowledge_worker(client, payload, monkeypatch):
    monkeypatch.setattr(main, "redis_client", MagicMock(side_effect=ConnectionError("credentials")))
    response = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert response.status_code == 503
    assert "credentials" not in response.text


def test_result_written_between_lookup_and_lock_is_not_recomputed(client, payload, monkeypatch):
    alert = Alert.model_validate(queued(payload))
    store = main.redis_client()
    # Reproduce another worker committing immediately before this one gets its lock.
    real_set = store.set

    def acquire_after_completion(key, value, **kwargs):
        real_set(key.replace(":lock", ":result"), json.dumps({"fingerprint": main.fingerprint(alert)}))
        return real_set(key, value, **kwargs)

    monkeypatch.setattr(store, "set", acquire_after_completion)
    monkeypatch.setattr(main, "redis_client", lambda: store)
    run = MagicMock()
    monkeypatch.setattr(main, "analyze", run)
    response = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert response.status_code == 200
    assert response.json["duplicate"] is True
    run.assert_not_called()


def test_stale_queued_alert_skips_model(payload):
    data = queued(payload)
    data["time"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert "older than" in analysis.analyze(Alert.model_validate(data)).rationale
    analysis.OpenAI.assert_not_called()


def sourced_buy(payload):
    response = model_response(
        payload, decision="Buy", buy_price=200, stop_loss=190, sell_target=220,
        time_limit="5 trading days", market_data_sufficient=True,
        sources=[{"url": "https://example.com/history", "title": "Market history",
                  "kind": "market_data", "as_of": payload["time"]}],
    )
    response.citations = ["https://example.com/history"]
    return response


def test_cited_fresh_market_evidence_allows_analysis(payload, monkeypatch):
    install_model(monkeypatch, sourced_buy(payload))
    result = analysis.analyze(Alert.model_validate(queued(payload)))
    assert result.decision == "Buy"
    assert result.market_data_sufficient is True
    assert result.sources[0].url == "https://example.com/history"


@pytest.mark.parametrize("failure", ["no_search", "no_citation", "unknown_time", "stale", "future", "news_only", "insufficient"])
def test_missing_market_evidence_downgrades_to_watch(payload, monkeypatch, failure):
    response = sourced_buy(payload)
    data = json.loads(response.output_text)
    if failure == "no_search":
        response.output = []
    elif failure == "no_citation":
        response.citations = []
    elif failure == "unknown_time":
        data["sources"][0]["as_of"] = None
    elif failure in ("stale", "future"):
        days = -8 if failure == "stale" else 1
        data["sources"][0]["as_of"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    elif failure == "news_only":
        data["sources"][0]["kind"] = "news"
    else:
        data["market_data_sufficient"] = False
    response.output_text = json.dumps(data)
    install_model(monkeypatch, response)
    result = analysis.analyze(Alert.model_validate(queued(payload)))
    assert result.decision == "Watch"
    assert result.buy_price is result.stop_loss is result.sell_target is None
    assert result.market_data_sufficient is False


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress"])
def test_incomplete_research_is_retryable(payload, monkeypatch, status):
    response = model_response(payload)
    response.status = status
    install_model(monkeypatch, response)
    with pytest.raises(ValueError, match="Incomplete"):
        analysis.analyze(Alert.model_validate(queued(payload)))


def test_sdk_wire_contract_with_search_annotation(payload, monkeypatch):
    from openai import OpenAI

    def handle(request):
        assert request.url.path == "/v1/responses"
        data = json.loads(request.content)
        assert data["tools"] == [{"type": "web_search"}]
        assert data["store"] is False
        result = sourced_buy(payload)
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1,
            "status": "completed", "model": analysis.DEFAULT_MODEL,
            "output": [
                {"id": "search_test", "type": "web_search_call", "status": "completed",
                 "action": {"type": "search", "query": "AAPL history"}},
                {"id": "message_test", "type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": result.output_text,
                              "annotations": [{"type": "url_citation", "url": "https://example.com/history",
                                               "title": "Market history", "start_index": 0, "end_index": 1}]}]},
            ],
        })

    def factory(**options):
        return OpenAI(**options, http_client=httpx.Client(transport=httpx.MockTransport(handle)))

    monkeypatch.setattr(analysis, "OpenAI", factory)
    assert analysis.analyze(Alert.model_validate(queued(payload))).decision == "Buy"


@pytest.fixture
def rich_payload(payload):
    payload.update({
        "open": 199, "high": 202, "low": 198, "close": 200.5,
        "volume": 1000, "supertrend": 195, "adx": 28,
        "highest20": 205, "lowest20": 185, "avgVol20": 900,
        "avgClose20": 195, "prevCloses": [199, 198, 197, 196, 195],
    })
    return payload


def test_rich_pine_payload_accepted_and_preserved(client, rich_payload):
    del rich_payload["price"]  # Accept the original script's close-only price.
    assert client.post(WEBHOOK, json=rich_payload).status_code == 202
    alert = main.publish.call_args.args[0]
    assert alert.price == alert.close == 200.5
    assert alert.adx == 28
    assert alert.prevCloses == [199, 198, 197, 196, 195]


def test_rich_context_reaches_grok(rich_payload, monkeypatch):
    sdk, _ = install_model(monkeypatch, model_response(rich_payload))
    analysis.analyze(Alert.model_validate(queued(rich_payload)))
    data = json.loads(sdk.responses.create.call_args.kwargs["input"])["alert"]
    assert data["supertrend"] == 195
    assert data["highest20"] == 205
    assert data["prevCloses"] == [199, 198, 197, 196, 195]


@pytest.mark.parametrize("changes", [
    {"adx": 101}, {"adx": -1}, {"volume": -1}, {"avgVol20": "NaN"},
    {"supertrend": True}, {"close": 201}, {"high": 199}, {"low": 201},
    {"highest20": 180}, {"prevCloses": [1, 2]},
    {"prevCloses": [1, 2, 3, 4, True]},
])
def test_invalid_rich_fields_rejected(client, rich_payload, changes):
    rich_payload.update(changes)
    assert client.post(WEBHOOK, json=rich_payload).status_code == 400
    main.publish.assert_not_called()


def test_missing_volume_is_accepted_as_unknown(client, rich_payload):
    rich_payload.update(volume=None, avgVol20=None)
    assert client.post(WEBHOOK, json=rich_payload).status_code == 202
    assert main.publish.call_args.args[0].volume is None
