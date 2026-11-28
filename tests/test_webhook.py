import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import fakeredis
import httpx
import pytest

from recommendations import watch
import main
from grok_bot import TTL as GROK_RESULT_TTL
from models import Alert

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
        "GROK_WEBHOOK_URL": "https://bot.example/webhook", "GROK_WEBHOOK_KEY": "bot-test",
        "GROK_CALLBACK_TOKEN": "callback-test",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(main, "redis_client", lambda: fakeredis.FakeRedis(server=server, decode_responses=True))
    monkeypatch.setattr(main, "publish", MagicMock())
    def pending(store, alert, job_id, fingerprint):
        store.set("tv:{" + job_id + "}:bot_pending", json.dumps({
            "fingerprint": fingerprint, "alert": alert.model_dump(mode="json")}), ex=86400)
    monkeypatch.setattr(main, "dispatch_grok_bot", MagicMock(side_effect=pending))
    main.app.config.update(TESTING=True)


@pytest.fixture
def client():
    return main.app.test_client()


def queued(payload):
    return {key: value for key, value in payload.items() if key != "secret"}


def complete(client, payload, recommendation=None):
    from grok_bot import CALLBACK_PATH
    processed = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert processed.status_code == 202
    return client.post(CALLBACK_PATH, headers={"Authorization": "Bearer callback-test"}, json={
        "job_id": processed.json["job_id"],
        "status": "completed", "ticker": payload["ticker"],
        "recommendation": {"action": "wait"}, "analysis": {},
        "summary": (recommendation or watch("Insufficient data")).recommendation_text,
        "reasons": [(recommendation or watch("Insufficient data")).rationale],
        "recommendation_text": (recommendation or watch("Insufficient data")).recommendation_text,
        "notification_text": (recommendation or watch("Insufficient data")).notification_text,
    })


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
    monkeypatch.delenv("GROK_WEBHOOK_KEY")
    monkeypatch.delenv("TV_WEBHOOK_SECRET")
    assert client.get("/").status_code == 200
    assert client.get("/health").json == {"status": "ok"}
    assert client.post(WEBHOOK, json={}).status_code == 503
    assert client.post(WORKER, json={}, headers=AUTH).status_code == 503


def test_worker_requires_own_token(client, payload):
    assert client.post(WORKER, json=queued(payload)).status_code == 401
    assert client.post(WORKER, json=queued(payload), headers={"Authorization": "Bearer tv-test"}).status_code == 401


def test_worker_dispatches_and_callback_stores_result_without_redispatch(client, payload):
    processed = complete(client, payload)
    assert processed.status_code == 200
    second = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert second.status_code == 200 and second.json["duplicate"] is True
    main.dispatch_grok_bot.assert_called_once()
    url = main.RESULTS_PATH + "/" + processed.json["job_id"]
    assert client.get(url).status_code == 401
    response = client.get(url, headers={"Authorization": "Bearer result-test"})
    assert response.status_code == 200
    assert response.json["analysis"]["decision"] == "Watch"
    assert "secret" not in response.json["alert"]
    with main.redis_client() as store:
        assert 0 < store.ttl("tv:{" + processed.json["job_id"] + "}:result") <= GROK_RESULT_TTL


def test_duplicate_alert_id_cannot_overwrite_result(client, payload):
    payload["alert_id"] = "unique-strategy-event"
    assert complete(client, payload).status_code == 200
    payload["price"] = 201
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 409


def test_busy_worker_is_retried(client, payload):
    alert = Alert.model_validate(queued(payload))
    with main.redis_client() as store:
        store.set("tv:{" + main.job_id_for(alert) + "}:lock", "other-worker", ex=120)
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 503


def test_worker_failure_releases_lock_for_retry(client, payload):
    main.dispatch_grok_bot.side_effect = RuntimeError("sensitive provider response")
    response = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert response.status_code == 503 and "sensitive" not in response.text
    main.dispatch_grok_bot.side_effect = None
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 202


