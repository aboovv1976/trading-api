import json

import httpx
import pytest

import grok_bot
import whatsapp_chat as chat
from test_whatsapp import inbox
from test_whatsapp_commands import sending, command_payload, deliver, graph_requests, seed


def bot_requests(sending):
    return [request for request in sending[2] if request.url.host == "chat-bot.test"]


def run_last_worker(sending):
    queue = [request for request in sending[2] if request.url.host == "qstash.test"][-1]
    return sending[0].post("/api/v1/whatsapp-reply-71f295b68cd903ea", json=json.loads(queue.content),
                           headers={"Authorization": "Bearer worker-test"})


def send_turn(sending, text, message_id="wamid.first", sender="15555550123"):
    payload = command_payload(text)
    payload["entry"][0]["changes"][0]["value"]["messages"][0].update(id=message_id, **{"from": sender})
    assert deliver(sending[0], payload).status_code == 200
    return run_last_worker(sending)


def reply_body(sending, **changes):
    original = json.loads(bot_requests(sending)[-1].content)
    return {"type": "conversation_reply", "status": "completed", "conversation_id": original["conversation_id"],
            "message_id": original["message_id"], "message": "Here is the answer.",
            "answered_at": "2026-11-27T15:00:00Z", **changes}


def callback(sending, body=None, token="callback-test"):
    return sending[0].post(grok_bot.CALLBACK_PATH, json=body if body is not None else reply_body(sending),
                           headers={"Authorization": "Bearer " + token})


def test_plain_text_dispatches_to_chat_bot_and_waits_for_callback(sending):
    response = send_turn(sending, "Is NVDA still a buy?")
    assert response.status_code == 200 and response.json["status"] == "awaiting_callback"
    assert len(bot_requests(sending)) == 1 and not graph_requests(sending)
    request = bot_requests(sending)[0]
    body = json.loads(request.content)
    assert body == {"type": "message", "conversation_id": chat.conversation_id("123456789", "15555550123"),
                    "message_id": "wamid.first", "user_message": "Is NVDA still a buy?"}
    assert request.headers["Authorization"] == "Bearer chat-key-test"
    assert "15555550123" not in request.content.decode()
    assert "instructions" not in body and "history" not in body


def test_reply_callback_queues_whatsapp_delivery_to_original_sender(sending):
    send_turn(sending, "Explain NVDA")
    body = reply_body(sending, to="19999999999", job_id="a" * 64)
    assert callback(sending, body).status_code == 200
    assert not graph_requests(sending)
    assert run_last_worker(sending).status_code == 200
    sent = json.loads(graph_requests(sending)[0].content)
    assert sent["to"] == "15555550123"
    assert sent["context"]["message_id"] == "wamid.first"
    assert sent["text"]["body"] == "Here is the answer."
    assert len(bot_requests(sending)) == 1
    with sending[1]() as store:
        assert not list(store.scan_iter(match="tv:*:result"))


def test_followups_reuse_conversation_but_have_distinct_message_ids(sending):
    send_turn(sending, "Explain NVDA", "one")
    callback(sending)
    run_last_worker(sending)
    send_turn(sending, "Why that stop?", "two")
    messages = [json.loads(request.content) for request in bot_requests(sending)]
    assert messages[0]["conversation_id"] == messages[1]["conversation_id"]
    assert messages[0]["message_id"] != messages[1]["message_id"]
    assert messages[1]["user_message"] == "Why that stop?"
    assert "Explain NVDA" not in bot_requests(sending)[1].content.decode()


def test_different_users_and_business_numbers_have_distinct_ids(sending, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "15555550123,15555550124")
    send_turn(sending, "First person's question", "one")
    send_turn(sending, "Second person's question", "two", "15555550124")
    first, second = [json.loads(request.content) for request in bot_requests(sending)]
    assert first["conversation_id"] != second["conversation_id"]
    assert first["user_message"] not in bot_requests(sending)[1].content.decode()
    assert chat.conversation_id("123456789", "15555550123") != chat.conversation_id("987654321", "15555550123")


def test_duplicate_dispatch_and_callback_do_not_repeat_bot_or_whatsapp_send(sending):
    send_turn(sending, "Explain NVDA", "same")
    send_turn(sending, "Explain NVDA", "same")
    assert len(bot_requests(sending)) == 1
    body = reply_body(sending)
    assert callback(sending, body).status_code == 200
    assert callback(sending, body).json["duplicate"] is True
    run_last_worker(sending)
    assert callback(sending, body).json["duplicate"] is True
    run_last_worker(sending)
    assert len(graph_requests(sending)) == 1 and len(bot_requests(sending)) == 1


@pytest.mark.parametrize("changes", [{"conversation_id": "other-conversation"}, {"message_id": "other-message"}])
def test_callback_cannot_cross_message_or_user_boundaries(sending, changes):
    send_turn(sending, "Explain NVDA")
    assert callback(sending, reply_body(sending, **changes)).status_code == 404
    assert not graph_requests(sending)


def test_callback_requires_shared_callback_token(sending):
    send_turn(sending, "Explain NVDA")
    assert callback(sending, token="chat-key-test").status_code == 401
    assert callback(sending, token="access-test").status_code == 401


def test_reset_uses_remote_reset_protocol(sending):
    send_turn(sending, "/reset", "reset")
    body = json.loads(bot_requests(sending)[0].content)
    assert body["type"] == "reset" and "user_message" not in body
    assert callback(sending, reply_body(sending, type="reset_done")).status_code == 200
    run_last_worker(sending)
    assert "history has been reset" in json.loads(graph_requests(sending)[0].content)["text"]["body"]


