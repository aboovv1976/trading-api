"""Queued WhatsApp lookups and per-user conversations."""

import hashlib
import json
import os
import re
import time
from datetime import datetime

import httpx
from flask import current_app

from reports import with_report
from whatsapp_chat import current as chat_current
from whatsapp_chat import dispatch as dispatch_chat
from whatsapp_chat import remember_context

WORKER_PATH = "/api/v1/whatsapp-reply-71f295b68cd903ea"
TTL = 7 * 86400
HELP = ("Trade-notify commands:\n/latest NVDA\n/latest NVDA, AAPL\n"
        "/latest NASDAQ:NVDA\n/latest all — list available tickers\n/reset — clear your conversation\n/help\n\n"
        "Returns the single latest stored recommendation per ticker. "
        "Use an exchange prefix if a symbol is ambiguous. Maximum 10 tickers. "
        "Lookups never call the bot. /reset resets bot history. Send any text without / to chat about stocks or Trade-notify.")


def allowed(sender):
    numbers = {n.strip().removeprefix("+") for n in os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "").split(",") if n.strip()}
    return isinstance(sender, str) and sender.isascii() and sender.isdecimal() and sender in numbers


def parse_command(text):
    if not isinstance(text, str) or not text.strip().startswith("/"):
        return None
    parts = text.strip().split(None, 1)
    if parts[0].lower() == "/help" and len(parts) == 1:
        return []
    if parts[0].lower() != "/latest" or len(parts) != 2:
        return []
    if parts[1].strip().lower() == "all":
        return ["*"]
    symbols = [s.strip().upper() for s in parts[1].split(",")]
    if not 1 <= len(symbols) <= 10 or any(not re.fullmatch(r"[A-Z0-9_.:/!\-]{1,40}", s) for s in symbols):
        return []
    return list(dict.fromkeys(symbols))


def enqueue(store, message, phone_id):
    content = message.get("text")
    text = content.get("body") if message.get("type") == "text" and isinstance(content, dict) else None
    if not isinstance(text, str) or not text.strip():
        current_app.logger.info("WhatsApp message ignored reason=not_text_or_empty")
        return
    if not isinstance(message.get("id"), str) or not message["id"]:
        raise ValueError("Missing WhatsApp message ID")
    # Only the configured business number can trigger an outgoing message.
    configured_phone = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not configured_phone:
        current_app.logger.warning("WhatsApp message ignored reason=business_phone_not_configured")
        return
    if phone_id != configured_phone:
        current_app.logger.warning("WhatsApp message ignored reason=business_phone_mismatch")
        return
    missing = [name for name in ("WHATSAPP_ACCESS_TOKEN", "WORKER_TOKEN", "WORKER_URL", "QSTASH_URL", "QSTASH_TOKEN") if not os.environ.get(name)]
    if missing:
        current_app.logger.error("WhatsApp reply configuration missing variables=%s", ",".join(missing))
        raise ValueError("WhatsApp replies not configured")
    job_id = hashlib.sha256((phone_id + ":" + message["id"]).encode()).hexdigest()
    key = "wa:command:" + job_id
    message = {**message, "text": {"body": text[:6000]}}
    store.set(key, json.dumps({"message": message, "phone_id": phone_id}), nx=True, ex=TTL)
    if store.exists(key + ":done"):
        return
    publish_job(job_id)
    current_app.logger.info("WhatsApp message queued job_id=%s kind=%s", job_id, "command" if text.lstrip().startswith("/") else "chat")


def publish_job(job_id):
    origin = httpx.URL(os.environ["WORKER_URL"])
    queue = httpx.URL(os.environ["QSTASH_URL"])
    if any(url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment for url in (origin, queue)):
        raise ValueError("Invalid queue configuration")
    destination = str(origin.copy_with(path=WORKER_PATH))
    with httpx.Client(timeout=2, follow_redirects=False) as client:
        response = client.post(str(queue).rstrip("/") + "/v2/publish/" + destination,
            headers={"Authorization": "Bearer " + os.environ["QSTASH_TOKEN"],
                     "Upstash-Forward-Authorization": "Bearer " + os.environ["WORKER_TOKEN"],
                     "Upstash-Retries": "5", "Upstash-Timeout": "300s"}, json={"job_id": job_id})
        response.raise_for_status()


