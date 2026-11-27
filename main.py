"""TradingView ingress and a queue-driven analysis worker for Vercel."""

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from flask import Flask, jsonify, request
from pydantic import ValidationError
from redis import Redis

from analysis import analyze
from models import Alert

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
RESULT_TTL = 7 * 24 * 60 * 60
LOCK_TTL = 120  # Longer than the configured 60-second Vercel invocation.

# A timed-out worker cannot release another delivery's lock or overwrite its result.
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
COMPLETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
    redis.call('del', KEYS[1])
    return 1
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
                "Upstash-Timeout": "60s",
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
    if not configured("WORKER_TOKEN", "REDIS_URL", "XAI_API_KEY"):
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
    try:
        store = redis_client()
        existing = store.get(result_key)
        if existing:
            if json.loads(existing)["fingerprint"] != fingerprint(alert):
                return jsonify(error="alert_id already used for different data"), 409
            return jsonify(status="completed", job_id=job_id, duplicate=True), 200
        acquired = bool(store.set(lock_key, owner, nx=True, ex=LOCK_TTL))
        if not acquired:
            return jsonify(error="Analysis in progress"), 503
        # Another invocation may have completed between the first GET and SET NX.
        existing = store.get(result_key)
        if existing:
            if json.loads(existing)["fingerprint"] != fingerprint(alert):
                return jsonify(error="alert_id already used for different data"), 409
            return jsonify(status="completed", job_id=job_id, duplicate=True), 200
        result = {
            "job_id": job_id, "fingerprint": fingerprint(alert),
            "alert": alert.model_dump(mode="json"),
            "analysis": analyze(alert).model_dump(mode="json"),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if not store.eval(COMPLETE, 2, lock_key, result_key, owner, json.dumps(result), RESULT_TTL):
            raise RuntimeError("Worker lease expired")
        acquired = False
        app.logger.info("Analysis stored job_id=%s", job_id)
        return jsonify(status="completed", job_id=job_id), 200
    except Exception as exc:
        app.logger.error("Analysis failed job_id=%s (%s)", job_id, type(exc).__name__)
        return jsonify(error="Analysis temporarily unavailable"), 503
    finally:
        if acquired and store is not None:
            try:
                store.eval(RELEASE, 1, lock_key, owner)
            except Exception:
                app.logger.error("Worker lock cleanup failed job_id=%s", job_id)
        if store is not None:
            store.close()


@app.get("/api/v1/results/<job_id>")
def get_result(job_id):
    if not configured("RESULT_API_TOKEN", "REDIS_URL"):
        return jsonify(error="Server misconfigured"), 503
    if not bearer_authorized("RESULT_API_TOKEN"):
        return jsonify(error="Unauthorized"), 401
    if len(job_id) != 64 or any(c not in "0123456789abcdef" for c in job_id):
        return jsonify(error="Invalid job ID"), 400
    try:
        with redis_client() as store:
            result = store.get("tv:{" + job_id + "}:result")
        if result is None:
            return jsonify(error="Result not available"), 404
        return jsonify(json.loads(result)), 200
    except Exception as exc:
        app.logger.error("Result lookup failed (%s)", type(exc).__name__)
        return jsonify(error="Storage unavailable"), 503


@app.after_request
def prevent_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def health():
    # Liveness only; this does not claim external services are healthy.
    return jsonify(status="ok"), 200
