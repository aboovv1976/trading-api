import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import whatsapp_commands as commands
from test_whatsapp import deliver, event, inbox


@pytest.fixture
def sending(inbox, monkeypatch):
    values = {"WHATSAPP_PHONE_NUMBER_ID": "123456789", "WHATSAPP_ACCESS_TOKEN": "access-test",
              "WORKER_URL": "https://example.test/api/v1/worker", "WORKER_TOKEN": "worker-test",
              "QSTASH_URL": "https://qstash.test", "QSTASH_TOKEN": "queue-test", "RESULT_API_TOKEN": "result-test",
              "GROK_CALLBACK_TOKEN": "callback-test", "GROK_CHAT_WEBHOOK_URL": "https://chat-bot.test/webhook",
              "GROK_CHAT_WEBHOOK_KEY": "chat-key-test",
              "GROK_WEBHOOK_URL": "https://analysis-bot.test/webhook", "GROK_WEBHOOK_KEY": "analysis-key-test"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("WHATSAPP_GRAPH_API_VERSION", raising=False)
    requests = []
    def handle(request):
        assert request.url.host != "api.x.ai", "No direct model calls are permitted"
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.reply"}]})
    original = httpx.Client
    monkeypatch.setattr(commands.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    return inbox[0], inbox[1], requests


def command_payload(text="/latest NVDA, AAPL"):
    payload = event()
    value = payload["entry"][0]["changes"][0]["value"]
    value["metadata"]["phone_number_id"] = "123456789"
    value["messages"][0].update(timestamp=str(int(time.time())), text={"body": text})
    return payload


def run_worker(sending):
    client, _, requests = sending
    queued = json.loads(next(r for r in requests if r.url.host == "qstash.test").content)
    return client.post(commands.WORKER_PATH, json=queued, headers={"Authorization": "Bearer worker-test"})


def seed(factory, ticker, count=4):
    with factory() as store:
        for i in range(count):
            job = hashlib.sha256((ticker + str(i)).encode()).hexdigest()
            record = {"job_id": job, "status": "completed", "completed_at": (datetime.now(timezone.utc) - timedelta(minutes=i)).isoformat(),
                      "alert": {"ticker": ticker, "interval": "5"},
                      "analysis": {"decision": "Watch", "notification_text": f"{ticker} recommendation {i}",
                                   "recommendation_text": "Full report"}}
            store.set("tv:{" + job + "}:result", json.dumps(record), ex=604800)


def graph_requests(sending):
    return [r for r in sending[2] if r.url.host == "graph.facebook.com"]


def test_latest_comma_list_returns_only_newest_per_ticker(sending):
    seed(sending[1], "NASDAQ:NVDA")
    seed(sending[1], "NASDAQ:AAPL")
    assert deliver(sending[0], command_payload()).status_code == 200
    assert not graph_requests(sending)  # No sends before queued worker runs.
    assert run_worker(sending).status_code == 200
    sent = graph_requests(sending)
    assert len(sent) == 2
    for request, ticker in zip(sent, ("NASDAQ:NVDA", "NASDAQ:AAPL")):
        body = json.loads(request.content)
        assert body["to"] == "15555550123"
        assert body["context"]["message_id"] == "wamid.test"
        assert request.headers["Authorization"] == "Bearer access-test"
        assert str(request.url).endswith("/v25.0/123456789/messages")
        text = body["text"]["body"]
        assert ticker + " recommendation 0" in text
        for older in (1, 2, 3):
            assert f"recommendation {older}" not in text
        assert "https://example.test/api/v1/reports-" in text
        assert "access-test" not in text and "result-test" not in text
        assert len(text) <= 4096


def test_repeated_delivery_and_worker_do_not_repeat_reply(sending):
    payload = command_payload("/help")
    assert deliver(sending[0], payload).status_code == 200
    assert run_worker(sending).status_code == 200
    assert deliver(sending[0], payload).status_code == 200
    assert run_worker(sending).status_code == 200
    assert len(graph_requests(sending)) == 1


def test_ordinary_chat_is_queued_without_sending_in_ingress(sending):
    assert deliver(sending[0], command_payload("I like NVDA")).status_code == 200
    assert len(sending[2]) == 1 and sending[2][0].url.host == "qstash.test"
    assert not graph_requests(sending)


@pytest.mark.parametrize("text", ["/help", "/unknown", "/all", "/latest", "/latest NVDA,,AAPL", "/latest " + ",".join(["A"] * 11)])
def test_help_and_invalid_commands(sending, text):
    deliver(sending[0], command_payload(text))
    assert run_worker(sending).status_code == 200
    assert json.loads(graph_requests(sending)[0].content)["text"]["body"] == commands.HELP


def test_ambiguous_symbol_requires_exchange(sending):
    seed(sending[1], "NASDAQ:ABC")
    seed(sending[1], "NYSE:ABC")
    deliver(sending[0], command_payload("/latest ABC"))
    run_worker(sending)
    text = json.loads(graph_requests(sending)[0].content)["text"]["body"]
    assert "ambiguous" in text and "NASDAQ:ABC" in text and "NYSE:ABC" in text
    assert "recommendation 0" not in text


def test_exact_exchange_does_not_match_other_exchange(sending):
    seed(sending[1], "NASDAQ:ABC")
    seed(sending[1], "NYSE:ABC")
    deliver(sending[0], command_payload("/latest NYSE:ABC"))
    run_worker(sending)
    text = json.loads(graph_requests(sending)[0].content)["text"]["body"]
    assert "NYSE:ABC recommendation 0" in text and "NASDAQ:ABC" not in text


def test_missing_results_request_analysis(sending):
    deliver(sending[0], command_payload("/latest NVDA"))
    run_worker(sending)
    assert "Requesting a daily analysis" in json.loads(graph_requests(sending)[0].content)["text"]["body"]
    assert len([r for r in sending[2] if r.url.host == "analysis-bot.test"]) == 1


@pytest.mark.parametrize("configured", ["", "19999999999"])
def test_allowlist_fail_closed_before_storage_or_queue(sending, monkeypatch, configured):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", configured)
    response = deliver(sending[0], command_payload())
    assert response.status_code == 200 and response.json["status"] == "ignored"
    assert not sending[2]
    with sending[1]() as store:
        assert store.dbsize() == 0


def test_mixed_batch_strips_disallowed_messages_and_contacts(sending):
    payload = command_payload("Hello")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "blocked", "from": "19999999999", "text": {"body": "private-blocked-text"}})
    value["contacts"] = [{"wa_id": "19999999999", "profile": {"name": "blocked-name"}}]
    response = deliver(sending[0], payload)
    assert response.status_code == 200
    with sending[1]() as store:
        record = store.get("wa:webhook:" + response.json["event_id"])
        assert "private-blocked-text" not in record and "blocked-name" not in record


