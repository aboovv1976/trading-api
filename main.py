"""TradingView ingress and a queue-driven analysis worker for Vercel."""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from flask import Flask, Response, jsonify, render_template, request
from pydantic import ValidationError
from redis import Redis

from grok_bot import create_blueprint as create_grok_blueprint
from grok_bot import dispatch as dispatch_grok_bot
from models import Alert
from reports import REPORTS_PATH, valid_job_id, valid_report_signature, with_report
from whatsapp import create_blueprint as create_whatsapp_blueprint

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
LOCK_TTL = 360  # Longer than the configured 300-second Vercel invocation.
QSTASH_DELIVERY_TIMEOUT = 330  # Allows for invocation startup and shutdown.
RESULTS_PATH = "/api/v1/results-8dc2a850b73dc31d"
# A timed-out dispatch worker cannot release another delivery's lock.
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def configured(*names):
    return all(os.environ.get(name) for name in names)


def authorized(value, env_name):
    expected = os.environ.get(env_name)
    return bool(
        expected and isinstance(value, str)
        and hmac.compare_digest(value.encode(), expected.encode())
    )


def bearer_authorized(env_name):
    header = request.headers.get("Authorization", "")
    return header.startswith("Bearer ") and authorized(header[7:], env_name)


def redis_client():
    # No credentials or network connection are required at module import time.
    return Redis.from_url(
        os.environ["REDIS_URL"], decode_responses=True,
        socket_connect_timeout=2, socket_timeout=2,
    )


app.register_blueprint(create_whatsapp_blueprint(lambda: redis_client()))
app.register_blueprint(create_grok_blueprint(lambda: redis_client()))


