import hashlib
import hmac
import json

import fakeredis
import pytest

import main
from whatsapp import EVENT_TTL, MAX_PAYLOAD_BYTES, WHATSAPP_PATH


@pytest.fixture
def inbox(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-test")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret-test")
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "15555550123")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    server = fakeredis.FakeServer()
    factory = lambda: fakeredis.FakeRedis(server=server, decode_responses=True)
    monkeypatch.setattr(main, "redis_client", factory)
    main.app.config.update(TESTING=True)
    return main.app.test_client(), factory


def event():
    return {"object": "whatsapp_business_account", "entry": [{"id": "account-test", "changes": [{
        "field": "messages", "value": {"metadata": {"phone_number_id": "business-test"},
        "messages": [{"id": "wamid.test", "from": "15555550123", "type": "text", "text": {"body": "Hello"}}]},
    }]}]}


def deliver(client, payload=None, raw=None, secret="app-secret-test"):
    if raw is None:
        raw = json.dumps(payload if payload is not None else event()).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(WHATSAPP_PATH, data=raw, content_type="application/json",
                       headers={"X-Hub-Signature-256": signature})


def verify(client, **changes):
    query = {"hub.mode": "subscribe", "hub.verify_token": "verify-test", "hub.challenge": "001234"}
    query.update(changes)
    return client.get(WHATSAPP_PATH, query_string=query)


def test_verification_is_exact_plaintext_and_independent_of_inbox(inbox, monkeypatch):
    client, _ = inbox
    monkeypatch.delenv("WHATSAPP_APP_SECRET")
    monkeypatch.delenv("REDIS_URL")
    response = verify(client)
    assert response.status_code == 200
    assert response.data == b"001234"
    assert response.mimetype == "text/plain"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("changes", [{"hub.verify_token": "wrong"}, {"hub.mode": "wrong"}, {"hub.verify_token": "\u00e9"}])
def test_verification_auth(inbox, changes):
    assert verify(inbox[0], **changes).status_code == 403


@pytest.mark.parametrize("challenge", [None, "", "abc", "<script>", "\u0661", "1" * 129])
def test_invalid_challenge(inbox, challenge):
    assert verify(inbox[0], **{"hub.challenge": challenge}).status_code == 400


def test_missing_verification_config(inbox, monkeypatch):
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN")
    assert verify(inbox[0]).status_code == 503


def test_signed_message_persisted_before_acknowledgement(inbox):
    client, factory = inbox
    payload = event()
    response = deliver(client, payload)
    assert response.status_code == 200
    assert response.json["duplicate"] is False
    with factory() as store:
        key = "wa:webhook:" + response.json["event_id"]
        saved = json.loads(store.get(key))
        assert saved["payload"] == payload
        assert saved["received_at"]
        assert EVENT_TTL - 2 <= store.ttl(key) <= EVENT_TTL


def test_duplicate_envelope_not_overwritten_or_expiry_extended(inbox):
    client, factory = inbox
    first = deliver(client)
    key = "wa:webhook:" + first.json["event_id"]
    with factory() as store:
        original = store.get(key)
        store.expire(key, 123)
    response = deliver(client, raw=json.dumps(event(), sort_keys=True, indent=2).encode())
    assert response.status_code == 200
    assert response.json["duplicate"] is True
    assert response.json["event_id"] == first.json["event_id"]
    with factory() as store:
        assert store.get(key) == original
        assert 120 <= store.ttl(key) <= 123


def test_delivery_statuses_and_unknown_message_types_preserved(inbox):
    payload = event()
    value = payload["entry"][0]["changes"][0]["value"]
    value["statuses"] = [{"id": "wamid.outgoing", "status": "delivered", "recipient_id": "15555550123"}]
    value["messages"][0] = {"id": "wamid.new", "type": "future_type", "user_id": "opaque-id", "from": "15555550123"}
    response = deliver(inbox[0], payload)
    assert response.status_code == 200
    with inbox[1]() as store:
        assert json.loads(store.get("wa:webhook:" + response.json["event_id"]))["payload"] == payload


@pytest.mark.parametrize("secret", ["wrong", "verify-test"])
def test_wrong_signature_cannot_store(inbox, secret):
    assert deliver(inbox[0], secret=secret).status_code == 401
    with inbox[1]() as store:
        assert store.dbsize() == 0


def test_requires_signature_even_for_invalid_json(inbox):
    assert inbox[0].post(WHATSAPP_PATH, data=b"bad-json").status_code == 401


@pytest.mark.parametrize("raw", [b"invalid", b"\xff", b"[]", b"{}", b'{"object":"other","entry":[]}',
    b'{"object":"whatsapp_business_account","entry":[null]}',
    b'{"object":"whatsapp_business_account","entry":[{"changes":{}}]}',
    b'{"object":"whatsapp_business_account","entry":[],"bad":"\\ud800"}',
    b'{"object":"whatsapp_business_account","entry":[],"bad":NaN}'])
def test_invalid_signed_payload(inbox, raw):
    assert deliver(inbox[0], raw=raw).status_code == 400


@pytest.mark.parametrize("variable", ["WHATSAPP_APP_SECRET", "REDIS_URL"])
def test_missing_receiving_configuration(inbox, monkeypatch, variable):
    monkeypatch.delenv(variable)
    assert deliver(inbox[0]).status_code == 503


def test_storage_error_requests_retry_without_logging_content(inbox, monkeypatch, caplog):
    def unavailable():
        raise ConnectionError("private-credential")
    monkeypatch.setattr(main, "redis_client", unavailable)
    response = deliver(inbox[0])
    assert response.status_code == 503
    assert "private-credential" not in caplog.text
    assert "15555550123" not in caplog.text
    assert "Hello" not in caplog.text


def test_whatsapp_batch_size_independent_of_tradingview_limit(inbox, monkeypatch):
    for name in ("TV_WEBHOOK_SECRET", "QSTASH_URL", "QSTASH_TOKEN", "WORKER_TOKEN", "WORKER_URL"):
        monkeypatch.setenv(name, "test-configured")
    payload = event()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "x" * (129 * 1024)
    assert deliver(inbox[0], payload).status_code == 200
    assert inbox[0].post("/api/v1/tv-alert-x7k9m2p", data=b"x" * (129 * 1024),
                         content_type="application/json").status_code == 413
    assert deliver(inbox[0], raw=b"x" * (MAX_PAYLOAD_BYTES + 1)).status_code == 413