def test_allowlist_rechecked_by_worker(sending, monkeypatch):
    deliver(sending[0], command_payload("/help"))
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "")
    assert run_worker(sending).status_code == 200
    assert not graph_requests(sending)


def test_expired_message_not_replied_to(sending):
    payload = command_payload("/help")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(int(time.time()) - 86401)
    deliver(sending[0], payload)
    assert run_worker(sending).status_code == 200
    assert not graph_requests(sending)


def test_worker_authentication_and_payload(sending):
    assert sending[0].post(commands.WORKER_PATH, json={"job_id": "a" * 64}).status_code == 401
    assert sending[0].post(commands.WORKER_PATH, json={"job_id": "invalid"}, headers={"Authorization": "Bearer worker-test"}).status_code == 400
    assert not graph_requests(sending)


def test_rebatched_incoming_message_not_replied_to_twice(sending):
    payload = command_payload("/help")
    deliver(sending[0], payload)
    run_worker(sending)
    payload["extra"] = "different-envelope"
    deliver(sending[0], payload)
    run_worker(sending)
    assert len(graph_requests(sending)) == 1


def test_wrong_business_number_does_not_send(sending):
    payload = command_payload("/help")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999999999"
    assert deliver(sending[0], payload).status_code == 200
    assert not sending[2]


@pytest.mark.parametrize("text", ["/latest all", "/LATEST ALL", "/latest All"])
def test_all_lists_only_available_symbols_without_recommendation_bodies(sending, text):
    seed(sending[1], "NYSE:ZZZ")
    seed(sending[1], "NASDAQ:AAPL")
    deliver(sending[0], command_payload(text))
    assert run_worker(sending).status_code == 200
    body = json.loads(graph_requests(sending)[0].content)["text"]["body"]
    assert body == "Available tickers with stored recommendations:\nNASDAQ:AAPL\nNYSE:ZZZ"
    assert "recommendation 0" not in body


def test_all_handles_empty_results(sending):
    deliver(sending[0], command_payload("/latest all"))
    run_worker(sending)
    assert "No tickers" in json.loads(graph_requests(sending)[0].content)["text"]["body"]


def test_expired_and_invalid_results_not_listed(sending):
    seed(sending[1], "NASDAQ:NVDA")
    with sending[1]() as store:
        store.set("tv:{bad}:result", "invalid JSON")
        for ticker, analysis_data, completed in [
            ("NYSE:OLD", {"decision": "Watch"}, datetime.now(timezone.utc) - timedelta(days=8)),
            ("NYSE:EMPTY", None, datetime.now(timezone.utc)),
        ]:
            store.set("tv:{" + ticker + "}:result", json.dumps({"job_id": "a" * 64,
                "completed_at": completed.isoformat(), "alert": {"ticker": ticker}, "analysis": analysis_data}))
    deliver(sending[0], command_payload("/latest all"))
    run_worker(sending)
    text = json.loads(graph_requests(sending)[0].content)["text"]["body"]
    assert "NASDAQ:NVDA" in text and "NYSE:OLD" not in text and "NYSE:EMPTY" not in text


