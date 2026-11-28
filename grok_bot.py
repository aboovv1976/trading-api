"""External Grok bot dispatch and authenticated result callbacks."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Literal

import httpx
from flask import Blueprint, current_app, jsonify, request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from bot_payloads import RichBotCallback, presentation
from on_demand import notify
from recommendations import watch
from whatsapp_chat import receive as receive_chat_reply

CALLBACK_PATH = "/api/v1/grok-callback-73e312d8bab9bfda"
TTL = 7 * 86400
PENDING_TTL = 86400
PROVENANCE = "External bot sources are bot-reported; xAI response citation metadata is unavailable for matching."
COMMIT = """
if redis.call('exists', KEYS[1]) == 1 then
    if redis.call('get', KEYS[3]) == ARGV[2] then return 2 end
    return -1
end
if redis.call('get', KEYS[2]) ~= ARGV[3] then return 0 end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[4])
redis.call('set', KEYS[3], ARGV[2], 'EX', ARGV[4])
redis.call('del', KEYS[2])
return 1
"""


class BotRejection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["rejected"]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("A rejection reason is required")
        return value


CALLBACK_BODY = TypeAdapter(RichBotCallback | BotRejection)


def reject_nonfinite(value):
    raise ValueError("Nonfinite JSON number")


def dispatch(store, alert, job_id, fingerprint, *, source="tradingview"):
    payload = alert.model_dump(mode="json") if source == "tradingview" else alert
    if not all(os.environ.get(name) for name in ("GROK_WEBHOOK_URL", "GROK_WEBHOOK_KEY", "GROK_CALLBACK_TOKEN")):
        raise ValueError("Grok bot integration not configured")
    url = httpx.URL(os.environ["GROK_WEBHOOK_URL"])
    if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
        raise ValueError("GROK_WEBHOOK_URL must be HTTPS without user credentials")
    if source == "tradingview" and (datetime.now(timezone.utc) - alert.time).total_seconds() > PENDING_TTL:
        raise ValueError("Alert too old for external analysis")
    prefix = "tv:{" + job_id + "}:"
    pending_key = prefix + "bot_pending"
    pending = {"job_id": job_id, "fingerprint": fingerprint,
               "alert": payload, "source": source,
               "submitted_at": datetime.now(timezone.utc).isoformat()}
    store.set(pending_key, json.dumps(pending), nx=True, ex=PENDING_TTL)
    existing = store.get(pending_key)
    if not existing or json.loads(existing)["fingerprint"] != fingerprint:
        raise ValueError("Pending alert conflict")
    if store.exists(prefix + "bot_sent"):
        return
    # Never forward TV_WEBHOOK_SECRET or any callback credential. Configure
    # the callback URL/key separately in the bot's trusted automation settings.
    with httpx.Client(timeout=httpx.Timeout(10, connect=3), follow_redirects=False) as client:
        response = client.post(str(url), headers={"Authorization": "Bearer " + os.environ["GROK_WEBHOOK_KEY"]},
                               json={"job_id": job_id, **payload})
        response.raise_for_status()
    store.set(prefix + "bot_sent", "accepted", ex=PENDING_TTL)


def checked_analysis(analysis):
    now = datetime.now(timezone.utc)
    fresh = any(source.kind == "market_data" and source.as_of is not None
                and -300 <= (now - source.as_of).total_seconds() <= 7 * 86400
                for source in analysis.sources)
    if not fresh or not analysis.market_data_sufficient:
        return watch("External bot did not supply sufficient, timestamped market evidence. "
                     "Bot rationale: " + analysis.rationale[:1200], sources=analysis.sources,
                     limitations=[PROVENANCE] + analysis.limitations)
    analysis.limitations = list(dict.fromkeys([PROVENANCE] + analysis.limitations))[:20]
    return analysis


def create_blueprint(redis_factory):
    blueprint = Blueprint("grok_bot", __name__)

    @blueprint.post(CALLBACK_PATH)
    def receive_result():
        token = os.environ.get("GROK_CALLBACK_TOKEN")
        if not token or not os.environ.get("REDIS_URL"):
            return jsonify(error="Grok callback not configured"), 503
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return jsonify(error="Unauthorized"), 401
        # Validate known fields and retain the complete rich bot response.
        try:
            raw = request.get_data()
            original = json.loads(raw, parse_constant=reject_nonfinite)
        except (ValueError, UnicodeError, RecursionError):
            return jsonify(error="Cannot associate invalid JSON with a job"), 400
        if isinstance(original, dict) and (
            "conversation_id" in original or "message_id" in original
            or original.get("type") in ("conversation_reply", "reset_done")
        ):
            try:
                with redis_factory() as store:
                    return receive_chat_reply(store, original)
            except Exception as exc:
                current_app.logger.error("Chat callback unavailable (%s)", type(exc).__name__)
                return jsonify(error="Chat callback unavailable"), 503
        job_id = original.get("job_id") if isinstance(original, dict) else None
        if not isinstance(job_id, str) or len(job_id) != 64 or any(c not in "0123456789abcdef" for c in job_id):
            return jsonify(error="Cannot associate callback without a valid job_id"), 400
        validation_errors = []
        error_reason = None
        try:
            body = CALLBACK_BODY.validate_json(raw)
        except ValidationError as exc:
            body = None
            error_reason = "Bot response did not match the expected completed or rejected format."
            # Describe field failures without copying values into diagnostics.
            validation_errors = [{"field": ".".join(map(str, issue["loc"]))[:200],
                                  "type": issue["type"]} for issue in exc.errors()[:20]]
        status = "error" if body is None else "rejected" if isinstance(body, BotRejection) else "completed"
        prefix = "tv:{" + job_id + "}:"
        if not isinstance(body, BotRejection):
            hash_input = json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        else:
            hash_input = body.model_dump_json()
        callback_hash = hashlib.sha256(hash_input.encode("utf-8", errors="surrogatepass")).hexdigest()
        try:
            with redis_factory() as store:
                previous = store.get(prefix + "result")
                if previous:
                    if store.get(prefix + "bot_callback_hash") == callback_hash:
                        previous_record = json.loads(previous)
                        notify(store, previous_record)
                        ack = {"status": previous_record.get("status", "completed"), "job_id": job_id, "duplicate": True}
                        if ack["status"] == "error":
                            ack["reason"] = previous_record["reason"]
                        return jsonify(ack), 200
                    return jsonify(error="Result already exists with different content"), 409
                pending_raw = store.get(prefix + "bot_pending")
                if pending_raw is None:
                    return jsonify(error="Unknown or expired pending job"), 404
                pending = json.loads(pending_raw)
                if isinstance(body, RichBotCallback):
                    expected_ticker = pending["alert"]["ticker"].upper()
                    supplied_ticker = body.ticker.upper()
                    same_symbol = supplied_ticker.split(":")[-1] == expected_ticker.split(":")[-1]
                    both_qualified = ":" in supplied_ticker and ":" in expected_ticker
                    if supplied_ticker != expected_ticker and (both_qualified or not same_symbol):
                        status = "error"
                        error_reason = "Bot ticker does not match the pending alert."
                        analysis = None
                    else:
                        try:
                            analysis = checked_analysis(presentation(body))
                        except Exception as exc:
                            status = "error"
                            error_reason = "The app could not interpret the bot response."
                            validation_errors = [{"field": "recommendation", "type": type(exc).__name__}]
                            analysis = None
                else:
                    analysis = None
                result = {"job_id": job_id, "fingerprint": pending["fingerprint"], "alert": pending["alert"],
                          "status": status, "source": pending.get("source", "tradingview"),
                          "analysis": analysis.model_dump(mode="json") if analysis is not None else None,
                          "completed_at": datetime.now(timezone.utc).isoformat(),
                          "analysis_backend": "grok_bot", "source_provenance": "bot_reported"}
                if status == "rejected":
                    result["reason"] = body.reason
                if status == "error":
                    result["reason"] = error_reason
                    result["error_type"] = "unexpected_bot_response"
                    if validation_errors:
                        result["validation_errors"] = validation_errors
                if not isinstance(body, BotRejection):
                    result["bot_response"] = original
                saved = store.eval(COMMIT, 3, prefix + "result", prefix + "bot_pending", prefix + "bot_callback_hash",
                                   json.dumps(result), callback_hash, pending_raw, TTL)
                if saved == -1:
                    return jsonify(error="Result already exists with different content"), 409
                if saved == 0:
                    return jsonify(error="Unknown or expired pending job"), 404
                notify(store, result)
            current_app.logger.info("Grok callback stored job_id=%s", job_id)
            ack = {"status": status, "job_id": job_id, "duplicate": saved == 2}
            if status == "error":
                ack["reason"] = error_reason
            return jsonify(ack), 200
        except Exception as exc:
            current_app.logger.error("Grok callback failed job_id=%s (%s)", job_id, type(exc).__name__)
            return jsonify(error="Result storage unavailable"), 503

    return blueprint
