import json

import httpx
import pytest

import grok_bot
import whatsapp_commands as commands
from test_grok_bot import callback, post_result
from test_whatsapp import deliver, inbox
from test_whatsapp_commands import command_payload, graph_requests, run_worker, seed, sending
from whatsapp_chat import conversation_key, conversation_id


def requests(sending):
    return [r for r in sending[2] if r.url.host == "analysis-bot.test"]


def start(sending, text="/latest AAPL"):
    assert deliver(sending[0], command_payload(text)).status_code == 200
    assert run_worker(sending).status_code == 200
    return json.loads(requests(sending)[0].content)["job_id"]


def run_notification(sending):
    queue = [r for r in sending[2] if r.url.host == "qstash.test"][-1]
    return sending[0].post(commands.WORKER_PATH, json=json.loads(queue.content),
                           headers={"Authorization": "Bearer worker-test"})


def test_missing_ticker_callback_report_and_context(sending):
    job = start(sending)
    body = json.loads(requests(sending)[0].content)
    assert body == {"type": "analyze", "job_id": job, "ticker": "AAPL", "interval": "1D"}
    assert requests(sending)[0].headers["Authorization"] == "Bearer analysis-key-test"
    with sending[1]() as store:
        context = conversation_key(conversation_id("123456789", "15555550123"))
        assert store.get(context + ":context_job_id") == job
    result = callback(job)
    assert post_result(sending[0], result).status_code == 200
    with sending[1]() as store:
        saved = json.loads(store.get("tv:{" + job + "}:result"))
        assert saved["source"] == "on_demand"
        assert saved["alert"] == {"type": "analyze", "ticker": "AAPL", "interval": "1D"}
        assert saved["bot_response"] == result
    assert run_notification(sending).status_code == 200
    text = json.loads(graph_requests(sending)[-1].content)["text"]["body"]
    assert "AAPL Buy" in text and "Report: https://" in text
    assert post_result(sending[0], result).json["duplicate"] is True
    assert run_notification(sending).status_code == 200
    assert len(graph_requests(sending)) == 2  # acknowledgment and finished report
    with sending[1]() as store:
        assert commands.recommendations(store, ["*"]) == ["Available tickers with stored recommendations:\nAAPL"]


def test_worker_retry_does_not_dispatch_twice(sending):
    start(sending)
    assert run_worker(sending).status_code == 200
    assert len(requests(sending)) == 1


def test_mixed_lookup_only_analyzes_missing_ticker(sending):
    seed(sending[1], "NASDAQ:NVDA")
    start(sending, "/latest NVDA, AAPL")
    assert len(requests(sending)) == 1
    assert json.loads(requests(sending)[0].content)["ticker"] == "AAPL"


def test_all_and_ambiguous_tickers_do_not_dispatch(sending):
    seed(sending[1], "NASDAQ:ABC")
    seed(sending[1], "NYSE:ABC")
    deliver(sending[0], command_payload("/latest ABC"))
    assert run_worker(sending).status_code == 200
    assert not requests(sending)
    with sending[1]() as store:
        missing = []
        commands.recommendations(store, ["*"], missing=missing)
        assert not missing


@pytest.mark.parametrize("body", ["rejected", "unexpected"])
def test_analysis_failures_are_stored_and_notified(sending, body):
    job = start(sending)
    packet = {"job_id": job, "status": "rejected", "reason": "Research unavailable"} if body == "rejected" else {"job_id": job, "unexpected": True}
    assert post_result(sending[0], packet).status_code == 200
    assert run_notification(sending).status_code == 200
    text = json.loads(graph_requests(sending)[-1].content)["text"]["body"]
    assert "Analysis rejected" in text if body == "rejected" else "Analysis error" in text
    with sending[1]() as store:
        assert json.loads(store.get("tv:{" + job + "}:result"))["source"] == "on_demand"


def test_callback_retries_notification_after_publish_failure(sending, monkeypatch):
    job = start(sending)
    original = commands.publish_job
    def fail(_):
        raise httpx.ConnectError("queue unavailable")
    monkeypatch.setattr(commands, "publish_job", fail)
    packet = callback(job)
    assert post_result(sending[0], packet).status_code == 503
    with sending[1]() as store:
        assert store.exists("tv:{" + job + "}:result")
    monkeypatch.setattr(commands, "publish_job", original)
    response = post_result(sending[0], packet)
    assert response.status_code == 200 and response.json["duplicate"]
    assert run_notification(sending).status_code == 200


def test_completed_on_demand_is_reused_by_new_command(sending):
    job = start(sending)
    assert post_result(sending[0], callback(job)).status_code == 200
    payload = command_payload("/latest AAPL")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "wamid.second"
    assert deliver(sending[0], payload).status_code == 200
    assert run_notification(sending).status_code == 200
    assert len(requests(sending)) == 1
    assert "AAPL Buy" in json.loads(graph_requests(sending)[-1].content)["text"]["body"]


def test_each_missing_ticker_delivers_own_result_without_implicit_context(sending):
    start(sending, "/latest AAPL, NVDA")
    bodies = [json.loads(r.content) for r in requests(sending)]
    assert {b["ticker"] for b in bodies} == {"AAPL", "NVDA"}
    with sending[1]() as store:
        context = conversation_key(conversation_id("123456789", "15555550123"))
        assert not store.exists(context + ":context_job_id")
    for body in bodies:
        packet = callback(body["job_id"])
        packet["ticker"] = body["ticker"]
        assert post_result(sending[0], packet).status_code == 200
        assert run_notification(sending).status_code == 200
    assert len(graph_requests(sending)) == 4  # two acknowledgments, two completed analyses


def test_callback_reply_rechecks_allowlist(sending, monkeypatch):
    job = start(sending)
    assert post_result(sending[0], callback(job)).status_code == 200
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "15555559999")
    assert run_notification(sending).status_code == 200
    assert len(graph_requests(sending)) == 1  # original acknowledgment only


def test_dispatch_retry_reuses_same_job(sending, monkeypatch):
    original = grok_bot.dispatch
    def fail(*args, **kwargs):
        raise httpx.ConnectError("bot unavailable")
    monkeypatch.setattr(grok_bot, "dispatch", fail)
    assert deliver(sending[0], command_payload("/latest AAPL")).status_code == 200
    assert run_worker(sending).status_code == 503
    monkeypatch.setattr(grok_bot, "dispatch", original)
    assert run_worker(sending).status_code == 200
    assert len(requests(sending)) == 1
