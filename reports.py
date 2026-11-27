"""Signed read-only links to model-written reports. No narrative generation."""

import hashlib
import hmac
import os
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit

REPORTS_PATH = "/api/v1/reports-da0db27189a6e42d"
REPORT_LIFETIME = 7 * 24 * 60 * 60


def valid_job_id(job_id):
    return len(job_id) == 64 and all(c in "0123456789abcdef" for c in job_id)


def signature(job_id, expires):
    # Domain separation means a report signature cannot be used as an API token.
    message = f"trading-report-v1:{job_id}:{expires}".encode()
    return hmac.new(os.environ["RESULT_API_TOKEN"].encode(), message, hashlib.sha256).hexdigest()


def valid_report_signature(job_id, expires, supplied):
    if not valid_job_id(job_id) or not isinstance(expires, str):
        return False
    if not expires.isascii() or not expires.isdecimal() or len(expires) > 12:
        return False
    if int(expires) <= time.time() or not isinstance(supplied, str):
        return False
    if len(supplied) != 64 or any(c not in "0123456789abcdef" for c in supplied):
        return False
    return hmac.compare_digest(signature(job_id, expires), supplied)


def with_report(record):
    analysis = record.get("analysis") or {}
    text = analysis.get("recommendation_text")
    short = analysis.get("notification_text")
    result = {**record, "recommendation_text": text, "notification_text": short,
              "report_url": None, "report_expires_at": None, "notification_message": None}
    if not text or not valid_job_id(record.get("job_id", "")):
        return result
    completed = datetime.fromisoformat(record["completed_at"].replace("Z", "+00:00"))
    if completed.tzinfo is None:
        raise ValueError("Report completion timestamp must include timezone")
    expires = str(int(completed.timestamp()) + REPORT_LIFETIME)
    if int(expires) <= time.time():
        return result
    # Use the configured production origin, never an incoming Host header.
    worker = urlsplit(os.environ["WORKER_URL"])
    if worker.scheme != "https" or not worker.netloc or worker.username or worker.password:
        raise ValueError("WORKER_URL must have a valid HTTPS production origin")
    base = f"{worker.scheme}://{worker.netloc}"
    query = urlencode({"expires": expires, "signature": signature(record["job_id"], expires)})
    result["report_url"] = f"{base}{REPORTS_PATH}/{record['job_id']}?{query}"
    result["report_expires_at"] = datetime.fromtimestamp(int(expires), completed.tzinfo).isoformat()
    result["notification_message"] = f"{short}\n\nFull report: {result['report_url']}" if short else None
    return result