def test_expired_dispatch_worker_cannot_release_new_lock(client, payload):
    alert = Alert.model_validate(queued(payload))
    prefix = "tv:{" + main.job_id_for(alert) + "}:"
    def lose_lock(*args):
        with main.redis_client() as store:
            store.set(prefix + "lock", "new-owner", ex=120)
    main.dispatch_grok_bot.side_effect = lose_lock
    assert client.post(WORKER, json=queued(payload), headers=AUTH).status_code == 202
    with main.redis_client() as store:
        assert store.get(prefix + "result") is None
        assert store.get(prefix + "lock") == "new-owner"


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
    assert request.headers["Upstash-Timeout"] == f"{main.QSTASH_DELIVERY_TIMEOUT}s"
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
        real_set(key.replace(":lock", ":result"), json.dumps({"fingerprint": main.fingerprint(alert), "status": "completed"}))
        return real_set(key, value, **kwargs)

    monkeypatch.setattr(store, "set", acquire_after_completion)
    monkeypatch.setattr(main, "redis_client", lambda: store)
    run = MagicMock()
    monkeypatch.setattr(main, "dispatch_grok_bot", run)
    response = client.post(WORKER, json=queued(payload), headers=AUTH)
    assert response.status_code == 200
    assert response.json["duplicate"] is True
    run.assert_not_called()


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
    assert client.post(WEBHOOK, json=rich_payload).status_code == 202
    alert = main.publish.call_args.args[0]
    assert alert.price == alert.close == 200.5
    assert alert.adx == 28
    assert alert.prevCloses == [199, 198, 197, 196, 195]


def test_rich_context_reaches_bot_worker(client, rich_payload):
    assert client.post(WORKER, json=queued(rich_payload), headers=AUTH).status_code == 202
    data = main.dispatch_grok_bot.call_args.args[1].model_dump(mode="json")
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


RESULT_AUTH = {"Authorization": "Bearer result-test"}


def test_listing_traverses_all_completed_results(client):
    expected = set()
    with main.redis_client() as store:
        for index in range(75):
            job_id = f"{index:064x}"
            expected.add(job_id)
            store.set("tv:{" + job_id + "}:result", json.dumps({"job_id": job_id, "analysis": {"decision": "Watch"}}), ex=100)
            store.set("tv:{" + job_id + "}:lock", "owner", ex=100)
        store.set("unrelated:data", "not a result")
    found = set()
    cursor = "0"
    for _ in range(100):
        response = client.get(main.RESULTS_PATH, query_string={"count": "7", "cursor": cursor}, headers=RESULT_AUTH)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert all(record["analysis"]["decision"] == "Watch" for record in response.json["results"])
        assert all("recommendation_text" in record for record in response.json["results"])
        found.update(record["job_id"] for record in response.json["results"])
        cursor = response.json["next_cursor"]
        assert isinstance(cursor, str)
        if cursor == "0":
            break
    else:
        pytest.fail("Listing did not finish")
    assert found == expected


def test_empty_result_listing(client):
    response = client.get(main.RESULTS_PATH, headers=RESULT_AUTH)
    assert response.status_code == 200
    assert response.json == {"results": [], "next_cursor": "0"}


@pytest.mark.parametrize("suffix", ["", "/" + "a" * 64])
def test_both_result_routes_require_result_token(client, suffix):
    assert client.get(main.RESULTS_PATH + suffix).status_code == 401
    assert client.get(main.RESULTS_PATH + suffix, headers=AUTH).status_code == 401


@pytest.mark.parametrize("query", [
    {"cursor": "-1"}, {"cursor": "abc"}, {"cursor": "1.5"},
    {"cursor": str(2**64)}, {"cursor": ""}, {"cursor": "١"},
    {"count": "0"}, {"count": "51"}, {"count": "abc"}, {"count": "1.5"},
])
def test_listing_rejects_invalid_pagination(client, query):
    assert client.get(main.RESULTS_PATH, query_string=query, headers=RESULT_AUTH).status_code == 400


def test_listing_skips_results_expired_during_scan(client, monkeypatch):
    store = main.redis_client()
    key = "tv:{" + "a" * 64 + "}:result"
    store.set(key, '{"job_id": "expired"}')
    real_scan = store.scan

    def scan_then_expire(**kwargs):
        result = real_scan(**kwargs)
        store.delete(key)
        return result

    monkeypatch.setattr(store, "scan", scan_then_expire)
    monkeypatch.setattr(main, "redis_client", lambda: store)
    response = client.get(main.RESULTS_PATH, headers=RESULT_AUTH)
    assert response.status_code == 200
    assert response.json["results"] == []


@pytest.mark.parametrize("suffix", ["", "/" + "a" * 64])
def test_result_storage_errors_are_redacted(client, monkeypatch, suffix):
    monkeypatch.setattr(main, "redis_client", MagicMock(side_effect=ConnectionError("private credentials")))
    response = client.get(main.RESULTS_PATH + suffix, headers=RESULT_AUTH)
    assert response.status_code == 503
    assert "private credentials" not in response.text


def test_specific_result_validation_and_missing_record(client):
    assert client.get(main.RESULTS_PATH + "/invalid", headers=RESULT_AUTH).status_code == 400
    assert client.get(main.RESULTS_PATH + "/" + "a" * 64, headers=RESULT_AUTH).status_code == 404


def test_old_result_routes_removed(client):
    assert client.get("/api/v1/results", headers=RESULT_AUTH).status_code == 404
    assert client.get("/api/v1/results/" + "a" * 64, headers=RESULT_AUTH).status_code == 404


