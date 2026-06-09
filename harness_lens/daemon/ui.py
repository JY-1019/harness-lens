"""The daemon's live GUI — a single dependency-free vanilla-JS page served at ``/ui``.

This is the stdlib-first GUI (no React/Vite build): the daemon serves one HTML document that
loads a REST snapshot, opens the ``/ws`` patch stream, and renders the live Flow/Task/Step tree
with the approval/mode controls. It speaks the same REST + WebSocket protocol a later React/React
Flow build would, so migrating the frontend later is a drop-in replacement, not a protocol change.

Interactions implemented: live tree (node colours by status, pulse on running/pending), multi-
session stacking, step detail panel with lazy-loaded full output, mode toggle, pending-approval
badge + approval cards with a timeout countdown, and approve/deny (disabled while the socket is
down, so no optimistic approval is sent over a dead connection).
"""

from __future__ import annotations

_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>harness-lens · live</title>
<style>
  :root { color-scheme: light dark; --line:#8886; --blue:#3b82f6; --green:#22a35a; --red:#dc2626; --amber:#e0a200; }
  * { box-sizing: border-box; }
  body { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin:0; height:100vh; display:flex; flex-direction:column; }
  header { display:flex; align-items:center; gap:1rem; padding:.5rem .9rem; border-bottom:1px solid var(--line); }
  header h1 { font-size:1rem; margin:0; }
  .grow { flex:1; }
  .badge { border:1px solid var(--line); border-radius:999px; padding:.1rem .55rem; }
  .badge.alert { background:var(--amber); color:#000; border-color:var(--amber); cursor:pointer; }
  #conn.bad { color:var(--red); } #conn.ok { color:var(--green); }
  button { font:inherit; padding:.25rem .7rem; border:1px solid var(--line); border-radius:6px; background:transparent; cursor:pointer; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  #mode { font-weight:600; }
  main { flex:1; display:grid; grid-template-columns: 15rem 1fr 22rem; min-height:0; }
  aside, #detail { overflow:auto; padding:.6rem; }
  aside { border-right:1px solid var(--line); }
  #canvas { overflow:auto; padding:.7rem; display:grid; gap:.8rem; align-content:start;
            grid-template-columns: repeat(auto-fill, minmax(23rem, 1fr)); }
  #canvas .empty { grid-column:1/-1; }
  #detail { border-left:1px solid var(--line); }
  h2 { font-size:.8rem; opacity:.7; margin:.2rem 0 .5rem; text-transform:uppercase; letter-spacing:.04em; }
  .sess { display:flex; gap:.4rem; align-items:center; padding:.2rem; border-radius:5px; cursor:pointer; }
  .sess:hover { background:#8881; }
  .dot { width:.6rem; height:.6rem; border-radius:50%; display:inline-block; }
  .flowcard { border:1px solid var(--line); border-top:3px solid var(--line); border-radius:10px;
              padding:.55rem .8rem .7rem; background:#8881; box-shadow:0 1px 4px #0003;
              display:flex; flex-direction:column; min-width:0; }
  .flowhead { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
              margin:-.05rem -.1rem .45rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
  .flowhead b { font-size:.95rem; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .tasknode { margin:.3rem 0 .3rem .4rem; }
  .taskhdr { display:inline-flex; gap:.4rem; align-items:center; border:1px solid var(--line); border-radius:6px; padding:.15rem .5rem; }
  .prompt { display:block; margin:.25rem 0 .15rem .2rem; padding:.3rem .55rem; border-left:2px solid var(--blue);
            background:#8881; border-radius:4px; white-space:pre-wrap; word-break:break-word;
            max-height:9rem; overflow:auto; font-size:.82rem; opacity:.92; }
  .children { border-left:1px solid var(--line); margin-left:.7rem; padding-left:.6rem; }
  .step { display:inline-flex; gap:.45rem; align-items:center; border:1px solid var(--line); border-radius:6px;
          padding:.12rem .5rem; margin:.2rem 0; cursor:pointer; }
  .step.sel { outline:2px solid var(--blue); }
  .st-running { border-color:var(--blue); animation:pulse 1.1s ease-in-out infinite; }
  .st-ok { border-color:var(--green); } .st-failed { border-color:var(--red); color:var(--red); }
  .st-denied { opacity:.55; text-decoration:line-through; }
  .st-pending_approval { border-color:var(--amber); background:var(--amber); color:#000; animation:pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.55} }
  .chip { font-size:.72rem; border:1px solid var(--line); border-radius:4px; padding:0 .3rem; opacity:.85; }
  .muted { opacity:.6; } .mono { white-space:pre-wrap; word-break:break-word; }
  .appr { border:1px solid var(--amber); border-radius:8px; padding:.5rem; margin-bottom:.6rem; }
  .bar { height:5px; background:#8883; border-radius:3px; overflow:hidden; margin:.4rem 0; }
  .bar > i { display:block; height:100%; background:var(--amber); }
  .row { display:flex; gap:.4rem; }
  pre { white-space:pre-wrap; word-break:break-word; background:#8881; border-radius:6px; padding:.4rem; max-height:18rem; overflow:auto; }
  input[type=text] { font:inherit; width:100%; padding:.2rem; }
  /* scope editor modal */
  .modal { position:fixed; inset:0; background:#0008; display:flex; align-items:center; justify-content:center; z-index:50; }
  .modalbox { background:Canvas; color:CanvasText; border:1px solid var(--line); border-radius:10px;
              width:min(48rem,94vw); max-height:88vh; display:flex; flex-direction:column; box-shadow:0 6px 30px #0007; }
  .modalhead, .modalfoot { display:flex; align-items:center; gap:.5rem; padding:.55rem .8rem; }
  .modalhead { border-bottom:1px solid var(--line); } .modalfoot { border-top:1px solid var(--line); }
  #scopeList { overflow:auto; padding:.6rem .8rem; display:flex; flex-direction:column; gap:.7rem; }
  #scopeList:empty::after { content:"스코프가 없습니다. '+ 추가'로 만드세요."; opacity:.6; }
  .scoperow { border:1px solid var(--line); border-radius:8px; padding:.5rem .6rem; display:grid; gap:.4rem; }
  .scoperow .line { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }
  .scoperow label { font-size:.74rem; opacity:.65; }
  .scoperow input, .scoperow select, .scoperow textarea { font:inherit; padding:.2rem .35rem; border:1px solid var(--line);
              border-radius:5px; background:transparent; color:inherit; }
  .scoperow .nm { flex:1; min-width:7rem; } .scoperow .mv { flex:1; min-width:11rem; } .scoperow .l3 { width:5rem; }
  .scoperow textarea { width:100%; min-height:2.2rem; resize:vertical; }
  .scoperow .del { color:var(--red); border:1px solid var(--red); border-radius:5px; padding:.1rem .5rem; margin-left:auto; }
</style>
</head>
<body>
<header>
  <h1>harness-lens <span class="muted">live</span></h1>
  <button id="mode" title="모드 전환">mode: …</button>
  <button id="scopes" title="프로젝트/세션별 정책 스코프">scopes</button>
  <span class="grow"></span>
  <span id="pending" class="badge" style="display:none"></span>
  <span id="conn" class="badge">●</span>
</header>
<main>
  <aside><h2>세션</h2><div id="sessions"></div></aside>
  <div id="canvas"></div>
  <div id="detail"><h2>상세</h2><div id="detail-body" class="muted">노드를 선택하세요.</div></div>
</main>
<div id="scopeModal" class="modal" style="display:none">
  <div class="modalbox">
    <div class="modalhead"><b>정책 스코프</b><span class="muted">프로젝트(cwd)/세션별로 전역 base 위에 덮어씀</span>
      <span class="grow"></span><button id="scopeAdd">+ 추가</button><button id="scopeClose">✕</button></div>
    <div id="scopeList"></div>
    <div class="modalfoot"><span id="scopeMsg" class="muted"></span><span class="grow"></span><button id="scopeSave">저장</button></div>
  </div>
</div>
<script>
const TOKEN = "__HL_TOKEN__";
const H = { "X-HL-Token": TOKEN, "Content-Type": "application/json" };
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const api = (p, opt={}) => fetch(p, { ...opt, headers: { ...H, ...(opt.headers||{}) } });

const state = { flows:{}, tasks:{}, steps:{}, approvals:{}, selected:new Set(), sel:null, mode:"observe", connected:false, snapRev:0 };
const SRC_ICON = { claude_code:"🟧", codex:"🟦" };

// ---- snapshot load ----
async function loadSnapshot() {
  const st = await (await api("/api/status")).json();
  state.mode = st.mode; state.snapRev = st.rev || 0; renderMode();
  const flows = await (await api("/api/flows?limit=50")).json();
  for (const f of flows) state.flows[f.flow_id] = f;
  // auto-select running flows
  flows.filter(f => f.status === "running").forEach(f => state.selected.add(f.flow_id));
  if (!state.selected.size && flows[0]) state.selected.add(flows[0].flow_id);
  for (const id of state.selected) await loadTree(id);
  const appr = await (await api("/api/approvals")).json();
  appr.forEach(a => state.approvals[a.approval_id] = { ...a, step_id:a.step_id });
  renderSessions(); renderCanvas(); renderPending();
}
async function loadTree(flowId) {
  const tree = await (await api("/api/flows/" + flowId + "/tree")).json();
  if (tree.detail) return;
  state.flows[flowId] = { ...state.flows[flowId], ...flowOnly(tree) };
  (function walk(tasks){ for (const t of tasks||[]) { state.tasks[t.task_id] = taskOnly(t);
    for (const s of t.steps||[]) state.steps[s.step_id] = s; walk(t.children); } })(tree.tasks);
}
const flowOnly = t => { const { tasks, ...f } = t; return f; };
const taskOnly = t => { const { steps, children, ...x } = t; return x; };

// ---- websocket patch stream ----
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => { state.connected = true; renderConn(); };
  ws.onclose = () => { state.connected = false; renderConn(); renderCanvas(); setTimeout(connect, 1500); };
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if ((m.rev||0) <= state.snapRev && m.op !== "hello") return; applyPatch(m); };
}
function applyPatch(m) {
  if (m.op === "upsert") {
    if (m.entity === "flow") state.flows[m.data.flow_id] = { ...state.flows[m.data.flow_id], ...m.data };
    if (m.entity === "task") state.tasks[m.data.task_id] = m.data;
    if (m.entity === "step") state.steps[m.data.step_id] = { ...state.steps[m.data.step_id], ...m.data };
    if (m.entity === "flow" && !(m.data.flow_id in seenSidebar)) renderSessions();
    scheduleRender();
  } else if (m.op === "approval") {
    state.approvals[m.data.approval_id] = m.data; renderPending(); scheduleRender();
  } else if (m.op === "mode_changed") {
    state.mode = m.data.mode; renderMode();
  }
}
let raf = 0; const seenSidebar = {};
function scheduleRender() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; renderCanvas(); renderPending(); }); }

// ---- rendering ----
function renderMode() {
  const b = $("#mode"); b.textContent = "mode: " + state.mode;
  b.style.background = state.mode === "enforce" ? "var(--amber)" : "transparent";
  b.style.color = state.mode === "enforce" ? "#000" : "";
}
function renderConn() { const c = $("#conn"); c.textContent = state.connected ? "● 연결됨" : "● 끊김"; c.className = "badge " + (state.connected ? "ok" : "bad"); }
function statusColor(s) { return ({running:"var(--blue)",completed:"var(--green)",failed:"var(--red)",aborted:"#888"})[s] || "#888"; }

function renderSessions() {
  const box = $("#sessions"); box.replaceChildren();
  const flows = Object.values(state.flows).sort((a,b)=>(b.started_at||0)-(a.started_at||0));
  for (const f of flows) {
    seenSidebar[f.flow_id] = 1;
    const row = el("label", "sess");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = state.selected.has(f.flow_id);
    cb.onchange = async () => { if (cb.checked) { state.selected.add(f.flow_id); await loadTree(f.flow_id); } else state.selected.delete(f.flow_id); renderCanvas(); };
    const dot = el("span", "dot"); dot.style.background = statusColor(f.status);
    row.append(cb, dot, el("span", null, (SRC_ICON[f.source]||"") + " " + (f.title || f.flow_id).slice(0,22)));
    box.append(row);
  }
}

function renderCanvas() {
  const c = $("#canvas"); c.replaceChildren();
  const ids = [...state.selected];
  if (!ids.length) { c.append(el("p","muted empty","세션을 선택하세요.")); return; }
  for (const id of ids) {
    const f = state.flows[id]; if (!f) continue;
    const card = el("div","flowcard");
    card.style.borderTopColor = statusColor(f.status);  // status accent across the card top
    const head = el("div","flowhead");
    const dot = el("span","dot"); dot.style.background = statusColor(f.status);
    const title = el("b",null,(SRC_ICON[f.source]||"")+" "+(f.title||id).slice(0,120));
    if (f.title) title.title = f.title;  // full label on hover (title ellipsizes to one line)
    head.append(dot, title,
      el("span","chip",f.status), el("span","chip",(f.total_tokens||0).toLocaleString()+" tok"),
      el("span","chip","mode:"+(f.mode||"?")));
    card.append(head);
    const roots = Object.values(state.tasks).filter(t => t.flow_id===id && !t.parent_task_id)
      .sort((a,b)=>a.seq-b.seq);
    for (const t of roots) card.append(renderTask(t));
    c.append(card);
  }
}
function renderTask(t) {
  const wrap = el("div","tasknode");
  const hdr = el("div","taskhdr");
  const isSub = t.kind==="subagent";
  const label = el("span",null,isSub ? ("🤖 "+(t.agent_name||"subagent")) : "● "+(t.title||"turn").slice(0,40));
  if (!isSub && t.title) label.title = t.title;  // full prompt on hover
  hdr.append(label);
  if (t.retry_count>0) hdr.append(el("span","chip","⟳"+t.retry_count));
  hdr.append(el("span","chip muted",t.status));
  wrap.append(hdr);
  // The full user prompt that opened this turn — shown verbatim, not just as a truncated title.
  if (!isSub && t.title) { const p = el("div","prompt"); p.textContent = t.title; wrap.append(p); }
  const kids = el("div","children");
  Object.values(state.steps).filter(s => s.task_id===t.task_id).sort((a,b)=>a.seq-b.seq)
    .forEach(s => kids.append(renderStep(s)));
  Object.values(state.tasks).filter(x => x.parent_task_id===t.task_id).sort((a,b)=>a.seq-b.seq)
    .forEach(x => kids.append(renderTask(x)));
  wrap.append(kids);
  return wrap;
}
function renderStep(s) {
  const n = el("div","step st-"+s.status + (state.sel===s.step_id?" sel":""));
  n.append(el("span",null,s.tool_name||"?"));
  if (s.duration_ms!=null) n.append(el("span","chip",s.duration_ms+"ms"));
  if (s.tokens!=null) n.append(el("span","chip",s.tokens+"t"));
  if (s.judge_score!=null) n.append(el("span","chip","L2 "+Number(s.judge_score).toFixed(2)));
  if (s.decision_layer!=null) n.append(el("span","chip","L"+s.decision_layer));
  n.onclick = () => { state.sel = s.step_id; showDetail(s.step_id); renderCanvas(); };
  if (s.status === "pending_approval") n.append(renderApprovalInline(s));
  return n;
}
function renderApprovalInline(s) {
  const appr = Object.values(state.approvals).find(a => a.step_id===s.step_id && !a.resolved_at);
  if (!appr) return el("span","chip","대기");
  const card = el("span","chip"); card.textContent = "승인대기"; return card;
}

// ---- detail panel ----
async function showDetail(stepId) {
  const body = $("#detail-body"); body.replaceChildren(el("p","muted","불러오는 중…"));
  const s = await (await api("/api/steps/"+stepId)).json();
  body.replaceChildren();
  body.append(el("div",null, el("b",null,s.tool_name||"?")));
  const meta = el("div","muted"); meta.textContent = `status=${s.status}` + (s.decision?` · decision=${s.decision} (L${s.decision_layer})`:"");
  body.append(meta);
  if (s.decision_reason) body.append(field("결정 사유", s.decision_reason));
  if (s.tool_input) body.append(field("input", s.tool_input));
  if (s.tool_output) body.append(field("output", s.tool_output));
  if (s.judge_reason) body.append(field("judge", s.judge_reason));
  const appr = Object.values(state.approvals).find(a => a.step_id===stepId && !a.resolved_at);
  if (appr) body.append(approvalCard(appr, s));
}
function field(label, text) { const d = el("div"); d.append(el("h2",null,label)); const p = el("pre"); p.textContent = typeof text==="string"?text:JSON.stringify(text,null,2); d.append(p); return d; }

// ---- approvals ----
function approvalCard(appr, step) {
  const card = el("div","appr");
  card.append(el("div",null,"⚠ 승인 대기 — " + (appr.reason||"")));
  const bar = el("div","bar"); const fill = el("i"); bar.append(fill); card.append(bar);
  if (appr.timeout_at) {
    const total = Math.max(1, appr.timeout_at - (appr._t0 || (appr._t0 = Date.now()/1000)));
    const tick = () => { const left = appr.timeout_at - Date.now()/1000; fill.style.width = Math.max(0, Math.min(100, left/total*100))+"%";
      if (left <= 0 || appr.resolved_at) clearInterval(iv); };
    const iv = setInterval(tick, 200); tick();
  }
  const row = el("div","row");
  const yes = el("button",null,"승인"); const no = el("button",null,"거부");
  yes.disabled = no.disabled = !state.connected;  // never resolve over a dead socket
  const reason = el("input"); reason.type = "text"; reason.placeholder = "거부 사유(선택)";
  yes.onclick = () => resolveApproval(appr.approval_id, "approved");
  no.onclick = () => resolveApproval(appr.approval_id, "denied", reason.value);
  row.append(yes, no); card.append(row, reason);
  return card;
}
async function resolveApproval(id, resolution, reason) {
  await api("/api/approvals/"+id, { method:"POST", body: JSON.stringify({ resolution, reason: reason||null }) });
  const a = state.approvals[id]; if (a) a.resolved_at = Date.now()/1000;
  renderPending(); if (state.sel) showDetail(state.sel);
}
function renderPending() {
  const open = Object.values(state.approvals).filter(a => !a.resolved_at);
  const b = $("#pending");
  if (!open.length) { b.style.display = "none"; return; }
  b.style.display = ""; b.className = "badge alert"; b.textContent = "승인 대기 " + open.length;
  b.onclick = () => { const first = open[0]; if (first) { state.sel = first.step_id; showDetail(first.step_id);
    // ensure the flow is selected/visible
    const s = state.steps[first.step_id]; if (s) { state.selected.add(s.flow_id); renderCanvas(); } } };
}

// ---- mode toggle ----
$("#mode").onclick = async () => {
  const next = state.mode === "enforce" ? "observe" : "enforce";
  const r = await api("/api/mode", { method:"POST", body: JSON.stringify({ mode: next }) });
  if (r.ok) { state.mode = (await r.json()).mode; renderMode(); }
};

// ---- scope editor ----
const L3KEYS = [["retry_threshold","retry"],["latency_multiplier","lat"],
                ["failure_count_trigger","fail"],["quality_threshold","qual"]];
function opt(value, label) { const o = el("option", null, label); o.value = value; return o; }
function scopeRow(s) {
  s = s || { name:"", match:{}, mode:"", layer3:{}, add_invariants:[] };
  const row = el("div","scoperow");
  const l1 = el("div","line");
  const nm = el("input"); nm.className="nm"; nm.placeholder="이름"; nm.value = s.name||""; nm.dataset.f="name";
  const mt = el("select"); mt.dataset.f="matchType";
  mt.append(opt("cwd_prefix","cwd 경로"), opt("session_id","세션 ID"));
  mt.value = (s.match && s.match.session_id) ? "session_id" : "cwd_prefix";
  const mv = el("input"); mv.className="mv"; mv.dataset.f="matchValue";
  mv.placeholder = "/path/to/project 또는 세션 ID";
  mv.value = (s.match && (s.match.cwd_prefix || s.match.session_id)) || "";
  const del = el("button","del","삭제"); del.onclick = () => row.remove();
  l1.append(nm, el("label",null,"match"), mt, mv, del);
  const l2 = el("div","line");
  const md = el("select"); md.dataset.f="mode";
  md.append(opt("","mode: 상속"), opt("observe","observe"), opt("enforce","enforce"));
  md.value = s.mode || "";
  l2.append(md, el("label",null,"L3"));
  for (const [key,lab] of L3KEYS) {
    const i = el("input"); i.className="l3"; i.type="number"; i.step="any"; i.placeholder=lab; i.dataset.l3=key;
    if (s.layer3 && s.layer3[key] != null) i.value = s.layer3[key];
    l2.append(i);
  }
  const l3 = el("div","line");
  const ta = el("textarea"); ta.dataset.f="invariants"; ta.placeholder="추가 invariant (한 줄에 하나, 전역 규칙 위에 가산)";
  ta.value = (s.add_invariants||[]).join("\n");
  l3.append(ta);
  row.append(l1, l2, l3);
  return row;
}
function collectScopes() {
  return [...document.querySelectorAll("#scopeList .scoperow")].map(row => {
    const get = f => row.querySelector('[data-f="'+f+'"]');
    const matchValue = get("matchValue").value.trim();
    const match = {}; if (matchValue) match[get("matchType").value] = matchValue;
    const layer3 = {};
    row.querySelectorAll("[data-l3]").forEach(i => { if (i.value !== "") layer3[i.dataset.l3] = Number(i.value); });
    const add_invariants = get("invariants").value.split("\n").map(x=>x.trim()).filter(Boolean);
    const out = { name:get("name").value.trim(), match, layer3, add_invariants };
    if (get("mode").value) out.mode = get("mode").value;
    return out;
  });
}
async function openScopes() {
  const data = await (await api("/api/scopes")).json();
  const list = $("#scopeList"); list.replaceChildren();
  (data.scopes||[]).forEach(s => list.append(scopeRow(s)));
  $("#scopeMsg").textContent = "";
  $("#scopeModal").style.display = "flex";
}
$("#scopes").onclick = openScopes;
$("#scopeClose").onclick = () => { $("#scopeModal").style.display = "none"; };
$("#scopeModal").onclick = e => { if (e.target.id === "scopeModal") $("#scopeModal").style.display = "none"; };
$("#scopeAdd").onclick = () => { $("#scopeList").append(scopeRow()); };
$("#scopeSave").onclick = async () => {
  const r = await api("/api/scopes", { method:"POST", body: JSON.stringify({ scopes: collectScopes() }) });
  if (r.ok) {
    const d = await r.json();
    $("#scopeMsg").textContent = "저장됨 — " + d.scopes.length + "개 스코프 적용 (즉시 반영)";
    loadSnapshot();  // mode chips may change for affected flows
  } else {
    $("#scopeMsg").textContent = "저장 실패 (" + r.status + ")";
  }
};

renderConn(); loadSnapshot().then(connect);
</script>
</body>
</html>
"""


def render_page(token: str) -> str:
    # Token is embedded for the page's own same-origin API/WS calls (loopback single-user model).
    return _PAGE.replace("__HL_TOKEN__", token)
