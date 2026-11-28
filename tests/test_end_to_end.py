"""Real app routes and Redis state across both bot workflows; HTTP is mocked."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import fakeredis
import httpx

import grok_bot
import main
from test_grok_bot import callback as analysis_reply
from test_webhook import PUBLISH
from test_whatsapp import deliver
from test_whatsapp_commands import command_payload


def test_alert_report_and_whatsapp_conversation_round_trip(monkeypatch):
    values = {"TV_WEBHOOK_SECRET": "tv-test", "QSTASH_URL": "https://queue.test", "QSTASH_TOKEN": "queue-test",
        "WORKER_TOKEN": "worker-test", "WORKER_URL": "https://production.test/api/v1/analyze-e8f8323f5eecdfce",
        "REDIS_URL": "redis://unused", "RESULT_API_TOKEN": "result-test", "GROK_CALLBACK_TOKEN": "callback-test",
        "GROK_WEBHOOK_URL": "https://analysis-bot.test/webhook", "GROK_WEBHOOK_KEY": "analysis-key-test",
        "GROK_CHAT_WEBHOOK_URL": "https://chat-bot.test/webhook", "GROK_CHAT_WEBHOOK_KEY": "chat-key-test",
        "WHATSAPP_APP_SECRET": "app-secret-test", "WHATSAPP_PHONE_NUMBER_ID": "123456789",
        "WHATSAPP_ALLOWED_NUMBERS": "15555550123", "WHATSAPP_ACCESS_TOKEN": "access-test"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(main, "redis_client", lambda: fakeredis.FakeRedis(server=server, decode_responses=True))
    monkeypatch.setattr(main, "publish", PUBLISH)
    monkeypatch.setattr(main, "dispatch_grok_bot", grok_bot.dispatch)
    queued, analysis_requests, chat_requests, sent = [], [], [], []
    def http(request):
        body = json.loads(request.content)
        host = request.url.host
        if host == "queue.test":
            destination = str(request.url).split("/v2/publish/", 1)[1]
            queued.append((urlsplit(destination).path, body))
        elif host == "analysis-bot.test":
            assert request.headers["Authorization"] == "Bearer analysis-key-test"
            analysis_requests.append(body)
        elif host == "chat-bot.test":
            assert request.headers["Authorization"] == "Bearer chat-key-test"
            chat_requests.append(body)
        elif host == "graph.facebook.com":
            assert request.headers["Authorization"] == "Bearer access-test"
            sent.append(body)
        else:
            raise AssertionError("Unexpected external host: " + host)
        return httpx.Response(200, json={"accepted": True})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(http), **kwargs))
    main.app.config.update(TESTING=True)
    client = main.app.test_client()
    worker_auth = {"Authorization": "Bearer worker-test"}
    callback_auth = {"Authorization": "Bearer callback-test"}
    def drain():
        while queued:
            route, body = queued.pop(0)
            assert client.post(route, json=body, headers=worker_auth).status_code in (200, 202)
    alert = {"secret": "tv-test", "alert_id": "round-trip-alert", "ticker": "NASDAQ:AAPL", "signal": "green",
             "price": 200.5, "interval": "60", "time": datetime.now(timezone.utc).isoformat()}
    accepted = client.post("/api/v1/tv-alert-x7k9m2p", json=alert)
    assert accepted.status_code == 202
    drain()
    job_id = accepted.json["job_id"]
    assert len(analysis_requests) == 1 and analysis_requests[0]["job_id"] == job_id
    assert "secret" not in analysis_requests[0]
    completed = analysis_reply(job_id)
    assert client.post(grok_bot.CALLBACK_PATH, json=completed, headers=callback_auth).status_code == 200
    assert deliver(client, command_payload("/latest AAPL")).status_code == 200
    drain()
    assert len(sent) == 1 and sent[0]["to"] == "15555550123"
    report_url = sent[0]["text"]["body"].split("Report: ", 1)[1]
    parsed = urlsplit(report_url)
    assert client.get(parsed.path + "?" + parsed.query).status_code == 200
    assert client.get(parsed.path + "?" + parsed.query + "x").status_code == 403
    message = command_payload("Why this recommendation?")
    message["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.followup"
    assert deliver(client, message).status_code == 200
    drain()
    assert len(chat_requests) == 1 and len(sent) == 1
    assert chat_requests[0]["job_id"] == job_id
    chat_reply = {"type": "conversation_reply", "status": "completed", "message": "Here is why that stored recommendation was made.",
                  "conversation_id": chat_requests[0]["conversation_id"], "message_id": chat_requests[0]["message_id"]}
    assert client.post(grok_bot.CALLBACK_PATH, json=chat_reply, headers=callback_auth).status_code == 200
    drain()
    assert len(sent) == 2 and sent[1]["context"]["message_id"] == "wamid.followup"
    assert client.post(grok_bot.CALLBACK_PATH, json=chat_reply, headers=callback_auth).json["duplicate"] is True
    drain()
    assert len(sent) == 2 and len(analysis_requests) == len(chat_requests) == 1


def test_documented_callback_schema_matches_runtime():
    path = Path(main.__file__).parent / "docs" / "grok_bot_callback.schema.json"
    assert json.loads(path.read_text()) == grok_bot.CALLBACK_BODY.json_schema()
