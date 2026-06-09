"""End-to-end wiring of the local GUI: the page serves, reads expose requests, and the
human-edit POST routes (Layer 1/2/3) apply through the same loopback + CSRF gates."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import HTTPServer

import pytest

from harness_lens import gui
from harness_lens.reconstructor import Reconstructor
from harness_lens.service import LensService


@pytest.fixture
def server(tmp_home, monkeypatch):
    # Keep criteria writes from re-enforcing the developer's real instruction files.
    import harness_lens.detector as detector

    monkeypatch.setattr(detector, "detect_all", lambda: [])

    # Seed one Flow with a request so /api/flows has something to expose.
    svc = LensService(root=tmp_home)
    recon = Reconstructor(svc.store)
    recon.on_session_start("G1", platform="claude-code")
    recon.on_user_prompt("G1", "GUI에서 보일 요청")
    recon.on_pre_tool("G1", "Read", "x")
    recon.on_post_tool("G1", "Read", "ok", success=True)
    svc.close()

    httpd = HTTPServer(("127.0.0.1", 0), gui._Handler)
    httpd.csrf_token = "test-token"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address, httpd.csrf_token
    finally:
        httpd.shutdown()
        httpd.server_close()


def _req(addr, method, path, token=None, body=None):
    conn = http.client.HTTPConnection(*addr)
    headers = {"Host": "127.0.0.1"}
    if token:
        headers["X-HL-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body)
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_page_and_flow_requests(server):
    addr, _token = server
    status, page = _req(addr, "GET", "/")
    assert status == 200 and b"harness-lens" in page
    # The editable-layer + requests JS surface is present.
    assert b"renderLayer1" in page and b"api/layer1" in page and b"requests" in page

    status, raw = _req(addr, "GET", "/api/flows")
    assert status == 200
    flows = json.loads(raw)
    assert flows and [r["text"] for r in flows[0]["requests"]] == ["GUI에서 보일 요청"]
    assert flows[0]["tasks"][0]["request"] == "GUI에서 보일 요청"


def test_layer1_2_3_edit_routes(server):
    addr, token = server

    # Layer 1 — invariants.
    status, raw = _req(addr, "POST", "/api/layer1", token, {"invariants": ["X 금지", "Y 금지"]})
    assert status == 200
    assert json.loads(raw)["invariants"] == ["X 금지", "Y 금지"]

    # Layer 2 — domain criteria (missing id auto-assigned).
    status, raw = _req(addr, "POST", "/api/layer2", token,
                       {"domain_criteria": [{"description": "새 기준", "weight": 1.5}]})
    assert status == 200
    dcs = json.loads(raw)["domain_criteria"]
    assert dcs[0]["id"].startswith("DC-") and dcs[0]["weight"] == 1.5

    # Layer 3 — thresholds.
    status, raw = _req(addr, "POST", "/api/layer3", token, {"retry_threshold": 5})
    assert status == 200
    assert json.loads(raw)["layer3"]["retry_threshold"] == 5


def test_write_requires_csrf_token(server):
    addr, _token = server
    status, _ = _req(addr, "POST", "/api/layer1", token=None, body={"invariants": []})
    assert status == 403  # no X-HL-Token → refused


def test_bad_host_refused(server):
    addr, _token = server
    conn = http.client.HTTPConnection(*addr)
    conn.request("GET", "/api/flows", headers={"Host": "evil.example"})
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 403  # DNS-rebinding guard
