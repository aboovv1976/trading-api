"""Meta verification handshake and authenticated WhatsApp webhook inbox."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request

WHATSAPP_PATH = "/api/v1/whatsapp-webhook-9119d4cb5e7c7192"
EVENT_TTL = 7 * 24 * 60 * 60
MAX_PAYLOAD_BYTES = 3 * 1024 * 1024


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
        except (ValueError, UnicodeError, RecursionError):
            return jsonify(error="Invalid WhatsApp event"), 400
        record = json.dumps({
            "event_id": event_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }, ensure_ascii=False, allow_nan=False)
        try:
            with redis_factory() as store:
                stored = store.set("wa:webhook:" + event_id, record, nx=True, ex=EVENT_TTL)
        except Exception as exc:
            # Do not acknowledge a delivery that could not be persisted. Meta
            # can retry; logs contain no message content, numbers or secrets.
            current_app.logger.error("WhatsApp event storage failed (%s)", type(exc).__name__)
            return jsonify(error="Webhook storage unavailable"), 503
        current_app.logger.info("WhatsApp webhook accepted event_id=%s duplicate=%s", event_id, not bool(stored))
        return jsonify(status="received", event_id=event_id, duplicate=not bool(stored)), 200

    return blueprint
