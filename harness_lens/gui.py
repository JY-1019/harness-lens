"""Local web GUI to monitor, manage, and edit the harness (design §4).

The CLI already renders the Flow/Task/Step trajectory and the 3-Layer criteria; this serves the
same data as a single-user, localhost-only dashboard so the harness can be watched live and the
editable layer adjusted from a browser. It is intentionally dependency-free (Python stdlib
``http.server``) so it runs under the same ``uvx`` env as the rest of the package.

Routes:
  * ``GET  /``            — the dashboard HTML (vanilla JS, no external assets).
  * ``GET  /api/flows``   — recent Flow/Task/Step trees with Layer 1/2/3 status (monitor).
  * ``GET  /api/layers``  — the 3-Layer criteria (Layer 1/2 read-only, Layer 3 editable).
  * ``GET  /api/status``  — Judge / hit-rate / gap / candidate counts.
  * ``POST /api/layer3``  — persist an edit of the Layer-3 thresholds, then re-enforce.

Only Layer 3 is editable: Layer 1 (invariants) and Layer 2 (domain criteria) are non-evolvable by
design, so the GUI presents them read-only and accepts edits to Layer 3 alone — the same boundary
the evolver and ``enforce`` honour.
"""

from __future__ import annotations

import json
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .components import ComponentError
from .service import LensService

