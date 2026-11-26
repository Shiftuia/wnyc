"""HTTP surface: health, input limits, per-visitor limits that cannot be dodged with X-Forwarded-For,
concurrency, path traversal, tile bounds, security headers."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agent
import server

client = TestClient(server.app)


def ask(q="I use a wheelchair, $3,000, I work at Times Square", headers=None):
    r = client.post("/api/ask", json={"question": q}, headers=headers or {})
    return r, [json.loads(line) for line in r.text.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def reset_limits(monkeypatch):
    for t in server._hits.values():
        t.clear()
    server._day.update(date="", n=0)
    monkeypatch.setattr(agent, "run", lambda question, fresh=False: iter([{"type": "answer", "text": "ok", "model": None},
                                                                          {"type": "done", "seconds": 0}]))


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["zones"] == 2
    assert r.json()["model"] == agent.MODEL


def test_question_length_limited():
    assert client.post("/api/ask", json={"question": "x" * 1001}).status_code == 422
    assert client.post("/api/ask", json={"question": ""}).status_code == 422
    assert client.post("/api/ask", content=b"{" * 10, headers={"content-type": "application/json"}).status_code == 422


def test_per_visitor_limit_not_spoofable_with_xff(monkeypatch):
    monkeypatch.setattr(server, "PER_IP_HOUR", 2)
    me = {"X-WNYC-Client": "203.0.113.7"}
    assert ask(headers=me)[1][0]["type"] == "answer"
    assert ask(headers={**me, "X-Forwarded-For": "198.51.100.1"})[1][0]["type"] == "answer"
    ev = ask(headers={**me, "X-Forwarded-For": "198.51.100.2", "Forwarded": "for=198.51.100.3",
                      "X-Real-IP": "198.51.100.4"})[1]
    assert ev[0]["type"] == "error" and "last hour" in ev[0]["message"]
    assert ask(headers={"X-WNYC-Client": "203.0.113.8"})[1][0]["type"] == "answer"


def test_client_header_ignored_unless_trusted(monkeypatch):
    monkeypatch.setattr(server, "TRUST_CLIENT_HEADER", False)
    monkeypatch.setattr(server, "PER_IP_HOUR", 1)
    assert ask(headers={"X-WNYC-Client": "a"})[1][0]["type"] == "answer"
    assert ask(headers={"X-WNYC-Client": "b"})[1][0]["type"] == "error"  # same TCP peer


def test_daily_limit(monkeypatch):
    monkeypatch.setattr(server, "PER_DAY", 1)
    assert ask(headers={"X-WNYC-Client": "c1"})[1][0]["type"] == "answer"
    assert "today" in ask(headers={"X-WNYC-Client": "c2"})[1][0]["message"]


def test_concurrency_limit():
    taken = 0
    while server._ask_slots.acquire(blocking=False):
        taken += 1
    try:
        ev = ask()[1]
        assert ev[0]["type"] == "error" and "busy" in ev[0]["message"]
    finally:
        for _ in range(taken):
            server._ask_slots.release()
    assert ask(headers={"X-WNYC-Client": "c3"})[1][0]["type"] == "answer"


def test_slot_released_after_error(monkeypatch):
    def boom(question, fresh=False):
        raise RuntimeError("x")
        yield
    monkeypatch.setattr(agent, "run", boom)
    for i in range(server.ASK_CONCURRENCY + 2):
        assert ask(headers={"X-WNYC-Client": f"e{i}"})[1][0]["type"] == "error"
    monkeypatch.undo()


def test_lookup_limit(monkeypatch):
    monkeypatch.setattr(server, "LOOKUPS_PER_IP_HOUR", 2)
    monkeypatch.setattr(server.engine, "geocode", lambda q: {"error": "offline"})
    h = {"X-WNYC-Client": "g1"}
    assert client.get("/api/geocode?q=a", headers=h).status_code == 200
    assert client.get("/api/geocode?q=b", headers=h).status_code == 200
    assert client.get("/api/geocode?q=c", headers=h).status_code == 429


@pytest.mark.parametrize("body", [
    {"origin": {"lat": "x", "lon": 1}, "destination": "Times Square"},
    {"origin": {"lat": 10.0, "lon": -73.9}, "destination": "Times Square"},
    {"origin": "Times Square", "destination": "x" * 50, "when": "../../etc"},
    {"origin": {"lat": 40.7, "lon": -73.9, "label": "x" * 500}, "destination": "a"},
])
def test_trip_input_validated(body):
    assert client.post("/api/trip", json=body).status_code == 422


@pytest.mark.parametrize("path", [
    "/basemap/tiles/3/9/0.pbf", "/basemap/tiles/3/0/-1.pbf", "/basemap/tiles/15/0/0.pbf", "/basemap/tiles/99/0/0.pbf",
    "/basemap/fonts/..%2F..%2F..%2Fetc/0-255.pbf", "/basemap/fonts/Noto/..%2Fx.pbf", "/basemap/fonts/Noto/0-255%2F..pbf",
    "/basemap/sprite/..%2F..%2Fetc%2Fpasswd",
    "/static/..%2Fagent.py", "/static/../server.py", "/static/%2e%2e/%2e%2e/.env",
    "/api/map/..%2F..%2Fetc%2Fpasswd.png", "/api/map/0123456789abcdef.png",
    "/docs", "/openapi.json",
])
def test_no_traversal_or_out_of_range(path):
    r = client.get(path)
    assert r.status_code in (404, 204), (path, r.status_code)
    assert b"root:" not in r.content and b"ANTHROPIC" not in r.content


def test_security_headers():
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' https://metrics.shiftuia.com;" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_pages_have_no_inline_script_and_carry_tracker_and_support_link():
    import re
    for page in ("/", "/map"):
        html = client.get(page).text
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, f"inline script on {page}: {tag}"
        assert 'data-website-id="09771a91-3c14-401d-b45e-8b874dd8880f"' in html
        assert 'data-domains="wnyc.shiftuia.com"' in html and 'data-exclude-search="true"' in html
        link = re.search(r'<a class="support-link"[^>]*>', html).group(0)
        assert 'href="https://shiftuia.com/support"' in link and 'rel="noopener"' in link
        assert 'data-umami-event="support-click"' in link and 'data-umami-event-placement="wnyc-header"' in link


@pytest.fixture
def basemap(tmp_path, monkeypatch):
    (tmp_path / "style.json").write_text('{"sprite": "{ORIGIN}/basemap/sprite", "glyphs": "{ORIGIN}/basemap/fonts"}')
    monkeypatch.setattr(server, "BASEMAP", tmp_path)


def test_style_origin_from_ingress_headers(basemap):
    r = client.get("/basemap/style.json", headers={"Host": "wnyc.shiftuia.com", "X-Forwarded-Proto": "https",
                                                     "X-Forwarded-Host": "wnyc.shiftuia.com"})
    assert r.json()["sprite"] == "https://wnyc.shiftuia.com/basemap/sprite"   # no mixed content behind TLS


def test_style_origin_rejects_injection(basemap, monkeypatch):
    for h in ({"X-Forwarded-Host": 'evil.example","x":"'}, {"X-Forwarded-Host": "a/b"}, {"Host": "a b"}):
        assert client.get("/basemap/style.json", headers=h).status_code == 400
    assert client.get("/basemap/style.json", headers={"X-Forwarded-Proto": "javascript"}).json()["sprite"].startswith("http://")
    monkeypatch.setattr(server, "TRUST_CLIENT_HEADER", False)   # untrusted peer: forwarded headers ignored
    r = client.get("/basemap/style.json", headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"})
    assert r.json()["sprite"] == "http://testserver/basemap/sprite"
