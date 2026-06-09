"""Local web GUI to monitor, manage, and edit the harness (design §4).

The CLI already renders the Flow/Task/Step trajectory and the 3-Layer criteria; this serves the
same data as a single-user, localhost-only dashboard so the harness can be watched live and the
editable layer adjusted from a browser. It is intentionally dependency-free (Python stdlib
``http.server``) so it runs under the same ``uvx`` env as the rest of the package.

Routes:
  * ``GET  /``            — the dashboard HTML (vanilla JS, no external assets).
  * ``GET  /api/flows``   — recent Flow/Task/Step trees + the user requests that opened them.
  * ``GET  /api/layers``  — the 3-Layer criteria.
  * ``GET  /api/status``  — Judge / hit-rate / gap / candidate counts.
  * ``POST /api/layer1``  — replace the Layer-1 invariants (human owner edit), then re-enforce.
  * ``POST /api/layer2``  — replace the Layer-2 domain criteria (human owner edit), re-enforce.
  * ``POST /api/layer3``  — persist an edit of the Layer-3 thresholds, then re-enforce.

Editability has two distinct actors. **AHE auto-evolution** stays Layer-3 only — the evolver and
its predictions never touch Layer 1/2 (the CriteriaGuard enforces that). But the **human harness
owner** may edit every layer from here: Layer 3 inline, Layer 2 freely, and Layer 1 behind an
explicit unlock (its invariants are safety rules, so the UI guards against accidental deletion).
Each write backs up ``criteria.yaml`` and re-enforces the managed instruction block.
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
  /* mermaid-style tree: rounded node boxes joined by elbow connectors (dependency-free CSS). */
  .tree { --line: #8886; margin-top: .4rem; }
  .twig { position: relative; padding-left: 1.2rem; }
  .twig::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; border-left: 1px solid var(--line); }
  .twig:last-child::before { bottom: auto; height: 1.05rem; }
  .twig::after { content: ''; position: absolute; left: 0; top: 1.05rem; width: 1rem; border-top: 1px solid var(--line); }
  .node { display: inline-flex; gap: .5rem; align-items: baseline; flex-wrap: wrap;
          border: 1px solid #8886; border-radius: 7px; padding: .25rem .6rem; margin: .35rem 0; }
  .node.task { cursor: pointer; }
  details.twig > summary { list-style: none; }
  details.twig > summary::-webkit-details-marker { display: none; }
  .node.task .caret { opacity: .55; }
  .node .tool { font-weight: 600; }
  .verdict { font-size: .8rem; }
  .io { margin: 0 0 .15rem 1.2rem; opacity: .72; white-space: pre-wrap; word-break: break-word; font-size: .82rem; }
  .io b { opacity: .55; font-weight: 400; margin-right: .3rem; }
  .harness { margin: .05rem 0 .25rem 1.2rem; font-size: .8rem; }
  .harness > div { margin: .05rem 0; }
  .harness b { font-weight: 600; margin-right: .35rem; opacity: .85; }
  .hpass { color: #2a9d3a; } .hfail { color: #c0392b; }
  .sysprompt { margin: .4rem 0 .2rem; font-size: .82rem; }
  .sysprompt > summary { cursor: pointer; }
  .sysprompt pre { white-space: pre-wrap; word-break: break-word; max-height: 22rem; overflow: auto;
                   border: 1px solid #8884; border-radius: 6px; padding: .5rem; margin: .3rem 0; opacity: .9; }
  ul { margin: .3rem 0; padding-left: 1.2rem; }
  form { display: grid; grid-template-columns: max-content max-content; gap: .4rem .8rem; align-items: center; }
  input { font: inherit; width: 7rem; padding: .15rem .3rem; }
  button { font: inherit; padding: .3rem .9rem; cursor: pointer; margin-top: .6rem; }
  .pill { border: 1px solid #8884; border-radius: 999px; padding: 0 .5rem; font-size: .8rem; }
  .huse { border-color: #5a7cffaa; color: #5a7cff; }
  #msg { margin-left: .8rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .5rem; }
  .card { border: 1px solid #8884; border-radius: 6px; padding: .5rem .7rem; }
  .card b { display: block; font-size: 1.2rem; }
  /* user requests (prompts) that opened the Flow's tasks */
  .requests { margin: .4rem 0 .2rem; border-left: 2px solid #5a7cff88; padding-left: .6rem; }
  .req { margin: .15rem 0; white-space: pre-wrap; word-break: break-word; }
  .req .reqnum { color: #5a7cff; font-weight: 600; margin-right: .4rem; }
  .req .reqtext { opacity: .9; }
  .node .request { opacity: .75; font-style: italic; }
  /* editable layer rows */
  .editlist { display: grid; gap: .35rem; margin: .3rem 0; }
  .erow { display: flex; gap: .4rem; align-items: center; flex-wrap: wrap; }
  .erow input { width: auto; }
  .erow input.desc { flex: 1 1 22rem; }
  .erow input.wt { width: 4.5rem; }
  .erow .cid { opacity: .6; min-width: 4rem; }
  .lockbtn { margin-left: .6rem; font-size: .8rem; padding: .1rem .6rem; }
  .rowdel { padding: .1rem .5rem; }
  .l-actions { margin-top: .5rem; display: flex; gap: .5rem; align-items: center; }
  .warn { color: #c0392b; font-size: .82rem; margin: .3rem 0; }
  .smsg { font-size: .85rem; }
</style>
</head>
<body>
<h1>harness-lens <span class="muted">— Flow / Task / Step · 3-Layer</span></h1>

<section id="status"><h2>Status</h2><div class="grid" id="status-grid"></div></section>

<section><h2>3-Layer 하네스 <span class="muted">(사람이 직접 편집)</span> <span id="layer-msg" class="smsg"></span></h2>
  <div id="layer1"></div>
  <div id="layer2"></div>
  <div id="layer3"></div>
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

// The 3-Layer harness is human-editable here (AHE auto-evolution still only touches Layer 3).
// `_layers` holds the working copy: add/delete reads the live inputs back into it before
// re-rendering so in-progress edits are never lost. Layer 1 stays behind an unlock toggle.
let _layers = null;
let _l1Unlocked = false;

async function loadLayers() {
  _layers = await (await fetch('api/layers')).json();
  renderLayer1(); renderLayer2(); renderLayer3();
}

async function saveLayer(path, body) {
  const msg = $('layer-msg');
  msg.textContent = '저장 중…'; msg.className = 'smsg muted';
  let res, data;
  try {
    const token = document.querySelector('meta[name=hl-token]').content;
    res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-HL-Token': token }, body: JSON.stringify(body) });
    data = await res.json();
  } catch (e) { msg.textContent = '오류: ' + e; msg.className = 'smsg bad'; return; }
  if (res.ok) {
    _l1Unlocked = false;              // re-lock Layer 1 after a successful write
    await loadLayers(); await loadStatus(); await loadFlows();
    msg.textContent = '저장됨 · 재강제 완료'; msg.className = 'smsg ok';
  } else {
    msg.textContent = '거부: ' + ((data && data.error) || res.status); msg.className = 'smsg bad';
  }
}

function rowDeleteButton(collect, assign, rerender) {
  const del = el('button', 'rowdel', '삭제'); del.type = 'button';
  del.addEventListener('click', () => {
    const row = del.closest('.erow');
    const vals = collect();
    vals.splice([...row.parentNode.children].indexOf(row), 1);
    assign(vals); rerender();
  });
  return del;
}

// -- Layer 1 (invariants, unlock-gated) -- //
function collectL1() {
  return [...$('layer1').querySelectorAll('.editlist input.desc')].map(i => i.value);
}
function renderLayer1() {
  const box = $('layer1'); box.replaceChildren();
  const h = el('h2'); h.append(document.createTextNode('Layer 1 — Invariants '));
  const lock = el('button', 'lockbtn'); lock.type = 'button';
  lock.textContent = _l1Unlocked ? '🔓 편집 중 — 다시 잠그기' : '🔒 잠금 — 클릭해 편집';
  lock.addEventListener('click', () => {
    if (_l1Unlocked) _layers.invariants = collectL1();  // preserve edits when re-locking
    _l1Unlocked = !_l1Unlocked; renderLayer1();
  });
  h.append(lock); box.append(h);
  if (!_l1Unlocked) {
    const ul = el('ul');
    if (!_layers.invariants.length) ul.append(el('li', 'muted', '(없음)'));
    _layers.invariants.forEach(r => ul.append(el('li', null, r)));
    box.append(ul); return;
  }
  box.append(el('p', 'warn', '⚠ Layer 1은 안전 불변식입니다. 저장 시 criteria.yaml이 백업되고 즉시 재강제됩니다.'));
  const list = el('div', 'editlist');
  _layers.invariants.forEach(r => {
    const row = el('div', 'erow');
    const inp = el('input', 'desc'); inp.type = 'text'; inp.value = r; inp.placeholder = '불변식 내용';
    row.append(inp, rowDeleteButton(collectL1, v => _layers.invariants = v, renderLayer1));
    list.append(row);
  });
  box.append(list);
  const actions = el('div', 'l-actions');
  const add = el('button', null, '+ 불변식 추가'); add.type = 'button';
  add.addEventListener('click', () => { _layers.invariants = collectL1(); _layers.invariants.push(''); renderLayer1(); });
  const save = el('button', null, '저장 + 재강제'); save.type = 'button';
  save.addEventListener('click', () => saveLayer('api/layer1', { invariants: collectL1() }));
  actions.append(add, save); box.append(actions);
}

// -- Layer 2 (domain criteria) -- //
function collectL2() {
  return [...$('layer2').querySelectorAll('.editlist .erow')].map(row => ({
    id: row.dataset.id || '',
    description: row.querySelector('input[data-field=description]').value,
    weight: Number(row.querySelector('input[data-field=weight]').value || 1),
  }));
}
function renderLayer2() {
  const box = $('layer2'); box.replaceChildren(el('h2', null, 'Layer 2 — Domain criteria'));
  const list = el('div', 'editlist');
  _layers.domain_criteria.forEach(d => {
    const row = el('div', 'erow'); row.dataset.id = d.id || '';
    row.append(el('span', 'cid', d.id || '(new)'));
    const desc = el('input', 'desc'); desc.type = 'text'; desc.value = d.description || ''; desc.placeholder = '기준 설명'; desc.dataset.field = 'description';
    const wt = el('input', 'wt'); wt.type = 'number'; wt.step = 'any'; wt.min = '0'; wt.title = 'weight'; wt.value = (d.weight != null ? d.weight : 1); wt.dataset.field = 'weight';
    row.append(desc, wt, rowDeleteButton(collectL2, v => _layers.domain_criteria = v, renderLayer2));
    list.append(row);
  });
  box.append(list);
  const actions = el('div', 'l-actions');
  const add = el('button', null, '+ 기준 추가'); add.type = 'button';
  add.addEventListener('click', () => { _layers.domain_criteria = collectL2(); _layers.domain_criteria.push({ id: '', description: '', weight: 1 }); renderLayer2(); });
  const save = el('button', null, '저장 + 재강제'); save.type = 'button';
  save.addEventListener('click', () => saveLayer('api/layer2', { domain_criteria: collectL2() }));
  actions.append(add, save); box.append(actions);
}

// -- Layer 3 (QA thresholds) -- //
function renderLayer3() {
  const box = $('layer3'); box.replaceChildren(el('h2', null, 'Layer 3 — QA thresholds (편집 가능)'));
  const form = el('form'); form.id = 'layer3-form';
  Object.entries(_layers.layer3).forEach(([k, val]) => {
    form.append(el('label', null, k));
    const inp = el('input'); inp.name = k; inp.value = val; inp.step = 'any'; inp.type = 'number'; form.append(inp);
  });
  box.append(form);
  const actions = el('div', 'l-actions');
  const save = el('button', null, '저장 + 재강제'); save.type = 'button';
  save.addEventListener('click', () => {
    const params = {};
    form.querySelectorAll('input').forEach(i => { if (i.value !== '') params[i.name] = Number(i.value); });
    saveLayer('api/layer3', params);
  });
  actions.append(save); box.append(actions);
}

// One-sided 0.5 cutoff matches the Judge: a step "passes" Layer 2 at/above it.
const L2_PASS = 0.5;
const clip = (s, n) => { s = (s || '').replace(/\\s+/g, ' ').trim(); return s.length > n ? s.slice(0, n) + '…' : s; };

function l1Badge(p) { return p === true ? el('span', 'ok verdict', 'L1✓') : p === false ? el('span', 'bad verdict', 'L1✗') : el('span', 'muted verdict', 'L1–'); }
function l2Badge(v) {
  if (v == null) return el('span', 'muted verdict', 'L2–');
  return el('span', (v >= L2_PASS ? 'ok' : 'bad') + ' verdict', 'L2 ' + v.toFixed(2));
}

function parseJSON(s) { try { return s ? JSON.parse(s) : null; } catch (e) { return null; } }

// User-authored harness components exercised at a step / across a Flow (skills, slash commands,
// instruction files, plugins, MCP). Recovered heuristically from tool name + I/O text.
const USE_LABEL = { invoke: '호출', mcp: 'mcp', skill: 'skill', command: 'cmd', plugin: 'plugin', instruction: '지시문' };
function usePill(u, suffix) { return el('span', 'pill huse', `${USE_LABEL[u.kind] || u.kind}: ${u.name}${suffix || ''}`); }

// Render the natural-language harness attribution stored per step (②-lite): which Layer-1
// invariants were checked / violated, and the Layer-2 Judge's per-criterion verdicts + reasons.
function harnessDetail(s) {
  const wrap = el('div', 'harness');
  let any = false;
  const d = parseJSON(s.layer1_detail);
  if (d) {
    const viol = d.violations || [], checked = d.checked || [];
    if (viol.length) {
      viol.forEach(v => { any = true; const r = el('div', 'hfail'); r.append(el('b', null, 'L1 위반'), document.createTextNode(`${v.rule} — ${v.detail}`)); wrap.append(r); });
    } else if (checked.length) {
      any = true; const r = el('div', 'hpass'); r.append(el('b', null, 'L1 통과'), document.createTextNode(`invariant ${checked.length}개 검사: ${checked.join(' · ')}`)); wrap.append(r);
    }
  }
  const vs = parseJSON(s.layer2_verdicts) || [];
  vs.forEach(v => { any = true; const r = el('div', v.passed ? 'hpass' : 'hfail'); r.append(el('b', null, `L2 [${v.criterion_id}] ${v.passed ? 'pass' : 'fail'}`), document.createTextNode(v.reason || '')); wrap.append(r); });
  return any ? wrap : null;
}

function stepNode(s, idx) {
  const twig = el('div', 'twig');
  const node = el('div', 'node');
  const flag = s.observed === false ? '?' : (s.success === false ? '⚠' : (s.success === true ? '✅' : '?'));
  node.append(el('span', null, `${flag} Step ${idx + 1}`), el('span', 'tool', s.tool_name || '?'), l1Badge(s.layer1_passed), l2Badge(s.layer2_score));
  if (s.latency_ms != null) node.append(el('span', 'muted verdict', `${s.latency_ms}ms`));
  if (s.retry_count) node.append(el('span', 'bad verdict', `retry ${s.retry_count}`));
  (s.harness_usage || []).forEach(u => node.append(usePill(u)));
  twig.append(node);
  if (s.input_summary) { const io = el('div', 'io'); io.append(el('b', null, 'in'), document.createTextNode(clip(s.input_summary, 200))); twig.append(io); }
  if (s.output_summary) { const io = el('div', 'io'); io.append(el('b', null, 'out'), document.createTextNode(clip(s.output_summary, 200))); twig.append(io); }
  const h = harnessDetail(s); if (h) twig.append(h);
  return twig;
}

function taskNode(t, i) {
  const fails = t.steps.filter(s => s.success === false).length;
  // A gap step has no observed outcome (success === null), so a partly-unobserved task
  // is "?" rather than a misleading ✅ — matching the CLI's treatment.
  const unobserved = t.steps.some(s => s.observed === false);
  const flag = fails ? '⚠' : (unobserved ? '?' : '✅');
  const det = el('details', 'twig'); det.open = fails > 0;  // auto-open tasks with failures
  const sum = el('summary');
  const node = el('div', 'node task');
  node.append(el('span', 'caret', det.open ? '▾' : '▸'),
              el('span', null, `${flag} Task ${i + 1}`),
              el('span', 'pill', t.category),
              el('span', 'muted verdict', `${t.steps.length} steps`));
  if (t.request) node.append(el('span', 'request', '“' + clip(t.request, 80) + '”'));
  sum.append(node); det.append(sum);
  det.addEventListener('toggle', () => { node.querySelector('.caret').textContent = det.open ? '▾' : '▸'; });
  const sub = el('div', 'tree');
  t.steps.forEach((s, k) => sub.append(stepNode(s, k)));
  det.append(sub);
  return det;
}

// The instruction file (CLAUDE.md/AGENTS.md) snapshotted at this Flow's start — the
// AHE-controllable source of the system prompt. Shows hash, size, managed-block presence, content.
function sysPromptNode(snap) {
  const det = el('details', 'sysprompt');
  const file = (snap.path || '').split('/').pop() || 'instruction';
  const sum = el('summary');
  sum.append(document.createTextNode(`System prompt (${file}, ${snap.scope}) · sha ${snap.sha256} · ${snap.line_count}줄 · `),
             el('span', snap.managed_block ? 'ok' : 'bad', '3-Layer 블록 ' + (snap.managed_block ? '✓ 포함됨' : '✗ 없음')));
  det.append(sum);
  const pre = el('pre'); pre.textContent = snap.content || '(내용 없음)'; det.append(pre);
  return det;
}

// Declared-vs-fired: for a Skill/command exercised in this Flow, show the prompts its own
// prompt file references and whether each one actually fired in the run. Bridges "what the
// Skill points at" (static parse) with "what the run touched" (trajectory attribution).
const REF_LABEL = { invoke: '호출', mcp: 'mcp', skill: 'skill', command: 'cmd', plugin: 'plugin', instruction: '지시문', import: 'import', script: 'script' };
function refGraphNode(g) {
  const det = el('details', 'sysprompt');
  const fired = g.references.filter(r => r.fired).length;
  const sum = el('summary');
  sum.append(document.createTextNode(`참조 그래프 · ${REF_LABEL[g.kind] || g.kind} ${g.name} → 선언 ${g.references.length}개 중 `),
             el('span', fired ? 'ok' : 'muted', `${fired}개 발동`));
  det.append(sum);
  const body = el('div', 'layers');
  g.references.forEach(r => {
    const cls = r.fired ? 'pill huse' : 'pill';
    body.append(el('span', cls, `${r.fired ? '● 발동' : '○ 미발동'} · ${REF_LABEL[r.kind] || r.kind}: ${r.name}`));
  });
  det.append(body);
  return det;
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
    // The user requests (prompts) sent in this Flow — what was actually asked.
    if ((f.requests || []).length) {
      const rq = el('div', 'requests');
      f.requests.forEach((r, i) => {
        const line = el('div', 'req');
        const t = r.text || '';
        line.append(el('span', 'reqnum', `요청 ${i + 1}`),
                    el('span', 'reqtext', t.length > 600 ? t.slice(0, 600) + '…' : t));
        rq.append(line);
      });
      card.append(rq);
    }
    if ((f.harness_used || []).length) {
      const hu = el('div', 'layers');
      hu.append(el('span', 'muted', '하네스 발동:'));
      f.harness_used.forEach(u => hu.append(usePill(u, ` ×${u.count}`)));
      card.append(hu);
    }
    (f.reference_graph || []).forEach(g => card.append(refGraphNode(g)));
    (f.system_prompt || []).forEach(snap => card.append(sysPromptNode(snap)));
    const tree = el('div', 'tree');
    (f.tasks || []).forEach((t, i) => tree.append(taskNode(t, i)));
    card.append(tree);
    box.append(card);
  });
}

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

    # Write routes. Layer 3 is the AHE-evolvable layer; Layer 1/2 here are *human owner* edits
    # (the GUI's explicit unlock), distinct from AHE auto-evolution which stays Layer-3 only.
    _WRITE_ROUTES = {
        "/api/layer3": lambda s, p: {"layer3": s.update_layer3(p)},
        "/api/layer1": lambda s, p: s.update_invariants(p.get("invariants", [])),
        "/api/layer2": lambda s, p: s.update_domain_criteria(p.get("domain_criteria", [])),
    }

    def do_POST(self) -> None:
        if not self._host_ok() or not self._csrf_ok():
            self._json(403, {"error": "forbidden"})
            return
        handler = self._WRITE_ROUTES.get(urlparse(self.path).path)
        if handler is None:
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
            payload = self._with_service(lambda s: handler(s, params))
        except ComponentError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, payload)

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