def recommendations(store, symbols, selected=None):
    # Keep only the newest record per symbol, and fail rather than return a
    # partial scan as though it represented the newest results.
    top = {}
    seen = set()
    for key in store.scan_iter(match="tv:*:result", count=100):
        if key in seen:
            continue
        seen.add(key)
        if len(seen) > 10000:
            return ["Too many stored results to search right now. Please try again later."]
        raw = store.get(key)
        if not raw:
            continue
        try:
            record = json.loads(raw)
            ticker = record["alert"]["ticker"].upper()
            analysis = record.get("analysis")
            if record.get("status") != "completed" or not isinstance(analysis, dict) or not analysis.get("notification_text"):
                continue
            if symbols != ["*"] and not any(s == ticker or (":" not in s and s == ticker.split(":")[-1]) for s in symbols):
                continue
            completed = datetime.fromisoformat(record["completed_at"].replace("Z", "+00:00"))
            if completed.tzinfo is None:
                continue
            if completed.timestamp() + TTL <= time.time():
                continue
            candidate = (completed.timestamp(), record["job_id"], record)
            if ticker not in top or candidate[:2] > top[ticker][:2]:
                top[ticker] = candidate
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    if symbols == ["*"]:
        if not top:
            return ["No tickers with stored recommendations available (results expire after 7 days)."]
        return chunks("Available tickers with stored recommendations:\n" + "\n".join(sorted(top)))
    replies = []
    for symbol in symbols:
        matches = sorted(t for t in top if t == symbol or (":" not in symbol and t.split(":")[-1] == symbol))
        if len(matches) > 1:
            replies.append(f"{symbol} is ambiguous. Use one of: {', '.join(matches)}")
            continue
        if not matches:
            replies.append(f"{symbol}: No stored recommendations available (results expire after 7 days).")
            continue
        ticker = matches[0]
        if selected is not None:
            selected.append(top[ticker][2]["job_id"])
        lines = [f"{ticker} — latest stored recommendation"]
        record = top[ticker][2]
        analysis = record.get("analysis") or {}
        summary = analysis["notification_text"]
        lines.append(f"\n{record['completed_at']} | interval {record['alert'].get('interval', 'unknown')}\n{str(summary)[:300]}")
        entry = analysis.get("entry_range")
        if entry and analysis.get("decision") == "Buy":
            lines.append(f"Entry range: {entry['low']}–{entry['high']}")
            lines.append(f"Stop: {analysis['stop_loss']}")
            targets = analysis["targets"]
            if targets:
                lines.append("Targets (bot order): " + ", ".join(str(target["price"]) for target in targets))
        if os.environ.get("RESULT_API_TOKEN") and os.environ.get("WORKER_URL"):
            try:
                link = with_report(record).get("report_url")
                if link:
                    lines.append("Report: " + link)
            except (ValueError, KeyError, TypeError):
                pass
        replies.append("\n".join(lines))
    # Stay below Meta's 4096-character text limit without splitting URLs.
    return [part for text in replies for part in chunks(text)]


def chunks(text):
    parts = []
    current = ""
    for line in text.splitlines():
        if len(current) + len(line) + 1 > 3500 and current:
            parts.append(current)
            current = ""
        while len(line) > 3500:
            parts.append(line[:3500])
            line = line[3500:]
        current = current + "\n" + line if current else line
    if current:
        parts.append(current)
    return parts


def process(store, job_id):
    key = "wa:command:" + job_id
    raw = store.get(key)
    if not raw:
        return "expired"
    done = store.get(key + ":done")
    if done:
        return done
    if not chat_current(store, job_id):
        store.set(key + ":done", "superseded", ex=TTL)
        return "superseded"
    job = json.loads(raw)
    message = job["message"]
    if not allowed(message.get("from")) or job["phone_id"] != os.environ.get("WHATSAPP_PHONE_NUMBER_ID"):
        store.set(key + ":done", "ignored", ex=TTL)
        return "ignored"
    try:
        age = time.time() - int(message["timestamp"])
    except (KeyError, TypeError, ValueError):
        age = 86400
    if age >= 86400 or age < -300:
        store.set(key + ":done", "expired", ex=TTL)
        return "expired"
    # Freeze reply content so retries address exactly the same reply parts.
    reply_key = key + ":replies"
    if not store.exists(reply_key):
        text = (message.get("text") or {}).get("body")
        symbols = parse_command(text)
        reset = isinstance(text, str) and text.strip().lower() == "/reset"
        if symbols is None or reset:
            dispatch_chat(store, job_id, job, reset=reset)
            return "awaiting_callback"
        else:
            selected = []
            texts = recommendations(store, symbols, selected=selected) if symbols else [HELP]
            if symbols and symbols != ["*"]:
                remember_context(store, job["phone_id"], message["from"], selected)
            store.set(reply_key, json.dumps(texts), nx=True, ex=TTL)
    texts = json.loads(store.get(reply_key))
    version = os.environ.get("WHATSAPP_GRAPH_API_VERSION", "v25.0")
    if not re.fullmatch(r"v\d+\.\d+", version) or not job["phone_id"].isascii() or not job["phone_id"].isdecimal():
        raise ValueError("Invalid WhatsApp API configuration")
    token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    delivery_status = "completed"
    with httpx.Client(timeout=10, follow_redirects=False) as client:
        for index, text in enumerate(texts):
            part_key = key + f":part:{index}"
            # Reserve before sending: an ambiguous timeout or process crash must
            # not trigger a duplicate send. Meta has no send idempotency key.
            if not store.set(part_key, "reserved", nx=True, ex=TTL):
                state = store.get(part_key)
                if state in ("reserved", "unknown"):
                    delivery_status = "delivery_uncertain"
                elif state != "accepted" and delivery_status == "completed":
                    delivery_status = "delivery_failed"
                continue
            try:
                response = client.post(f"https://graph.facebook.com/{version}/{job['phone_id']}/messages",
                    headers={"Authorization": "Bearer " + token},
                    json={"messaging_product": "whatsapp", "to": message["from"],
                          "context": {"message_id": message["id"]}, "type": "text", "text": {"body": text}})
            except httpx.TransportError:
                store.set(part_key, "unknown", ex=TTL)
                raise
            if response.status_code == 429:
                store.delete(part_key)
                response.raise_for_status()
            if not response.is_success:
                store.set(part_key, "rejected:" + str(response.status_code), ex=TTL)
                try:
                    error = response.json().get("error", {})
                    code = error.get("code") if isinstance(error.get("code"), int) else None
                    subcode = error.get("error_subcode") if isinstance(error.get("error_subcode"), int) else None
                except (ValueError, AttributeError):
                    code = subcode = None
                current_app.logger.error("WhatsApp send rejected job_id=%s part=%s http_status=%s code=%s subcode=%s", job_id, index, response.status_code, code, subcode)
                raise ValueError("Meta rejected WhatsApp reply")
            store.set(part_key, "accepted", ex=TTL)
            current_app.logger.info("WhatsApp send accepted job_id=%s part=%s", job_id, index)
    store.set(key + ":done", delivery_status, ex=TTL)
    return delivery_status