def test_dispatch_queue_and_lock_budget():
    from pathlib import Path
    config = json.loads((Path(main.__file__).parent / "vercel.json").read_text())
    duration = config["functions"]["main.py"]["maxDuration"]
    assert 10 < duration < main.QSTASH_DELIVERY_TIMEOUT < main.LOCK_TTL


@pytest.fixture
def enriched_payload(rich_payload):
    end = datetime.fromisoformat(rich_payload["time"]) - timedelta(seconds=1)
    rich_payload.update({
        "strategy_id": "st-adx-v1", "script_version": "2.0.0", "currency": "USD",
        "instrument_type": "stock", "exchange_timezone": "America/New_York",
        "chart_session": "regular", "session_state": "regular",
        "is_standard_chart": True, "bar_confirmed": True,
        "bar_open_time": (end - timedelta(minutes=5)).isoformat(),
        "bar_close_time": end.isoformat(), "atr": 2.5,
        "atr_pct": 2.5 / 200.5 * 100, "di_plus": 30, "di_minus": 15,
        "indicator_settings": {
            "atr_period": 10, "atr_multiplier": 3.0, "adx_length": 14,
            "adx_smoothing": 14, "adx_threshold": 25,
        },
    })
    return rich_payload


def test_enriched_alert_accepted_and_sent_to_bot(client, enriched_payload):
    assert client.post(WEBHOOK, json=enriched_payload).status_code == 202
    alert = main.publish.call_args.args[0]
    assert client.post(WORKER, json=queued(enriched_payload), headers=AUTH).status_code == 202
    data = main.dispatch_grok_bot.call_args.args[1].model_dump(mode="json")
    assert data["currency"] == "USD"
    assert data["is_standard_chart"] is True
    assert data["bar_confirmed"] is True
    assert data["atr"] == 2.5
    assert data["di_minus"] == 15
    assert data["indicator_settings"]["adx_smoothing"] == 14
    assert datetime.fromisoformat(data["bar_close_time"]) == alert.bar_close_time
    assert "secret" not in data


@pytest.mark.parametrize("changes", [
    {"atr": -1}, {"atr_pct": "Infinity"}, {"di_plus": True}, {"di_minus": -1},
    {"is_standard_chart": "true"}, {"bar_confirmed": 1},
    {"session_state": "unknown"}, {"currency": ""},
    {"bar_open_time": "2026-11-27T10:00:00"},
    {"bar_close_time": 123}, {"indicator_settings": {"atr_period": 10}},
])
def test_enriched_fields_are_validated(client, enriched_payload, changes):
    enriched_payload.update(changes)
    assert client.post(WEBHOOK, json=enriched_payload).status_code == 400


def test_bar_timestamps_must_be_consistent(client, enriched_payload):
    enriched_payload["bar_close_time"] = enriched_payload["bar_open_time"]
    assert client.post(WEBHOOK, json=enriched_payload).status_code == 400
    enriched_payload["bar_close_time"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    assert client.post(WEBHOOK, json=enriched_payload).status_code == 400


@pytest.mark.parametrize("changes", [
    {"atr_period": True}, {"atr_period": 0}, {"atr_multiplier": -1},
    {"adx_threshold": 101}, {"adx_smoothing": 0},
])
def test_indicator_parameters_are_validated(client, enriched_payload, changes):
    enriched_payload["indicator_settings"].update(changes)
    assert client.post(WEBHOOK, json=enriched_payload).status_code == 400


def test_result_has_structured_and_readable_views(client, enriched_payload):
    processed = complete(client, enriched_payload, watch("No reliable intraday history found."))
    assert processed.status_code == 200
    path = main.RESULTS_PATH + "/" + processed.json["job_id"]
    response = client.get(path, headers=RESULT_AUTH)
    assert response.status_code == 200
    assert response.json["analysis"]["decision"] == "Watch"
    assert response.json["recommendation_text"].startswith("WATCH")
    assert "No reliable intraday history found." in response.json["recommendation_text"]
    plain = client.get(path + "?format=text", headers=RESULT_AUTH)
    assert plain.status_code == 200 and plain.mimetype == "text/plain"
    assert plain.headers["Cache-Control"] == "no-store"
    assert plain.text == response.json["recommendation_text"]
    assert client.get(path + "?format=text").status_code == 401
    assert client.get(path + "?format=html", headers=RESULT_AUTH).status_code == 400


def test_close_only_payload_is_not_accepted_as_price(client, rich_payload):
    del rich_payload["price"]
    assert client.post(WEBHOOK, json=rich_payload).status_code == 400
    main.publish.assert_not_called()