_PAGE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="hl-token" content="__HL_TOKEN__">
<title>harness-lens</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; padding: 1.5rem; max-width: 60rem; }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; }
  h2 { font-size: 1rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; margin: 1.6rem 0 .8rem; }
  section { margin-bottom: 1.5rem; }
  .flow { border: 1px solid #8884; border-radius: 6px; padding: .6rem .8rem; margin-bottom: .6rem; }
  .flow-head { display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  .layers { display: flex; gap: .8rem; flex-wrap: wrap; font-size: .85rem; opacity: .85; }
  .ok { color: #2a9d3a; } .bad { color: #c0392b; } .muted { opacity: .6; }
  .task { margin: .25rem 0 0 1rem; font-size: .9rem; }
  ul { margin: .3rem 0; padding-left: 1.2rem; }
  form { display: grid; grid-template-columns: max-content max-content; gap: .4rem .8rem; align-items: center; }
  input { font: inherit; width: 7rem; padding: .15rem .3rem; }
  button { font: inherit; padding: .3rem .9rem; cursor: pointer; margin-top: .6rem; }
  .pill { border: 1px solid #8884; border-radius: 999px; padding: 0 .5rem; font-size: .8rem; }
  #msg { margin-left: .8rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .5rem; }
  .card { border: 1px solid #8884; border-radius: 6px; padding: .5rem .7rem; }
  .card b { display: block; font-size: 1.2rem; }
</style>
</head>
<body>
<h1>harness-lens <span class="muted">— Flow / Task / Step · 3-Layer</span></h1>

<section id="status"><h2>Status</h2><div class="grid" id="status-grid"></div></section>

<section><h2>3-Layer 하네스</h2>
  <div id="layer1"></div>
  <div id="layer2"></div>
  <h2>Layer 3 — QA thresholds <span class="muted">(편집 가능)</span></h2>
  <form id="layer3-form"></form>
  <div><button id="save">저장 + 재강제</button><span id="msg"></span></div>
</section>

<section><h2>Flows <span class="muted">(monitor)</span></h2><div id="flows"></div></section>

<script>
const $ = (id) => document.getElementById(id);
const text = (el, s) => { el.textContent = s; return el; };
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };

async function loadStatus() {
  const s = await (await fetch('api/status')).json();
  const grid = $('status-grid'); grid.replaceChildren();
  const card = (label, val) => { const c = el('div', 'card'); c.append(el('b', null, val), el('span', 'muted', label)); return c; };
  grid.append(
    card('예측 적중률', s.prediction_hit_rate == null ? 'n/a' : Math.round(s.prediction_hit_rate * 100) + '%'),
    card('gap 비율', Math.round(s.gap_ratio * 100) + '%'),
    card('Layer 1 invariants', s.layer1.length),
    card('Layer 2 criteria', s.layer2.length),
    card('적용 수정안', s.candidates.applied + s.candidates.confirmed),
  );
  const j = el('div', 'card'); j.append(el('b', null, 'Judge'), el('span', 'muted', s.judge)); grid.append(j);
}

async function loadLayers() {
  const v = await (await fetch('api/layers')).json();
  const l1 = $('layer1'); l1.replaceChildren(el('h2', null, 'Layer 1 — Invariants (읽기 전용)'));
  const ul1 = el('ul'); v.invariants.forEach(r => ul1.append(el('li', null, r))); l1.append(ul1);
  const l2 = $('layer2'); l2.replaceChildren(el('h2', null, 'Layer 2 — Domain criteria (읽기 전용)'));
  const ul2 = el('ul'); v.domain_criteria.forEach(d => ul2.append(el('li', null, `[${d.id}] ${d.description} (weight ${d.weight})`))); l2.append(ul2);
  const form = $('layer3-form'); form.replaceChildren();
  Object.entries(v.layer3).forEach(([k, val]) => {
    form.append(el('label', null, k));
    const inp = el('input'); inp.name = k; inp.value = val; inp.step = 'any'; inp.type = 'number'; form.append(inp);
  });
}

async function loadFlows() {
  const flows = await (await fetch('api/flows?limit=20')).json();
  const box = $('flows'); box.replaceChildren();
  if (!flows.length) { box.append(el('p', 'muted', '기록된 Flow가 없습니다.')); return; }
  flows.forEach(f => {
    const card = el('div', 'flow');
    const head = el('div', 'flow-head');
    head.append(el('span', null, `Flow ${f.session_id.slice(0, 8)} [${f.platform || '?'}] · ${f.status}`),
                el('span', 'pill', `${f.total_tokens.toLocaleString()} tok`));
    const layers = el('div', 'layers');
    const l1 = f.layer1_failed ? el('span', 'bad', `L1 위반 ${f.layer1_failed}`) : el('span', 'ok', 'L1 ok');
    const l2 = el('span', 'muted', 'L2 ' + (f.layer2_avg == null ? 'n/a' : f.layer2_avg.toFixed(2)));
    const trg = f.layer3_triggers || [];
    const l3 = trg.length ? el('span', 'bad', 'L3 ' + trg.join(', ')) : el('span', 'ok', 'L3 ok');
    layers.append(l1, l2, l3);
    if (f.gap_count) layers.append(el('span', 'muted', `gap ${Math.round(f.gap_ratio * 100)}% (관측 불가 ${f.gap_count})`));
    card.append(head, layers);
    (f.tasks || []).forEach((t, i) => {
      const fails = t.steps.filter(s => s.success === false).length;
      // A gap step has no observed outcome (success === null), so a partly-unobserved task
      // is "?" rather than a misleading ✅ — matching the CLI's treatment.
      const unobserved = t.steps.some(s => s.observed === false);
      const flag = fails ? '⚠' : (unobserved ? '?' : '✅');
      card.append(el('div', 'task', `${flag} Task ${i + 1} [${t.category}] · ${t.steps.length} steps`));
    });
    box.append(card);
  });
}

$('save').addEventListener('click', async () => {
  const params = {};
  $('layer3-form').querySelectorAll('input').forEach(i => { if (i.value !== '') params[i.name] = Number(i.value); });
  const msg = $('msg');
  const token = document.querySelector('meta[name=hl-token]').content;
  const res = await fetch('api/layer3', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HL-Token': token }, body: JSON.stringify(params) });
  const body = await res.json();
  if (res.ok) { text(msg, '저장됨 · 재강제 완료').className = 'ok'; await loadLayers(); await loadFlows(); }
  else { text(msg, '거부: ' + (body.error || res.status)).className = 'bad'; }
});

loadStatus(); loadLayers(); loadFlows();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "harness-lens-gui"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, *args) -> None:  # silence default request logging
        pass

    # A fresh service per request keeps each SQLite connection on its own request,
    # so the single-threaded server never shares a handle across calls.
    def _with_service(self, fn):
        service = LensService()
        try:
            return fn(service)
        finally:
            service.close()

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json(403, {"error": "forbidden"})
            return
        route = urlparse(self.path)
        path = route.path
        if path == "/":
            page = _PAGE.replace("__HL_TOKEN__", self.server.csrf_token)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/flows":
            qs = parse_qs(route.query)
            limit = int(qs.get("limit", ["20"])[0])
            only_failed = qs.get("fail", ["0"])[0] in ("1", "true")
            flows = self._with_service(lambda s: s.get_flow_summary(limit=limit, only_failed=only_failed))
            self._json(200, flows)
            return
        if path == "/api/layers":
            self._json(200, self._with_service(lambda s: s.layers_view()))
            return
        if path == "/api/status":
            self._json(200, self._with_service(self._status_payload))
            return
        self._json(404, {"error": "not found"})

    def _host_ok(self) -> bool:
        # Require the loopback name we serve on every request (reads included): under DNS
        # rebinding a malicious page keeps ``Host: attacker.example`` while its DNS is repointed
        # at 127.0.0.1, so without this check it could read the dashboard / ledger data as
        # same-origin. Loopback binding alone does not prevent that.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _csrf_ok(self) -> bool:
        # Writes need a second gate beyond the Host check: X-HL-Token must match the per-run
        # token embedded in our page. A cross-origin page cannot read it (same-origin policy)
        # and a no-cors POST cannot set a custom header, so a plain CSRF submission is refused.
        return secrets.compare_digest(self.headers.get("X-HL-Token") or "", self.server.csrf_token)

    def do_POST(self) -> None:
        if not self._host_ok() or not self._csrf_ok():
            self._json(403, {"error": "forbidden"})
            return
        if urlparse(self.path).path != "/api/layer3":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            params = json.loads(raw or b"{}")
            if not isinstance(params, dict):
                raise ValueError("expected a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": f"invalid JSON body: {exc}"})
            return
        try:
            layer3 = self._with_service(lambda s: s.update_layer3(params))
        except ComponentError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"layer3": layer3})

    @staticmethod
    def _status_payload(service: LensService) -> dict:
        s = service.status()
        judge = s["judge"]
        return {
            "judge": judge.recommendation,
            "prediction_hit_rate": s["prediction_hit_rate"],
            "gap_ratio": s["gap_ratio"],
            "layer1": s["layer1"],
            "layer2": s["layer2"],
            "candidates": s["candidates"],
        }


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Run the localhost GUI server until interrupted.

    Bound to loopback only — this exposes a write endpoint (Layer-3 edits) and is meant for the
    single local user, not the network.
    """
    httpd = HTTPServer((host, port), _Handler)
    # Per-run CSRF token: embedded in the served page, required on every write request.
    httpd.csrf_token = secrets.token_urlsafe(32)
    url = f"http://{host}:{port}/"
    print(f"harness-lens GUI → {url}  (Ctrl-C 로 종료)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nGUI 종료.")
    finally:
        httpd.server_close()