def canonical(alert):
    return json.dumps(alert.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def fingerprint(alert):
    return hashlib.sha256(canonical(alert).encode()).hexdigest()


def job_id_for(alert):
    identity = alert.alert_id if alert.alert_id else canonical(alert)
    return hashlib.sha256(identity.encode()).hexdigest()


def publish(alert, job_id):
    # Explicit destination prevents Host-header injection into queued jobs.
    destination = os.environ["WORKER_URL"]
    url = httpx.URL(destination)
    if url.scheme != "https" or not url.host or url.query or url.fragment or url.userinfo:
        raise ValueError("WORKER_URL must be a public HTTPS worker URL")
    queue_url = httpx.URL(os.environ["QSTASH_URL"])
    if queue_url.scheme != "https" or not queue_url.host or queue_url.query or queue_url.fragment or queue_url.userinfo:
        raise ValueError("QSTASH_URL must be an HTTPS service URL")
    # Never retry inline in TradingView's request.
    with httpx.Client(timeout=httpx.Timeout(0.5), follow_redirects=False) as client:
        response = client.post(
            str(queue_url).rstrip("/") + "/v2/publish/" + destination,
            headers={
                "Authorization": "Bearer " + os.environ["QSTASH_TOKEN"],
                "Upstash-Forward-Authorization": "Bearer " + os.environ["WORKER_TOKEN"],
                "Upstash-Deduplication-Id": fingerprint(alert),
                "Upstash-Retries": "5",
                "Upstash-Timeout": f"{QSTASH_DELIVERY_TIMEOUT}s",
            },
            json=alert.model_dump(mode="json"),
        )
        response.raise_for_status()


@app.post("/api/v1/tv-alert-x7k9m2p")
def tradingview_webhook():
    if not configured("TV_WEBHOOK_SECRET", "QSTASH_URL", "QSTASH_TOKEN", "WORKER_TOKEN", "WORKER_URL"):
        return jsonify(error="Server misconfigured"), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Expected a JSON object"), 400
    if not authorized(data.get("secret"), "TV_WEBHOOK_SECRET"):
        return jsonify(error="Unauthorized"), 401
    try:
        alert = Alert.model_validate({k: v for k, v in data.items() if k != "secret"})
    except ValidationError as exc:
        # Do not echo input values, which might contain credentials.
        fields = sorted({str(e["loc"][0]) if e["loc"] else "body" for e in exc.errors()})
        return jsonify(error="Invalid alert", fields=fields), 400
    age = (datetime.now(timezone.utc) - alert.time).total_seconds()
    if age > 86400 or age < -300:
        return jsonify(error="Alert time must be within the past 24 hours, with at most 5 minutes clock skew"), 400
    job_id = job_id_for(alert)
    try:
        publish(alert, job_id)
    except Exception as exc:
        app.logger.error("Queue publish failed (%s)", type(exc).__name__)
        return jsonify(error="Queue unavailable"), 503
    return jsonify(status="accepted", job_id=job_id), 202


@app.post("/api/v1/analyze-e8f8323f5eecdfce")
def analysis_worker():
    if not configured("WORKER_TOKEN", "REDIS_URL", "GROK_WEBHOOK_URL", "GROK_WEBHOOK_KEY", "GROK_CALLBACK_TOKEN"):
        return jsonify(error="Server misconfigured"), 503
    if not bearer_authorized("WORKER_TOKEN"):
        return jsonify(error="Unauthorized"), 401
    try:
        alert = Alert.model_validate(request.get_json(silent=True))
    except ValidationError:
        return jsonify(error="Invalid queued alert"), 400
    job_id = job_id_for(alert)
    # Shared hash tag supports Redis Cluster multi-key Lua scripts.
    prefix = "tv:{" + job_id + "}:"
    lock_key, result_key = prefix + "lock", prefix + "result"
    owner = uuid4().hex
    store = None
    acquired = False
    started = time.monotonic()
    stage = "redis_lookup"
    try:
        store = redis_client()
        existing = store.get(result_key)
        if existing:
            existing_record = json.loads(existing)
            if existing_record["fingerprint"] != fingerprint(alert):
                return jsonify(error="alert_id already used for different data"), 409
            return jsonify(status=existing_record["status"], job_id=job_id, duplicate=True), 200
        pending = store.get(prefix + "bot_pending")
        if pending:
            if json.loads(pending)["fingerprint"] != fingerprint(alert):
                return jsonify(error="alert_id already used for different data"), 409
        stage = "redis_lock"
        acquired = bool(store.set(lock_key, owner, nx=True, ex=LOCK_TTL))
        if not acquired:
            return jsonify(error="Analysis in progress"), 503
        # Another invocation may have completed between the first GET and SET NX.
        existing = store.get(result_key)
        if existing:
            existing_record = json.loads(existing)
            if existing_record["fingerprint"] != fingerprint(alert):
                return jsonify(error="alert_id already used for different data"), 409
            return jsonify(status=existing_record["status"], job_id=job_id, duplicate=True), 200
        stage = "bot_dispatch"
        dispatch_grok_bot(store, alert, job_id, fingerprint(alert))
        return jsonify(status="awaiting_callback", job_id=job_id), 202
    except Exception as exc:
        app.logger.error(
            "Analysis failed job_id=%s (%s) stage=%s elapsed_seconds=%.3f cause=%s",
            job_id, type(exc).__name__, stage, time.monotonic() - started,
            type(exc.__cause__).__name__ if exc.__cause__ else "none",
        )
        return jsonify(error="Analysis temporarily unavailable"), 503
    finally:
        if acquired and store is not None:
            try:
                store.eval(RELEASE, 1, lock_key, owner)
            except Exception:
                app.logger.error("Worker lock cleanup failed job_id=%s", job_id)
        if store is not None:
            store.close()


@app.get(RESULTS_PATH)
def list_results():
    if not configured("RESULT_API_TOKEN", "REDIS_URL"):
        return jsonify(error="Server misconfigured"), 503
    if not bearer_authorized("RESULT_API_TOKEN"):
        return jsonify(error="Unauthorized"), 401
    cursor_text = request.args.get("cursor", "0")
    count_text = request.args.get("count", "20")
    if (
        not cursor_text.isascii() or not cursor_text.isdecimal()
        or len(cursor_text) > 20 or int(cursor_text) > 2**64 - 1
        or not count_text.isascii() or not count_text.isdecimal()
        or len(count_text) > 2 or not 1 <= int(count_text) <= 50
    ):
        return jsonify(error="Use a valid cursor and count between 1 and 50"), 400
    try:
        with redis_client() as store:
            # SCAN avoids blocking Redis with KEYS or reading every result at once.
            # COUNT is a work hint, not an exact page size. Cursor 0 means done.
            next_cursor, keys = store.scan(
                cursor=int(cursor_text), match="tv:*:result", count=int(count_text),
            )
            values = []
            if keys:
                # Independent GETs work across hash slots; avoid multi-key MGET.
                with store.pipeline(transaction=False) as pipeline:
                    for key in keys:
                        pipeline.get(key)
                    values = pipeline.execute()
        # A result can expire between SCAN and GET. Skip it without failing a page.
        results = [with_report(json.loads(value)) for value in values if value is not None]
        return jsonify(results=results, next_cursor=str(next_cursor)), 200
    except Exception as exc:
        app.logger.error("Result listing failed (%s)", type(exc).__name__)
        return jsonify(error="Storage unavailable"), 503


@app.get(RESULTS_PATH + "/<job_id>")
def get_result(job_id):
    if not configured("RESULT_API_TOKEN", "REDIS_URL"):
        return jsonify(error="Server misconfigured"), 503
    if not bearer_authorized("RESULT_API_TOKEN"):
        return jsonify(error="Unauthorized"), 401
    if not valid_job_id(job_id):
        return jsonify(error="Invalid job ID"), 400
    output_format = request.args.get("format", "json")
    if output_format not in ("json", "text"):
        return jsonify(error="format must be json or text"), 400
    try:
        with redis_client() as store:
            result = store.get("tv:{" + job_id + "}:result")
        if result is None:
            return jsonify(error="Result not available"), 404
        record = with_report(json.loads(result))
        if output_format == "text":
            if not record["recommendation_text"]:
                return jsonify(error="Readable report not available; use the JSON result"), 409
            return Response(record["recommendation_text"], mimetype="text/plain")
        return jsonify(record), 200
    except Exception as exc:
        app.logger.error("Result lookup failed (%s)", type(exc).__name__)
        return jsonify(error="Storage unavailable"), 503


@app.post(REPORTS_PATH + "/<job_id>/link")
def create_report_link(job_id):
    if not configured("RESULT_API_TOKEN", "REDIS_URL", "WORKER_URL"):
        return jsonify(error="Server misconfigured"), 503
    if not bearer_authorized("RESULT_API_TOKEN"):
        return jsonify(error="Unauthorized"), 401
    if not valid_job_id(job_id):
        return jsonify(error="Invalid job ID"), 400
    try:
        with redis_client() as store:
            value = store.get("tv:{" + job_id + "}:result")
        if value is None:
            return jsonify(error="Result not available"), 404
        record = with_report(json.loads(value))
        if not record["report_url"]:
            return jsonify(error="Readable report not available for this result"), 409
        return jsonify({key: record[key] for key in (
            "job_id", "report_url", "report_expires_at", "notification_text", "notification_message",
        )}), 200
    except Exception as exc:
        app.logger.error("Report link generation failed (%s)", type(exc).__name__)
        return jsonify(error="Report temporarily unavailable"), 503


@app.get(REPORTS_PATH + "/<job_id>")
def view_report(job_id):
    if not configured("RESULT_API_TOKEN", "REDIS_URL"):
        return jsonify(error="Server misconfigured"), 503
    if not valid_report_signature(job_id, request.args.get("expires"), request.args.get("signature")):
        return jsonify(error="Invalid or expired report link"), 403
    try:
        with redis_client() as store:
            value = store.get("tv:{" + job_id + "}:result")
        if value is None:
            return jsonify(error="Report not available"), 404
        record = json.loads(value)
        if not (record.get("analysis") or {}).get("recommendation_text"):
            return jsonify(error="Readable report not available for this result"), 409
        response = Response(render_template(
            "report.html", record=record,
            alert_json=json.dumps(record.get("alert", {}), indent=2, ensure_ascii=False),
            analysis_json=json.dumps(record["analysis"], indent=2, ensure_ascii=False),
            bot_response_json=json.dumps(record["bot_response"], indent=2, ensure_ascii=False) if "bot_response" in record else None,
        ), mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'"
        )
        return response
    except Exception as exc:
        app.logger.error("Report rendering failed (%s)", type(exc).__name__)
        return jsonify(error="Report temporarily unavailable"), 503


@app.after_request
def prevent_caching(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.endpoint != "home":
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/")
def home():
    return render_template("home.html", year=datetime.now(timezone.utc).year)


@app.get("/health")
def health():
    # Liveness only; this does not claim external services are healthy.
    return jsonify(status="ok"), 200
