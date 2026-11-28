"""Meta verification handshake and authenticated WhatsApp webhook inbox."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request

from whatsapp_commands import WORKER_PATH, allowed, enqueue, process

WHATSAPP_PATH = "/api/v1/whatsapp-webhook-9119d4cb5e7c7192"
EVENT_TTL = 7 * 24 * 60 * 60
MAX_PAYLOAD_BYTES = 3 * 1024 * 1024


def permitted_payload(payload):
    entries = []
    for entry in payload["entry"]:
        changes = []
        for change in entry["changes"]:
            value = change.get("value")
            if change.get("field") != "messages" or not isinstance(value, dict):
                continue
            filtered = dict(value)
            for field, sender_field in (("messages", "from"), ("statuses", "recipient_id"), ("contacts", "wa_id")):
                items = value.get(field, [])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise ValueError("Invalid WhatsApp message array")
                if field in value:
                    filtered[field] = [item for item in items if allowed(item.get(sender_field))]
                    if field == "messages" and len(filtered[field]) != len(items):
                        current_app.logger.info("WhatsApp messages ignored reason=sender_not_allowed count=%s", len(items) - len(filtered[field]))
            if filtered.get("messages") or filtered.get("statuses"):
                changes.append({**change, "value": filtered})
        if changes:
            entries.append({**entry, "changes": changes})
    return {**payload, "entry": entries} if entries else None


def create_blueprint(redis_factory):
    blueprint = Blueprint("whatsapp", __name__)

    @blueprint.before_request
    def payload_limit():
        # Meta allows batches up to 3 MB. Keep the smaller global TradingView
        # limit unchanged; Flask 3.1 supports this per-request override.
        request.max_content_length = MAX_PAYLOAD_BYTES

    @blueprint.get(WHATSAPP_PATH)
    def verify():
        expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
        if not expected:
            return jsonify(error="WhatsApp verification not configured"), 503
        supplied = request.args.get("hub.verify_token", "")
        if request.args.get("hub.mode") != "subscribe" or not hmac.compare_digest(
            supplied.encode(), expected.encode(),
        ):
            return jsonify(error="Verification failed"), 403
        challenge = request.args.get("hub.challenge")
        if challenge is None or not challenge.isascii() or not challenge.isdecimal() or len(challenge) > 128:
            return jsonify(error="Invalid verification challenge"), 400
        # Meta expects the challenge itself, without JSON quoting or a wrapper.
        return Response(challenge, mimetype="text/plain")

    @blueprint.post(WHATSAPP_PATH)
    def receive():
        secret = os.environ.get("WHATSAPP_APP_SECRET")
        if not secret or not os.environ.get("REDIS_URL"):
            return jsonify(error="WhatsApp receiving not configured"), 503
        raw = request.get_data(cache=False)
        supplied = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return jsonify(error="Invalid webhook signature"), 401
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
                raise ValueError("Not a WhatsApp event")
            entries = payload.get("entry")
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise ValueError("Invalid entries")
            for entry in entries:
                changes = entry.get("changes")
                if not isinstance(changes, list) or any(not isinstance(change, dict) for change in changes):
                    raise ValueError("Invalid changes")
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            event_id = hashlib.sha256(canonical.encode()).hexdigest()
            payload = permitted_payload(payload)
        except (ValueError, UnicodeError, RecursionError):
            return jsonify(error="Invalid WhatsApp event"), 400
        if payload is None:
            current_app.logger.info("WhatsApp webhook ignored event_id=%s reason=no_allowed_messages_or_statuses", event_id)
            return jsonify(status="ignored"), 200
        record = json.dumps({
            "event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }, ensure_ascii=False, allow_nan=False)
        try:
            with redis_factory() as store:
                stored = store.set("wa:webhook:" + event_id, record, nx=True, ex=EVENT_TTL)
                for entry in payload["entry"]:
                    for change in entry["changes"]:
                        value = change["value"]
                        for message in value.get("messages", []):
                            enqueue(store, message, (value.get("metadata") or {}).get("phone_number_id"))
        except Exception as exc:
            # Do not acknowledge a delivery that could not be persisted. Meta
            # can retry; logs contain no message content, numbers or secrets.
            current_app.logger.error("WhatsApp event storage failed (%s)", type(exc).__name__)
            return jsonify(error="Webhook storage unavailable"), 503
        current_app.logger.info("WhatsApp webhook accepted event_id=%s duplicate=%s", event_id, not bool(stored))
        return jsonify(status="received", event_id=event_id, duplicate=not bool(stored)), 200

    @blueprint.post(WORKER_PATH)
    def reply_worker():
        expected = os.environ.get("WORKER_TOKEN")
        supplied = request.headers.get("Authorization", "")
        if not expected or not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
            return jsonify(error="Unauthorized"), 401
        body = request.get_json(silent=True)
        job_id = body.get("job_id") if isinstance(body, dict) else None
        if not isinstance(job_id, str) or len(job_id) != 64 or any(c not in "0123456789abcdef" for c in job_id):
            return jsonify(error="Invalid job ID"), 400
        try:
            with redis_factory() as store:
                status = process(store, job_id)
            current_app.logger.info("WhatsApp worker job_id=%s status=%s", job_id, status or "completed")
            return jsonify(status=status or "completed"), 200
        except Exception as exc:
            current_app.logger.error("WhatsApp command failed job_id=%s (%s)", job_id, type(exc).__name__)
            return jsonify(error="WhatsApp command unavailable"), 503

    return blueprint
