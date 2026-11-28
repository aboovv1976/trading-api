"""Asynchronous chat bot transport and callback correlation; no model SDK."""

import hashlib
import json
import os

import httpx
from flask import jsonify

TTL = 7 * 86400
COMMIT = """
local old = redis.call('get', KEYS[1])
if old then
    if old == ARGV[1] then return 2 end
    return -1
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[4])
redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[4])
redis.call('set', KEYS[3], ARGV[3], 'EX', ARGV[4])
return 1
"""


def conversation_id(phone_id, sender):
    return "wa-" + hashlib.sha256((phone_id + ":" + sender).encode()).hexdigest()


def conversation_key(identifier):
    return "wa:conversation:" + identifier


def pending_key(identifier, message_id):
    return "wa:chat_request:" + hashlib.sha256((identifier + ":" + message_id).encode()).hexdigest()


def remember_context(store, phone_id, sender, jobs):
    key = conversation_key(conversation_id(phone_id, sender)) + ":context_job_id"
    if len(set(jobs)) == 1:
        store.set(key, jobs[0], ex=TTL)
    else:
        store.delete(key)


def dispatch(store, job_id, job, reset=False):
    if not all(os.environ.get(name) for name in ("GROK_CHAT_WEBHOOK_URL", "GROK_CHAT_WEBHOOK_KEY", "GROK_CALLBACK_TOKEN")):
        raise ValueError("Chat bot webhook not configured")
    url = httpx.URL(os.environ["GROK_CHAT_WEBHOOK_URL"])
    if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
        raise ValueError("Chat bot webhook must use HTTPS without user credentials")
    message = job["message"]
    identifier = conversation_id(job["phone_id"], message["from"])
    base = conversation_key(identifier)
    if not reset and store.exists(base + ":reset_pending"):
        raise RuntimeError("Waiting for conversation reset callback")
    key = pending_key(identifier, message["id"])
    pending = store.get(key)
    if pending:
        pending = json.loads(pending)
    else:
        # Serialize reset generations so late callbacks from before a reset
        # cannot send obsolete replies. The bot owns the actual history files.
        with store.lock(base + ":reset_lock", timeout=10, blocking_timeout=2):
            if not reset and store.exists(base + ":reset_pending"):
                raise RuntimeError("Waiting for conversation reset callback")
            existing = store.get(key)
            if existing:
                pending = json.loads(existing)
            else:
                store.set(base + ":generation", 0, nx=True, ex=TTL)
                generation = store.incr(base + ":generation") if reset else int(store.get(base + ":generation"))
                store.expire(base + ":generation", TTL)
                if reset:
                    store.delete(base + ":context_job_id")
                    store.set(base + ":reset_pending", key, ex=TTL)
                body = {"type": "reset" if reset else "message", "conversation_id": identifier, "message_id": message["id"]}
                if not reset:
                    body["user_message"] = message["text"]["body"]
                    context_job = store.get(base + ":context_job_id")
                    if context_job:
                        body["job_id"] = context_job
                pending = {"command_job_id": job_id, "generation": generation, "body": body}
                with store.pipeline(transaction=True) as transaction:
                    transaction.set(key, json.dumps(pending), ex=TTL)
                    transaction.set("wa:command:" + job_id + ":chat", json.dumps({"conversation_id": identifier, "generation": generation}), ex=TTL)
                    transaction.execute()
    # A reset that superseded this pending request cancels it, even on retry.
    if not current(store, job_id):
        store.set("wa:command:" + job_id + ":done", "superseded", ex=TTL)
        return
    if store.exists(key + ":sent") or store.exists(key + ":callback_hash"):
        return
    with httpx.Client(timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as client:
        response = client.post(str(url), headers={"Authorization": "Bearer " + os.environ["GROK_CHAT_WEBHOOK_KEY"]}, json=pending["body"])
        response.raise_for_status()
    store.set(key + ":sent", "accepted", ex=TTL)


def current(store, job_id):
    state = store.get("wa:command:" + job_id + ":chat")
    if not state:
        return True
    state = json.loads(state)
    return state["generation"] == int(store.get(conversation_key(state["conversation_id"]) + ":generation") or 0)


def receive(store, original):
    # Imports are local to keep transport independent of the command module.
    from whatsapp_commands import chunks, publish_job

    identifier, message_id = original.get("conversation_id"), original.get("message_id")
    if not isinstance(identifier, str) or not isinstance(message_id, str) or not identifier or not message_id:
        return jsonify(error="Chat callback requires conversation_id and message_id"), 400
    key = pending_key(identifier, message_id)
    raw = store.get(key)
    if raw is None:
        return jsonify(error="Unknown or expired conversation message"), 404
    pending = json.loads(raw)
    job_id = pending["command_job_id"]
    command_key = "wa:command:" + job_id
    status, reason = "completed", None
    reset = pending["body"]["type"] == "reset"
    if original.get("status") == "rejected" and isinstance(original.get("reason"), str) and original["reason"].strip():
        status = "rejected"
        texts = ["The chat bot could not process your message: " + original["reason"][:2000]]
    elif reset and original.get("type") == "reset_done" and original.get("status", "completed") == "completed":
        texts = ["Your conversation history has been reset. Send a message to start again."]
    elif not reset and original.get("type") == "conversation_reply" and original.get("status") == "completed" and isinstance(original.get("message"), str) and original["message"].strip() and len(original["message"]) <= 12000:
        texts = chunks(original["message"])
    else:
        status = "error"
        reason = "Unexpected chat bot response format."
        texts = ["The chat bot returned a response the app could not use. Please send a new message."]
    fingerprint = hashlib.sha256(json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    record = {"status": status, "bot_response": original}
    if reason:
        record["reason"] = reason
    elif status == "rejected":
        record["reason"] = original["reason"][:2000]
    saved = store.eval(COMMIT, 3, key + ":callback_hash", command_key + ":replies", command_key + ":chat_result",
                       fingerprint, json.dumps(texts), json.dumps(record), TTL)
    if saved == -1:
        return jsonify(error="Different reply already recorded for this message"), 409
    if reset:
        store.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
                   1, conversation_key(identifier) + ":reset_pending", key)
    if not store.exists(command_key + ":done"):
        # Re-publish on duplicate callbacks too: a previous queue publish may
        # have failed after committing the reply. Sending is deduplicated later.
        publish_job(job_id)
    return jsonify(status=status, conversation_id=identifier, message_id=message_id, duplicate=saved == 2), 200
