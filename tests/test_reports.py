"""Report-link authorization, expiry, and rendering with actual Flask routes."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import fakeredis
import pytest

import analysis
import main
from models import Recommendation
from reports import REPORTS_PATH, REPORT_LIFETIME, signature

AUTH = {"Authorization": "Bearer result-test"}
JOB = "c" * 64


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("RESULT_API_TOKEN", "result-test")
    monkeypatch.setenv("REDIS_URL", "redis://unused")
    monkeypatch.setenv("WORKER_URL", "https://production.example/api/v1/analyze-hidden")
    server = fakeredis.FakeServer()
    monkeypatch.setattr(main, "redis_client", lambda: fakeredis.FakeRedis(server=server, decode_responses=True))
    recommendation = analysis.watch("Not enough evidence.").model_dump(mode="json")
    recommendation.update(
        recommendation_text="TRIGGER\nReported green flip.\nINPUTS\nNASDAQ:NVDA\nRECOMMENDATION\nWatch.\nRISKS AND LIMITATIONS\nInsufficient data.\nSOURCES\nNone.",
        notification_text="NVDA — Watch. Insufficient evidence for the 5-minute alert.",
    )
    record = {
        "job_id": JOB, "fingerprint": "test", "completed_at": datetime.now(timezone.utc).isoformat(),
        "alert": {"ticker": "NASDAQ:NVDA", "signal": "green", "price": 150},
        "analysis": recommendation,
    }
    with main.redis_client() as store:
        store.set("tv:{" + JOB + "}:result", json.dumps(record), ex=REPORT_LIFETIME)
    main.app.config.update(TESTING=True)
    return main.app.test_client(), record


def local_url(url):
    parsed = urlsplit(url)
    return parsed.path + "?" + parsed.query


def link_for(client):
    response = client.post(REPORTS_PATH + "/" + JOB + "/link", headers=AUTH)
    assert response.status_code == 200
    return response.json


def test_link_creation_authentication_and_metadata(setup):
    client, record = setup
    route = REPORTS_PATH + "/" + JOB + "/link"
    assert client.post(route).status_code == 401
    link = link_for(client)
    assert link["report_url"].startswith("https://production.example/")
    assert "result-test" not in link["report_url"]
    assert record["analysis"]["notification_text"] in link["notification_message"]
    assert link["report_url"] in link["notification_message"]
    assert client.post(REPORTS_PATH + "/invalid/link", headers=AUTH).status_code == 400
    assert client.post(REPORTS_PATH + "/" + "d" * 64 + "/link", headers=AUTH).status_code == 404


def test_signed_link_renders_without_api_token(setup):
    client, record = setup
    link = link_for(client)
    response = client.get(local_url(link["report_url"]))
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert "NASDAQ:NVDA" in response.text
    assert record["analysis"]["recommendation_text"] in response.text
    assert "Original validated alert" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("tamper", ["signature", "expires", "job_id", "missing", "expired", "unicode"])
def test_invalid_links_rejected(setup, tamper):
    client, _ = setup
    url = urlsplit(link_for(client)["report_url"])
    query = {key: value[0] for key, value in parse_qs(url.query).items()}
    path = url.path
    if tamper == "signature":
        query["signature"] = "0" * 64
    elif tamper == "expires":
        query["expires"] = str(int(query["expires"]) + 1)
    elif tamper == "job_id":
        path = path.replace(JOB, "d" * 64)
    elif tamper == "missing":
        query = {}
    elif tamper == "expired":
        query["expires"] = str(int(datetime.now(timezone.utc).timestamp()) - 1)
        query["signature"] = signature(JOB, query["expires"])
    else:
        query["signature"] = "é" * 64
    assert client.get(path + "?" + urlencode(query)).status_code == 403


def test_key_rotation_invalidates_report_links(setup, monkeypatch):
    client, _ = setup
    url = local_url(link_for(client)["report_url"])
    monkeypatch.setenv("RESULT_API_TOKEN", "new-key")
    assert client.get(url).status_code == 403


def test_deleted_result_not_available_through_signed_link(setup):
    client, _ = setup
    url = local_url(link_for(client)["report_url"])
    with main.redis_client() as store:
        store.delete("tv:{" + JOB + "}:result")
    assert client.get(url).status_code == 404


def test_reports_escape_model_and_alert_html(setup):
    client, record = setup
    record["analysis"]["recommendation_text"] = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    record["alert"]["ticker"] = "<svg onload=alert(1)>"
    with main.redis_client() as store:
        store.set("tv:{" + JOB + "}:result", json.dumps(record))
    page = client.get(local_url(link_for(client)["report_url"]))
    assert page.status_code == 200
    assert "<script>" not in page.text
    assert "<svg " not in page.text
    assert "<img " not in page.text
    assert "&lt;script&gt;" in page.text


def test_links_do_not_extend_retention_or_trust_host_header(setup):
    client, _ = setup
    route = REPORTS_PATH + "/" + JOB + "/link"
    first = client.post(route, headers={**AUTH, "Host": "attacker.example"}).json
    second = client.post(route, headers=AUTH).json
    assert first["report_url"] == second["report_url"]
    assert first["report_url"].startswith("https://production.example/")
    with main.redis_client() as store:
        assert 0 < store.ttl("tv:{" + JOB + "}:result") <= REPORT_LIFETIME


def test_report_link_lifetime_matches_result_storage():
    assert REPORT_LIFETIME == main.RESULT_TTL


def test_existing_json_and_text_views_use_model_report(setup):
    client, record = setup
    response = client.get(main.RESULTS_PATH + "/" + JOB, headers=AUTH)
    assert response.json["recommendation_text"] == record["analysis"]["recommendation_text"]
    assert response.json["report_url"]
    assert response.json["notification_message"]
    assert client.get(main.RESULTS_PATH + "/" + JOB + "?format=text", headers=AUTH).text == record["analysis"]["recommendation_text"]
    response = client.get(main.RESULTS_PATH, headers=AUTH)
    assert response.json["results"][0]["report_url"]


def test_legacy_json_available_but_no_formatter_or_link(setup):
    client, record = setup
    del record["analysis"]["recommendation_text"]
    del record["analysis"]["notification_text"]
    with main.redis_client() as store:
        store.set("tv:{" + JOB + "}:result", json.dumps(record))
    response = client.get(main.RESULTS_PATH + "/" + JOB, headers=AUTH)
    assert response.status_code == 200
    assert response.json["report_url"] is None
    assert response.json["recommendation_text"] is None
    assert client.post(REPORTS_PATH + "/" + JOB + "/link", headers=AUTH).status_code == 409
    assert client.get(main.RESULTS_PATH + "/" + JOB + "?format=text", headers=AUTH).status_code == 409


def test_new_analysis_requires_model_written_text():
    data = analysis.watch("Test").model_dump()
    del data["notification_text"]
    with pytest.raises(ValueError):
        Recommendation.model_validate(data)