def fake_http(monkeypatch, side_effect):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.side_effect = side_effect
    monkeypatch.setattr(commands.httpx, "Client", lambda **kwargs: client)
    return client


def test_uncertain_send_timeout_is_not_sent_again(sending, monkeypatch):
    deliver(sending[0], command_payload("/help"))
    http = fake_http(monkeypatch, httpx.ReadTimeout("private-provider-message"))
    assert run_worker(sending).status_code == 503
    result = run_worker(sending)
    assert result.status_code == 200 and result.json["status"] == "delivery_uncertain"
    assert http.post.call_count == 1
    with sending[1]() as store:
        statuses = list(store.scan_iter(match="wa:command:*:part:0"))
        assert len(statuses) == 1 and store.get(statuses[0]) == "unknown"


def test_rate_limit_retries_without_resending_accepted_part(sending, monkeypatch):
    deliver(sending[0], command_payload("/help"))
    request = httpx.Request("POST", "https://graph.facebook.com/test")
    http = fake_http(monkeypatch, [httpx.Response(429, request=request), httpx.Response(200, request=request)])
    assert run_worker(sending).status_code == 503
    assert run_worker(sending).status_code == 200
    assert run_worker(sending).status_code == 200
    assert http.post.call_count == 2


def test_queue_failure_preserves_command_and_retries_enqueue(sending, monkeypatch):
    from unittest.mock import MagicMock
    original_client = commands.httpx.Client
    request = httpx.Request("POST", "https://qstash.test/test")
    fake_http(monkeypatch, [httpx.Response(503, request=request)])
    payload = command_payload("/help")
    assert deliver(sending[0], payload).status_code == 503
    with sending[1]() as store:
        assert list(store.scan_iter(match="wa:command:*"))
    monkeypatch.setattr(commands.httpx, "Client", original_client)
    assert deliver(sending[0], payload).status_code == 200
    assert run_worker(sending).status_code == 200
    assert len(graph_requests(sending)) == 1


def test_long_symbol_list_is_split_into_bounded_replies(sending):
    for i in range(250):
        seed(sending[1], "EXCHANGE:SYMBOL" + str(i), count=1)
    deliver(sending[0], command_payload("/latest all"))
    assert run_worker(sending).status_code == 200
    texts = [json.loads(request.content)["text"]["body"] for request in graph_requests(sending)]
    assert len(texts) > 1
    assert all(len(text) <= 3500 for text in texts)
    assert all("EXCHANGE:SYMBOL" + str(i) in "\n".join(texts).splitlines() for i in range(250))


def test_meta_redirect_is_not_reported_as_success(sending, monkeypatch):
    deliver(sending[0], command_payload("/help"))
    request = httpx.Request("POST", "https://graph.facebook.com/test")
    http = fake_http(monkeypatch, [httpx.Response(302, headers={"Location": "https://other.test"}, request=request)])
    assert run_worker(sending).status_code == 503
    result = run_worker(sending)
    assert result.status_code == 200 and result.json["status"] == "delivery_failed"
    assert run_worker(sending).json["status"] == "delivery_failed"
    assert http.post.call_count == 1


def test_unapproved_sender_log_explains_ignore_without_message_or_phone(sending, monkeypatch, caplog):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "19999999999")
    body = command_payload("private message text")
    assert deliver(sending[0], body).status_code == 200
    assert "reason=sender_not_allowed" in caplog.text
    assert "private message text" not in caplog.text and "15555550123" not in caplog.text


def test_phone_id_mismatch_log_explains_missing_queue(sending, caplog):
    payload = command_payload("private message text")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "other-id"
    assert deliver(sending[0], payload).status_code == 200
    assert "reason=business_phone_mismatch" in caplog.text
    assert not sending[2]
    assert "private message text" not in caplog.text


def test_meta_error_logs_codes_without_provider_message(sending, monkeypatch, caplog):
    deliver(sending[0], command_payload("/help"))
    request = httpx.Request("POST", "https://graph.facebook.com/test")
    fake_http(monkeypatch, [httpx.Response(401, request=request, json={"error": {"code": 190,
        "error_subcode": 463, "message": "private provider details"}})])
    assert run_worker(sending).status_code == 503
    assert "http_status=401 code=190 subcode=463" in caplog.text
    assert "private provider details" not in caplog.text