def test_reset_suppresses_late_pre_reset_reply(sending):
    send_turn(sending, "Old question", "old")
    old_reply = reply_body(sending)
    send_turn(sending, "/reset", "reset")
    reset_reply = reply_body(sending, type="reset_done")
    assert callback(sending, old_reply).status_code == 200
    run_last_worker(sending)
    assert not graph_requests(sending)
    assert callback(sending, reset_reply).status_code == 200
    run_last_worker(sending)
    assert len(graph_requests(sending)) == 1
    assert json.loads(graph_requests(sending)[0].content)["context"]["message_id"] == "reset"


def test_callback_rechecks_allowlist_before_delivery(sending, monkeypatch):
    send_turn(sending, "Explain NVDA")
    monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "")
    callback(sending)
    run_last_worker(sending)
    assert not graph_requests(sending)


def test_plain_text_can_reference_latest_single_ticker_lookup(sending):
    seed(sending[1], "NASDAQ:NVDA")
    send_turn(sending, "/latest NVDA", "lookup")
    assert not bot_requests(sending)
    send_turn(sending, "Why this recommendation?", "followup")
    assert len(json.loads(bot_requests(sending)[0].content)["job_id"]) == 64


def test_multiple_ticker_lookup_does_not_guess_analysis_job(sending):
    seed(sending[1], "NASDAQ:NVDA")
    seed(sending[1], "NASDAQ:AAPL")
    send_turn(sending, "/latest NVDA, AAPL", "lookup")
    send_turn(sending, "Why?", "followup")
    assert "job_id" not in json.loads(bot_requests(sending)[0].content)


def test_chat_bot_rejection_is_recorded_and_replied(sending):
    send_turn(sending, "Explain NVDA")
    body = reply_body(sending, status="rejected", reason="Unsupported question")
    assert callback(sending, body).json["status"] == "rejected"
    run_last_worker(sending)
    assert "Unsupported question" in json.loads(graph_requests(sending)[0].content)["text"]["body"]


def test_unknown_chat_reply_is_marked_error_not_recommendation(sending):
    send_turn(sending, "Explain NVDA")
    body = reply_body(sending, type="unexpected", message="")
    assert callback(sending, body).json["status"] == "error"
    run_last_worker(sending)
    assert "could not use" in json.loads(graph_requests(sending)[0].content)["text"]["body"]


def test_conflicting_chat_callback_cannot_replace_first(sending):
    send_turn(sending, "Explain NVDA")
    assert callback(sending).status_code == 200
    assert callback(sending, reply_body(sending, message="Different reply")).status_code == 409


def test_long_reply_is_split_without_another_bot_call(sending):
    send_turn(sending, "Explain NVDA")
    assert callback(sending, reply_body(sending, message="x" * 8000)).status_code == 200
    run_last_worker(sending)
    parts = [json.loads(request.content)["text"]["body"] for request in graph_requests(sending)]
    assert "".join(parts) == "x" * 8000
    assert all(len(part) <= 3500 for part in parts)
    assert len(bot_requests(sending)) == 1


def test_callback_queue_publish_failure_can_retry_without_repeating_reply(sending, monkeypatch):
    import whatsapp_commands
    from unittest.mock import MagicMock
    send_turn(sending, "Explain NVDA")
    body = reply_body(sending)
    publish = whatsapp_commands.publish_job
    monkeypatch.setattr(whatsapp_commands, "publish_job", MagicMock(side_effect=RuntimeError("queue unavailable")))
    assert callback(sending, body).status_code == 503
    monkeypatch.setattr(whatsapp_commands, "publish_job", publish)
    assert callback(sending, body).json["duplicate"] is True
    run_last_worker(sending)
    assert len(graph_requests(sending)) == 1 and len(bot_requests(sending)) == 1


def test_new_chat_waits_for_reset_acknowledgement(sending):
    send_turn(sending, "/reset", "reset")
    reset_reply = reply_body(sending, type="reset_done")
    assert send_turn(sending, "New question", "new").status_code == 503
    assert len(bot_requests(sending)) == 1
    callback(sending, reset_reply)
    run_last_worker(sending)
    assert send_turn(sending, "New question", "new").json["status"] == "awaiting_callback"
    assert len(bot_requests(sending)) == 2


def test_ambiguous_dispatch_timeout_reuses_same_message_and_reset_generation(sending, monkeypatch):
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__enter__.return_value = client
    request = httpx.Request("POST", "https://chat-bot.test/webhook")
    client.post.side_effect = [httpx.ReadTimeout("private transport failure"), httpx.Response(200, request=request)]
    # Publish the command before replacing the HTTP client used by the worker.
    payload = command_payload("/reset")
    assert deliver(sending[0], payload).status_code == 200
    monkeypatch.setattr(chat.httpx, "Client", lambda **kwargs: client)
    assert run_last_worker(sending).status_code == 503
    assert run_last_worker(sending).json["status"] == "awaiting_callback"
    bodies = [call.kwargs["json"] for call in client.post.call_args_list]
    assert bodies[0] == bodies[1]
    with sending[1]() as store:
        generation = store.get(chat.conversation_key(bodies[0]["conversation_id"]) + ":generation")
        assert generation == "1"


def test_late_callback_does_not_send_after_whatsapp_window(sending):
    import time
    send_turn(sending, "Explain NVDA")
    with sending[1]() as store:
        keys = [key for key in store.scan_iter(match="wa:command:*") if key.count(":") == 2]
        job = json.loads(store.get(keys[0]))
        job["message"]["timestamp"] = str(int(time.time()) - 86401)
        store.set(keys[0], json.dumps(job), ex=604800)
    callback(sending)
    run_last_worker(sending)
    assert not graph_requests(sending)
