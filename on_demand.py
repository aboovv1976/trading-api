"""Request missing recommendations and deliver their asynchronous results."""

import hashlib
import json

TTL = 7 * 86400


def request_analysis(store, command_id, job, ticker):
    from grok_bot import dispatch

    # A queue retry must reuse the same bot job, while a new user request may
    # legitimately request another analysis after an earlier failure/expiry.
    job_id = hashlib.sha256(f"on_demand:{command_id}:{ticker}".encode()).hexdigest()
    prefix = "tv:{" + job_id + "}:"
    payload = {"type": "analyze", "ticker": ticker, "interval": "1D"}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    store.set("wa:on_demand:" + job_id, json.dumps(job), nx=True, ex=TTL)
    with store.lock(prefix + "dispatch_lock", timeout=30, blocking_timeout=2):
        if not store.exists(prefix + "result"):
            dispatch(store, payload, job_id, fingerprint, source="on_demand")
    return job_id


def notify(store, record):
    # Recipient information comes only from the authenticated WhatsApp ingress,
    # never from fields supplied by the analysis bot.
    from whatsapp_commands import chunks, publish_job, result_text

    job_id = record["job_id"]
    raw = store.get("wa:on_demand:" + job_id)
    if not raw:
        return
    # A recovered result needs a new delivery even if the error was already sent.
    delivery_id = hashlib.sha256(f"analysis_reply:{job_id}:{record['status']}".encode()).hexdigest()
    key = "wa:command:" + delivery_id
    if store.exists(key + ":done"):
        return
    if record["status"] == "completed":
        text = result_text(record)
    else:
        text = f"{record['alert']['ticker']}: Analysis {record['status']}. {record['reason']}"
    # Write content before publishing. Callback retries can safely republish a
    # saved result when QStash was unavailable after the result was committed.
    with store.pipeline(transaction=True) as pipe:
        pipe.set(key, raw, nx=True, ex=TTL)
        pipe.set(key + ":replies", json.dumps(chunks(text)), nx=True, ex=TTL)
        pipe.execute()
    publish_job(delivery_id)
