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
  main { flex:1; display:grid; grid-template-columns: 15rem 1fr 24rem; min-height:0; }
  aside, #detail { overflow:auto; padding:.6rem; }
  aside { border-right:1px solid var(--line); }
  #canvas { overflow:auto; padding:.9rem; }
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
  .flowhead b { font-size:.95rem; flex:1 1 12rem; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .flowtools { display:flex; gap:.35rem; align-items:center; margin-left:auto; }
  .iconbtn { width:1.9rem; height:1.9rem; display:inline-grid; place-items:center; padding:0; border-radius:6px; }
  .chip.scope { border-color:var(--blue); color:var(--blue); }
  .chip.project { max-width:15rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .flowmeta { display:grid; gap:.25rem; margin:.15rem 0 .45rem; }
  .flowmeta .line { display:flex; gap:.35rem; flex-wrap:wrap; align-items:center; min-width:0; }
  .flowchart { display:grid; gap:.2rem; padding:.15rem 0 .1rem; }
  .tasknode { position:relative; margin:.35rem 0 .35rem 1rem; padding-left:.8rem; }
  .tasknode::before { content:""; position:absolute; left:0; top:1rem; bottom:-.55rem; border-left:1px solid var(--line); }
  .tasknode::after { content:""; position:absolute; left:0; top:1rem; width:.55rem; border-top:1px solid var(--line); }
  .flowchart > .tasknode { margin-left:.1rem; }
  .flowchart > .tasknode::before { top:1.15rem; }
  .taskhdr { display:inline-flex; gap:.4rem; align-items:center; border:1px solid var(--line); border-radius:6px; padding:.15rem .5rem; background:Canvas; }
  .prompt { display:block; margin:.25rem 0 .15rem .2rem; padding:.3rem .55rem; border-left:2px solid var(--blue);
            background:#8881; border-radius:4px; white-space:pre-wrap; word-break:break-word;
            max-height:9rem; overflow:auto; font-size:.82rem; opacity:.92; }
  .children { display:grid; gap:.1rem; margin-left:.7rem; padding-left:.4rem; }
  .step { position:relative; display:flex; flex-direction:column; align-items:flex-start; width:max-content; max-width:100%; gap:.18rem;
          border:1px solid var(--line); border-radius:6px;
          padding:.16rem .5rem; margin:.2rem 0 .2rem .65rem; cursor:pointer; background:Canvas; }
  .step::before { content:""; position:absolute; left:-.65rem; top:1rem; width:.65rem; border-top:1px solid var(--line); }
  .step-main { display:flex; gap:.45rem; align-items:center; max-width:100%; }
  .step-usage { display:flex; gap:.3rem; flex-wrap:wrap; margin-left:1.35rem; }
  .step.sel { outline:2px solid var(--blue); }
  .st-running { border-color:var(--blue); animation:pulse 1.1s ease-in-out infinite; }
  .st-ok { border-color:var(--green); } .st-failed { border-color:var(--red); color:var(--red); }
  .st-denied { opacity:.55; text-decoration:line-through; }
  .st-pending_approval { border-color:var(--amber); background:var(--amber); color:#000; animation:pulse 1s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.55} }
  .tname { white-space:nowrap; }
  /* WHY a step was caught — inline reason under a denied/escalated step */
  .step-verdict { margin-left:1.35rem; font-size:.78rem; white-space:pre-wrap; word-break:break-word; cursor:pointer; }
  .step-verdict.bad { color:var(--red); }
  .step-verdict.warn { color:var(--amber); }
  .step-verdict.ok { color:var(--green); opacity:.8; }
  .step-verdict .vmark { font-weight:700; }
  .cmdsum { flex:0 1 auto; min-width:0; max-width:34rem; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; opacity:.62; font-size:.82rem; }
  .step.sel .cmdsum { opacity:.85; }
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
  .scopedc { display:grid; gap:.35rem; width:100%; }
  .scopedcrow { display:grid; grid-template-columns: 4.8rem 1fr 4.6rem auto; gap:.35rem; align-items:center; width:100%; }
  .scopedcrow input { min-width:0; }
  .ok { color:var(--green); }
  /* request banner — the user's ask, made the headline of each turn */
  .reqicon { opacity:.9; }
  .prompt .reqicon { font-weight:600; }
  /* harness (3-Layer) editor */
  .hl-badge { font-size:.72rem; border:1px solid var(--blue); color:var(--blue); border-radius:999px; padding:.05rem .55rem; }
  .hl-layer { border:1px solid var(--line); border-radius:8px; padding:.5rem .65rem; margin-bottom:.7rem; }
  .hl-layer h3 { font-size:.84rem; margin:.1rem 0 .2rem; display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; }
  .hl-help { font-size:.75rem; opacity:.68; margin:.05rem 0 .45rem; }
  .hl-item { display:flex; gap:.4rem; align-items:center; margin:.25rem 0; flex-wrap:wrap; }
  .hl-item input.t { flex:1; min-width:12rem; padding:.2rem .35rem; border:1px solid var(--line); border-radius:5px; background:transparent; color:inherit; }
  .hl-item input.w { width:4.4rem; padding:.2rem .35rem; border:1px solid var(--line); border-radius:5px; background:transparent; color:inherit; }
  .hl-item .cid { opacity:.55; min-width:4rem; font-size:.74rem; }
  .hl-del { color:var(--red); border:1px solid var(--red); border-radius:5px; padding:.05rem .5rem; }
  .hl-lock { font-size:.74rem; margin-left:auto; }
  .hl-actions { display:flex; gap:.5rem; margin-top:.45rem; }
  .hl-save { border:1px solid var(--green); color:var(--green); border-radius:6px; padding:.2rem .7rem; }
  .scopecards { display:grid; gap:.4rem; }
  .scopecard { border:1px solid var(--line); border-radius:7px; padding:.45rem .55rem; display:grid; gap:.25rem; }
  .scopecard b { font-size:.82rem; }
  .warn { color:var(--red); font-size:.76rem; margin:.2rem 0; }
  /* sidebar: each session = a [source] folder accordion → dropdown of the user's requests */
  #sessions { display:flex; flex-direction:column; }
  /* project group (folder) — sessions stack beneath it */
  .projgroup { border-bottom:1px solid var(--line); padding:.1rem 0 .25rem; }
  .projhead { display:flex; gap:.35rem; align-items:center; padding:.3rem .25rem; border-radius:5px; cursor:pointer; }
  .projhead:hover { background:#8881; }
  .projicon { font-size:.82rem; }
  .pname { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; font-size:.86rem; }
  .cnt { font-size:.66rem; opacity:.6; white-space:nowrap; }
  .modectl { display:inline-flex; }
  .modesel { font-size:.66rem; border-radius:4px; padding:.05rem .2rem; border:1px solid var(--line); background:transparent; color:inherit; cursor:pointer; }
  .modesel.enf { border-color:var(--amber); color:var(--amber); font-weight:700; }
  .modesel.obs { opacity:.75; }
  .projsessions { margin-left:.5rem; padding-left:.35rem; border-left:1px solid var(--line); }
  .sessitem { padding:.05rem 0 .15rem; }
  .sesshead { display:flex; gap:.4rem; align-items:center; padding:.22rem .2rem; border-radius:5px; cursor:pointer; }
  .sesshead:hover { background:#8881; } .sesshead.cur { background:#8882; }
  .caret { width:.85rem; opacity:.65; text-align:center; }
  .srctag { font-size:.66rem; border-radius:4px; padding:.03rem .32rem; color:#fff; font-weight:700; letter-spacing:.02em; white-space:nowrap; }
  .srctag.claude { background:#d97706; } .srctag.codex { background:#2563eb; }
  .sname { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .smeta { font-size:.74rem; opacity:.85; }
  .sesssub { font-size:.71rem; margin:0 0 .1rem 1.3rem; }
  .reqlist { display:flex; flex-direction:column; gap:.08rem; margin:.25rem 0 .15rem .9rem; padding-left:.35rem; border-left:1px solid var(--line); }
  .reqitem { display:flex; gap:.35rem; align-items:center; padding:.18rem .35rem; border-radius:5px; cursor:pointer; }
  .reqitem:hover { background:#8881; } .reqitem.sel { background:var(--blue); color:#fff; }
  .reqitem.sel .chip { border-color:#fff9; color:#fff; }
  .rtext { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .reqempty { padding:.2rem .4rem; }
  /* request view — the canvas focuses on one selected request */
  .reqview { max-width:62rem; }
  .reqhead { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin-bottom:.3rem; }
  .reqask { padding:.4rem .6rem; border-left:3px solid var(--blue); background:#8881; border-radius:5px;
            white-space:pre-wrap; word-break:break-word; max-height:11rem; overflow:auto; margin:.2rem 0 .2rem; }
  /* (A) service-harness usage chip on a step; (B) 3-Layer decision chip */
  .uchip { border-color:var(--green); color:var(--green); opacity:1; cursor:pointer; }
  .uchip:hover { background:#22a35a22; }
  .catsec { margin:.1rem 0 .45rem; }
  .cathead { font-size:.78rem; font-weight:600; opacity:.82; margin:.35rem 0 .1rem; padding:.05rem .4rem; border-left:3px solid var(--blue); }
  .trajhead { display:flex; align-items:center; gap:.5rem; }
  .hp-title { font-weight:600; }
  .hp-title:hover { text-decoration:underline; }
  .prov { font-size:.66rem; border-radius:4px; padding:.02rem .3rem; border:1px solid var(--line); white-space:nowrap; }
  .prov.proj { color:var(--blue); border-color:var(--blue); }
  .prov.glob { opacity:.55; }
  /* per-step 3-Layer badges — click to inspect that layer */
  .lyrbadges { display:inline-flex; gap:.18rem; }
  .lyr { font-size:.6rem; line-height:1.5; border:1px solid var(--line); border-radius:3px; padding:0 .25rem; opacity:.38; cursor:pointer; }
  .lyr:hover { opacity:1; }
  .lyr.acted { opacity:1; font-weight:700; }
  .lyr.bad { color:var(--red); border-color:var(--red); }
  .lyr.warn { color:var(--amber); border-color:var(--amber); }
  /* redesigned detail panel */
  .dh { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; margin-bottom:.35rem; }
  .dh-tool { font-weight:700; font-size:.95rem; }
  .dh-status { font-size:.72rem; border:1px solid var(--line); border-radius:4px; padding:0 .35rem; }
  .dcmd { background:#8881; border-left:2px solid var(--blue); border-radius:4px; padding:.3rem .5rem; margin-bottom:.6rem;
          white-space:pre-wrap; word-break:break-word; max-height:7rem; overflow:auto; font-size:.82rem; }
  .dcard { border:1px solid var(--line); border-radius:8px; padding:.4rem .55rem; margin-bottom:.6rem; }
  .dcard-h { font-size:.78rem; font-weight:600; opacity:.85; margin-bottom:.3rem; }
  .dcard-h.clickable { cursor:pointer; opacity:.72; margin-bottom:0; }
  .dcard-h.clickable:hover { opacity:1; }
  .dcard pre { margin:.35rem 0 0; max-height:24rem; }
  .lyrrow { border-top:1px solid var(--line); padding:.35rem 0 .25rem; }
  .lyrrow:first-of-type { border-top:0; }
  .lyrrow.focus { background:#3b82f614; border-radius:6px; padding:.35rem .3rem .25rem; }
  .lyrrow-h { display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; }
  .lyrname { font-weight:600; font-size:.8rem; }
  .verdict { font-size:.74rem; }
  .verdict.ok { color:var(--green); } .verdict.bad { color:var(--red); } .verdict.warn { color:var(--amber); }
  .lyrrules { margin:.25rem 0 0 .2rem; }
  .lyrrules .it { font-size:.8rem; margin:.1rem 0; opacity:.9; }
  /* the one rule that actually fired — make it pop out of a long list */
  .it.fired { opacity:1; font-weight:600; border-radius:5px; padding:.05rem .3rem; }
  .it.fired.bad { background:#ef444420; color:var(--red); box-shadow:inset 2px 0 0 var(--red); }
  .it.fired.warn { background:#f59e0b20; color:var(--amber); box-shadow:inset 2px 0 0 var(--amber); }
  .firedtag { font-size:.7rem; font-weight:700; margin-left:.35rem; }
  /* pinpoint callout at the top of the 3-Layer card */
  .firedcallout { display:flex; align-items:baseline; gap:.45rem; flex-wrap:wrap; padding:.35rem .5rem;
    margin:.1rem 0 .5rem; border-radius:6px; font-size:.8rem; }
  .firedcallout.bad { background:#ef444418; border:1px solid var(--red); }
  .firedcallout.warn { background:#f59e0b18; border:1px solid var(--amber); }
  .firedbadge { font-weight:700; }
  .firedcallout.bad .firedbadge { color:var(--red); } .firedcallout.warn .firedbadge { color:var(--amber); }
  .firedid { font-family:var(--mono,monospace); font-size:.74rem; padding:.05rem .35rem; border:1px solid var(--line);
    border-radius:999px; opacity:.85; }
  .firedtext { flex:1 1 12rem; opacity:.95; }
  .dchip { border-color:var(--amber); opacity:1; }
  .dchip.deny { border-color:var(--red); color:var(--red); }
  .dchip.esc { border-color:var(--amber); color:var(--amber); }
  .harnesspanel { border:1px solid var(--blue); border-radius:8px; padding:.45rem .6rem; margin:.6rem 0 .8rem; background:#3b82f612; }
  .harnesspanel.service { border-color:var(--green); background:#22a35a12; }
  /* project-first 3-Layer editor: a project picker selects whose harness you edit */
  .hp-picker { display:flex; gap:.35rem; align-items:center; flex-wrap:wrap; margin:.1rem 0 .7rem; }
  .hp-proj { font-size:.8rem; padding:.18rem .6rem; border-radius:999px; }
  .hp-proj.cur { background:var(--blue); color:#fff; border-color:var(--blue); }
  .harnesspanel .hp-head { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }
  .hp-body { margin:.45rem 0 .1rem; display:grid; gap:.5rem; }
  .hp-body h4 { margin:.1rem 0; font-size:.75rem; opacity:.75; text-transform:uppercase; letter-spacing:.03em; }
  .hp-body .it { font-size:.82rem; margin:.12rem 0 .12rem .3rem; }
  @media (max-width: 900px) {
    main { grid-template-columns: 1fr; }
    aside, #detail { max-height:16rem; border:0; border-bottom:1px solid var(--line); }
    #canvas { grid-template-columns: 1fr; }
    .scopedcrow { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<header>
  <h1>harness-lens <span class="muted">live</span></h1>
  <button id="mode" title="모드 전환">mode: …</button>
  <button id="harness" title="전역 base와 프로젝트별 하네스 보기">⚙ 하네스</button>
  <button id="scopes" title="프로젝트/세션별 하네스 오버레이">프로젝트 하네스</button>
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
<div id="harnessModal" class="modal" style="display:none">
  <div class="modalbox">
    <div class="modalhead"><b>⚙ 하네스 (3-Layer)</b>
      <span class="hl-badge">사람이 직접 편집 · AHE 자동진화는 Layer 3만</span>
      <span class="grow"></span><button id="harnessClose">✕</button></div>
    <div id="harnessBody" style="overflow:auto; padding:.6rem .8rem;"></div>
    <div class="modalfoot"><span id="harnessMsg" class="muted"></span></div>
  </div>
</div>
<script>
const TOKEN = "__HL_TOKEN__";
const H = { "X-HL-Token": TOKEN, "Content-Type": "application/json" };
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const api = (p, opt={}) => fetch(p, { ...opt, headers: { ...H, ...(opt.headers||{}) } });

const state = { flows:{}, tasks:{}, steps:{}, approvals:{}, effective:{}, serviceHarness:{},
  expanded:new Set(), expandedProjects:new Set(), loaded:new Set(), scopes:[],
  selFlow:null, selTask:null, sel:null,
  mode:"observe", connected:false, snapRev:0, trajMode:"category" };
const SRC_ICON = { claude_code:"🟧", codex:"🟦" };
const SRC_NAME = { claude_code:"claude", codex:"codex" };

// Make each step read like an action, not a bare tool name.
const TOOL_ICON = { Read:"📖", Glob:"🔎", Grep:"🔎", Bash:"⚡", Write:"✏️", Edit:"✏️", MultiEdit:"✏️",
  NotebookEdit:"✏️", apply_patch:"✏️", WebSearch:"🌐", WebFetch:"🌐", Task:"🤖", Agent:"🤖", TodoWrite:"📝", Skill:"🧩", SlashCommand:"🧩" };
function toolIcon(name){ if(!name) return "•"; if(name.startsWith("mcp__")) return "🔌"; return TOOL_ICON[name] || "🔧"; }
function toolLabel(name){ return name && name.startsWith("mcp__") ? name.replace(/^mcp__/,"").replace(/__/g," · ") : (name||"?"); }
// Service-harness (scaffolding) a step exercised — surfaced as chips so a sea of "Bash" steps
// still shows which skill/workflow/rule/MCP/instruction governed each action.
const USAGE_ICON = { skill:"🧩", command:"⌘", workflow:"🧭", invoke:"🧩", mcp:"🔌", mcp_config:"🔌",
  instruction:"📄", cursor_rule:"📕", plugin:"🧱", import:"📄", script:"📜" };
function usageLabel(u){ return (USAGE_ICON[u.kind]||"•") + " " + (u.kind==="mcp" ? toolLabel(u.name) : u.name); }
// A one-line summary of a step's actual argument — so a wall of "Bash" steps is scannable
// (the command), not all identical. Falls back to the most telling input field per tool.
function stepSummary(s){
  let inp = s.tool_input;
  if (typeof inp === "string") { try { inp = JSON.parse(inp); } catch(e) { return ""; } }
  if (!inp || typeof inp !== "object") return "";
  let v = inp.command != null ? (Array.isArray(inp.command) ? inp.command.join(" ") : inp.command)
    : inp.file_path || inp.path || inp.pattern || inp.url || inp.query || inp.prompt
    || inp.description || (inp.value != null && typeof inp.value !== "object" ? inp.value : "")
    || (inp.todos ? "todos" : "");
  return ("" + v).replace(/\s+/g, " ").trim();
}
// A clickable service-harness chip — click shows the component's actual prompt in the detail panel.
function usageChip(u, cwd){
  const c = el("span","chip uchip", usageLabel(u));
  c.title = "클릭 — " + u.kind + " 프롬프트 보기: " + u.name;
  c.onclick = (ev) => { ev.stopPropagation(); state.sel = null; showComponent(u.kind, u.name, cwd); };
  return c;
}
// Coarse category for grouping the trajectory: 탐색·읽기 / 구현 / 검증 / 외부도구 / 실행 / 서브에이전트.
const CAT_LABEL = { search:"🔎 탐색·읽기", impl:"✏️ 구현·수정", verify:"✅ 테스트·검증",
  external:"🌐 외부도구", run:"⚡ 실행·기타", agent:"🤖 서브에이전트", other:"• 기타" };
function stepCategory(s){
  const t = s.tool_name || "";
  const cmd = (stepSummary(s) || "").toLowerCase();
  if (t.startsWith("mcp__") || t==="WebSearch" || t==="WebFetch") return "external";
  if (t==="Task" || t==="Agent") return "agent";
  if (["Edit","Write","MultiEdit","NotebookEdit","apply_patch"].includes(t)) return "impl";
  if (["Read","Grep","Glob"].includes(t)) return "search";
  if (t==="Bash") {
    if (/\b(pytest|jest|vitest|go test|cargo test|npm (run )?test|yarn test|ruff|mypy|tsc|eslint|lint|test)\b/.test(cmd)) return "verify";
    if (/\b(grep|rg|find|fd|ls|cat|sed|head|tail|git (log|diff|show|status|branch))\b/.test(cmd)) return "search";
    return "run";
  }
  return "other";
}
// Legacy category labels that some older turn tasks carry as a "title" — not a real request.
const _CATS = new Set(["수집","실행","반영","조사","외부도구","기타","관측불가"]);
function cleanTitle(s){ s=(s==null?"":(""+s)).trim(); if(!s||s[0]==="{"||s[0]==="[") return ""; return _CATS.has(s)?"":s; }
function baseName(path){ path=(path||"").replace(/\/+$/,""); return path ? path.split("/").pop() : ""; }
function projectLabel(f){ return f.cwd ? baseName(f.cwd) || f.cwd : "cwd 미관측"; }
// A project = one cwd folder; sessions of the same folder stack under it in the sidebar.
function projKey(f){ return (f && f.cwd) ? (""+f.cwd).replace(/\/+$/,"") : "__nocwd__"; }
function projName(cwd){ return baseName(cwd) || cwd || "cwd 미관측"; }
async function loadScopes(){
  try { state.scopes = (await (await api("/api/scopes")).json()).scopes || []; }
  catch(e){ state.scopes = state.scopes || []; }
}
// The per-project enforce/observe override (exact-cwd scope), or null = inherits the global mode.
function projectOverrideMode(cwd){
  const k = (""+(cwd||"")).replace(/\/+$/,"");
  for (const s of (state.scopes||[])) {
    const m = (s.match||{}).cwd; if (m && (""+m).replace(/\/+$/,"")===k && s.mode) return s.mode;
  }
  return null;
}
// Only sessions tied to a real project folder are tracked; cwd-less subagent/tool sessions are hidden.
function hasCwd(f){ return !!(f && f.cwd && (""+f.cwd).trim()); }
function harnessName(f){ return (f.harness && f.harness.scope_name) || "global"; }
function harnessMode(f){ return (f.harness && f.harness.mode) || f.mode || state.mode || "?"; }
function l3Summary(layer3){
  const l3 = layer3 || {};
  return ["retry_threshold","latency_multiplier","failure_count_trigger","quality_threshold"]
    .filter(k => l3[k] != null).map(k => k.replace(/_threshold|_trigger|_multiplier/g,"")+":"+l3[k]).join(" · ");
}
function srcName(f){ return SRC_NAME[f.source] || f.source || "?"; }
function clip(s, n){ s = (s==null?"":(""+s)); return s.length > n ? s.slice(0,n) + "…" : s; }
function relTime(ts){
  if (!ts) return "";
  const sec = Math.max(0, Date.now()/1000 - ts);
  if (sec < 60) return "방금";
  if (sec < 3600) return Math.floor(sec/60) + "분 전";
  if (sec < 86400) return Math.floor(sec/3600) + "시간 전";
  return Math.floor(sec/86400) + "일 전";
}
// The user's requests inside a session = top-level 'turn' tasks (subagents are nested below them).
function turnTasks(flowId){
  return Object.values(state.tasks)
    .filter(t => t.flow_id === flowId && t.kind !== "subagent" && !t.parent_task_id)
    .sort((a,b) => a.seq - b.seq);
}
function requestLabel(t){ return cleanTitle(t && t.title) || "(요청 미관측)"; }
function stepCountForTask(taskId){ return Object.values(state.steps).filter(s => s.task_id === taskId).length; }
// Prefer landing on the most recent request that actually carries a prompt, not a blank turn.
function defaultTurn(flowId){
  const turns = turnTasks(flowId); if (!turns.length) return null;
  for (let i = turns.length - 1; i >= 0; i--) if (cleanTitle(turns[i].title)) return turns[i];
  return turns[turns.length - 1];
}

// ---- snapshot load ----
async function loadSnapshot() {
  const st = await (await api("/api/status")).json();
  state.mode = st.mode; state.snapRev = st.rev || 0; renderMode();
  await loadScopes();
  const flows = await (await api("/api/flows?limit=50&has_cwd=true")).json();
  for (const f of flows) state.flows[f.flow_id] = f;
  // Open the most recent session and select its latest request, so the page isn't empty.
  const recent = [...flows].filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0))[0];
  if (recent && !state.selFlow) {
    state.expandedProjects.add(projKey(recent));  // expand the project this session belongs to
    state.expanded.add(recent.flow_id);
    await loadTree(recent.flow_id);
    const turn = defaultTurn(recent.flow_id);
    state.selFlow = recent.flow_id;
    state.selTask = turn ? turn.task_id : null;
  }
  const appr = await (await api("/api/approvals")).json();
  appr.forEach(a => state.approvals[a.approval_id] = { ...a, step_id:a.step_id });
  renderSidebar(); renderCanvas(); renderPending();
}
async function loadTree(flowId) {
  const tree = await (await api("/api/flows/" + flowId + "/tree")).json();
  if (tree.detail) return;
  state.flows[flowId] = { ...state.flows[flowId], ...flowOnly(tree) };
  (function walk(tasks){ for (const t of tasks||[]) { state.tasks[t.task_id] = taskOnly(t);
    for (const s of t.steps||[]) state.steps[s.step_id] = s; walk(t.children); } })(tree.tasks);
  state.loaded.add(flowId);
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
    if (m.entity === "flow" && hasCwd(state.flows[m.data.flow_id]) && !(m.data.flow_id in seenSidebar)) renderSidebar();
    scheduleRender();
  } else if (m.op === "approval") {
    state.approvals[m.data.approval_id] = m.data; renderPending(); scheduleRender();
  } else if (m.op === "mode_changed") {
    state.mode = m.data.mode; renderMode();
  } else if (m.op === "criteria_changed") {
    loadSnapshot();
    if (_crit) refreshHarnessModal(m.data);
  }
}
let raf = 0; const seenSidebar = {};
function scheduleRender() { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; renderSidebar(); renderCanvas(); renderPending(); }); }

// ---- rendering ----
function renderMode() {
  const b = $("#mode"); b.textContent = "mode: " + state.mode;
  b.style.background = state.mode === "enforce" ? "var(--amber)" : "transparent";
  b.style.color = state.mode === "enforce" ? "#000" : "";
}
function renderConn() { const c = $("#conn"); c.textContent = state.connected ? "● 연결됨" : "● 끊김"; c.className = "badge " + (state.connected ? "ok" : "bad"); }
function statusColor(s) { return ({running:"var(--blue)",completed:"var(--green)",failed:"var(--red)",aborted:"#888"})[s] || "#888"; }

// Sidebar: grouped by PROJECT (folder). Each project is an accordion whose sessions stack beneath
// it (like a Codex-style project → conversations list). A session expands into the user's requests
// (turn tasks); picking one focuses the canvas. Each project header also pins its own enforce/observe.
function renderSidebar() {
  const box = $("#sessions"); box.replaceChildren();
  // cwd-less subagent/tool sessions are excluded from tracking.
  const flows = Object.values(state.flows).filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0));
  if (!flows.length) { box.append(el("p","muted","아직 추적된 세션이 없습니다.")); return; }
  // Group flows by project folder, keeping each group's most-recent activity for ordering.
  const groups = new Map();
  for (const f of flows) {
    seenSidebar[f.flow_id] = 1;
    const k = projKey(f);
    if (!groups.has(k)) groups.set(k, { key:k, cwd:f.cwd, flows:[], latest:0 });
    const g = groups.get(k); g.flows.push(f); g.latest = Math.max(g.latest, f.started_at||0);
  }
  [...groups.values()].sort((a,b)=>b.latest-a.latest).forEach(g => box.append(renderProject(g)));
}
function renderProject(g) {
  const open = state.expandedProjects.has(g.key);
  const wrap = el("div","projgroup");
  const head = el("div","projhead" + (open ? " open" : ""));
  const running = g.flows.some(f => f.status === "running");
  head.append(el("span","caret", open ? "▾" : "▸"), el("span","projicon","📁"));
  const name = el("span","pname", projName(g.cwd)); name.title = g.cwd || "";
  head.append(name);
  if (running) { const d = el("span","dot"); d.style.background = statusColor("running"); head.append(d); }
  head.append(el("span","grow"), el("span","cnt", g.flows.length + "세션"), projModeControl(g.cwd));
  head.onclick = (ev) => { if (ev.target.closest(".modectl")) return; toggleProject(g.key); };
  wrap.append(head);
  if (open) {
    const list = el("div","projsessions");
    g.flows.forEach(f => list.append(renderSession(f)));
    wrap.append(list);
  }
  return wrap;
}
// A per-project enforce/observe picker. "전역" clears the override so the project follows global mode.
function projModeControl(cwd) {
  const ov = projectOverrideMode(cwd);          // null → inherits global
  const eff = ov || state.mode;                 // currently effective mode for this project
  const wrap = el("span","modectl");
  const sel = el("select","modesel " + (eff === "enforce" ? "enf" : "obs"));
  [["", "전역(" + state.mode + ")"], ["observe", "observe"], ["enforce", "enforce"]].forEach(([v, label]) => {
    const o = el("option", null, label); o.value = v; if ((ov || "") === v) o.selected = true; sel.append(o);
  });
  sel.title = ov ? ("이 프로젝트 고정: " + ov) : ("전역 모드 따름 (" + state.mode + ")");
  sel.onclick = (ev) => ev.stopPropagation();
  sel.onchange = (ev) => { ev.stopPropagation(); setProjectMode(cwd, sel.value || "global"); };
  wrap.append(sel);
  return wrap;
}
async function setProjectMode(cwd, mode) {
  const r = await api("/api/projects/mode", { method:"POST", body: JSON.stringify({ cwd, mode }) });
  if (!r.ok) return;
  await loadScopes();
  // Refresh flow cards so each session's harness chip reflects the new project mode, then repaint.
  const flows = await (await api("/api/flows?limit=50&has_cwd=true")).json();
  for (const f of flows) state.flows[f.flow_id] = { ...state.flows[f.flow_id], ...f };
  renderSidebar(); renderCanvas();
}
function toggleProject(key) {
  if (state.expandedProjects.has(key)) state.expandedProjects.delete(key);
  else state.expandedProjects.add(key);
  renderSidebar();
}
// One session (conversation) inside a project group: "[claude]/[codex] · 시간 · 상태" → requests.
function renderSession(f) {
  const open = state.expanded.has(f.flow_id);
  const item = el("div","sessitem");
  const head = el("div","sesshead" + (state.selFlow===f.flow_id ? " cur" : ""));
  head.append(el("span","caret", open ? "▾" : "▸"),
    el("span","srctag " + (f.source==="codex" ? "codex" : "claude"), srcName(f)));
  const sub = el("span","sname smeta", relTime(f.started_at) + " · " + f.status);
  sub.title = f.cwd || "";
  const dot = el("span","dot"); dot.style.background = statusColor(f.status);
  head.append(sub, dot);
  head.onclick = () => toggleSession(f.flow_id);
  item.append(head);
  const turns = open ? turnTasks(f.flow_id) : [];
  if (open) {
    item.append(el("div","sesssub muted", "요청 " + turns.length));
    const list = el("div","reqlist");
    if (!turns.length) list.append(el("div","muted reqempty","(요청 미관측)"));
    // Newest request on top (turnTasks is chronological; reverse only for display).
    [...turns].reverse().forEach(t => list.append(reqItem(f, t)));
    item.append(list);
  }
  return item;
}
function reqItem(f, t) {
  const r = el("div","reqitem" + (state.selTask===t.task_id ? " sel" : ""));
  const dot = el("span","dot"); dot.style.background = statusColor(t.status);
  const txt = el("span","rtext", clip(requestLabel(t), 42)); txt.title = requestLabel(t);
  r.append(dot, txt);
  const n = stepCountForTask(t.task_id); if (n) r.append(el("span","chip", n + "단계"));
  if (t.retry_count > 0) r.append(el("span","chip","⟳"+t.retry_count));
  r.onclick = () => selectRequest(f.flow_id, t.task_id);
  return r;
}
async function toggleSession(flowId) {
  if (state.expanded.has(flowId)) { state.expanded.delete(flowId); }
  else { state.expanded.add(flowId); if (!state.loaded.has(flowId)) await loadTree(flowId); }
  state.selFlow = flowId;
  // Default to the latest meaningful request when opening a session with none selected for it.
  if (state.expanded.has(flowId) && !(state.selTask && state.tasks[state.selTask]
        && state.tasks[state.selTask].flow_id === flowId)) {
    const turn = defaultTurn(flowId);
    state.selTask = turn ? turn.task_id : null;
  }
  renderSidebar(); renderCanvas();
}
async function selectRequest(flowId, taskId) {
  if (!state.loaded.has(flowId)) await loadTree(flowId);
  state.selFlow = flowId; state.selTask = taskId; state.expanded.add(flowId);
  renderSidebar(); renderCanvas();
}

// The canvas focuses on the one selected request: its ask, the harness that applied to it,
// and the trajectory (what claude/codex actually did).
function renderCanvas() {
  const c = $("#canvas"); c.replaceChildren();
  if (!state.selFlow) { c.append(el("p","muted","왼쪽에서 세션을 펼쳐 요청을 선택하세요.")); return; }
  const f = state.flows[state.selFlow]; if (!f) { c.append(el("p","muted","세션을 불러오는 중…")); return; }
  if (!state.selTask) {
    c.append(el("p","muted","이 세션의 요청을 왼쪽에서 선택하세요."));
    c.append(renderHarnessPanel(f));
    return;
  }
  const t = state.tasks[state.selTask];
  if (!t) { c.append(el("p","muted","요청을 불러오는 중…")); return; }

  const view = el("div","reqview");
  const head = el("div","reqhead");
  head.append(el("span","srctag " + (f.source==="codex" ? "codex" : "claude"), srcName(f)));
  const proj = el("span","chip project","project:"+projectLabel(f)); proj.title = f.cwd || "cwd 미관측";
  head.append(proj, el("span","chip",t.status),
    el("span","chip",(f.total_tokens||0).toLocaleString()+" tok"));
  if (t.retry_count > 0) head.append(el("span","chip","⟳"+t.retry_count));
  view.append(head);

  // The user's request, verbatim.
  const ask = el("div","reqask"); ask.append(el("span","reqicon","🧑 "), document.createTextNode(requestLabel(t)));
  view.append(ask);

  // (A) the external scaffolding (service harness) applied to this session's project.
  view.append(renderServicePanel(f));
  // (B) our 3-Layer harness that gated this request.
  view.append(renderHarnessPanel(f));

  // The trajectory — steps + nested subagents. Each step carries its (A) usage + (B) decision chips.
  const th = el("div","trajhead");
  th.append(el("h2",null,"동작 내역 (시간순 ↑오래된 ↓최근)"));
  const modeBtn = el("button","iconbtn", state.trajMode==="time" ? "⏱ 시간순" : "🗂 카테고리");
  modeBtn.title = "보기 전환: 시간순 ↔ 카테고리별";
  modeBtn.onclick = () => { state.trajMode = state.trajMode==="time" ? "category" : "time"; renderCanvas(); };
  th.append(el("span","grow"), modeBtn);
  view.append(th);
  view.append(renderTrajectory(t));
  c.append(view);
}

function renderHarnessPanel(f) {
  const p = el("div","harnesspanel");
  const head = el("div","hp-head");
  const title = el("span","hp-title","⚙ 3-Layer 하네스 — 클릭해 적용 규칙 보기");
  title.style.cursor = "pointer";
  head.append(title,
    el("span","chip scope","harness:"+harnessName(f)),
    el("span","chip","mode:"+harnessMode(f)));
  if (f.harness) {
    head.append(el("span","chip","L1 "+f.harness.invariants_count),
      el("span","chip","L2 "+f.harness.domain_criteria_count));
    const l3 = l3Summary(f.harness.layer3); if (l3) head.append(el("span","chip",l3));
  }
  // L3 threshold breach (run-level; L3 never blocks a step, so surface WHICH limit was crossed).
  const l3s = f.l3_status;
  if (l3s && l3s.breached) {
    const bits = [];
    if (l3s.failures && l3s.failures.breached) bits.push("실패 "+l3s.failures.value+"/"+l3s.failures.threshold);
    if (l3s.slow && l3s.slow.breached) bits.push("지연 "+l3s.slow.value+"건(>"+l3s.slow.threshold+"x)");
    if (l3s.low_quality && l3s.low_quality.breached) bits.push("저품질 "+l3s.low_quality.value+"건(<"+l3s.low_quality.threshold+")");
    const c = el("span","chip dchip deny","⚠ L3 임계 초과: "+bits.join(" · "));
    c.title = "Layer 3 한계선 초과 — 차단은 아니고 경보/자동진화 트리거 대상";
    head.append(c);
  }
  const detailBtn = el("button","iconbtn","▸"); detailBtn.title = "적용 규칙 자세히 보기";
  const editBtn = el("button",null,"이 프로젝트 하네스 편집");
  editBtn.onclick = (ev) => { ev.stopPropagation(); openHarness(f.cwd || "__global__"); };
  const full = el("button",null,"전역 기본");
  full.onclick = (ev) => { ev.stopPropagation(); openHarness("__global__"); };
  head.append(el("span","grow"), detailBtn, editBtn, full);
  p.append(head);
  const body = el("div","hp-body"); body.style.display = "none"; p.append(body);
  const toggle = () => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "";
    detailBtn.textContent = open ? "▸" : "▾";
    if (!open) loadEffectiveInto(body, f);
  };
  detailBtn.onclick = (ev) => { ev.stopPropagation(); toggle(); };
  title.onclick = toggle;
  return p;
}
async function loadEffectiveInto(body, f) {
  body.replaceChildren(el("p","muted","불러오는 중…"));
  let e = state.effective[f.flow_id];
  if (!e) {
    e = await (await api("/api/criteria/effective?flow_id="+encodeURIComponent(f.flow_id))).json();
    state.effective[f.flow_id] = e;
  }
  body.replaceChildren();
  const sc = e.scope || null;
  body.append(el("div","muted", "적용 하네스: " + (e.scope_name||"global") + " · mode: " + (e.mode||"?")
    + (sc ? "  (전역 기본 + 이 프로젝트 규칙)" : "  (전역 기본만 적용)")));
  // Provenance tags so it is clear which rule came from the global base vs this project's scope.
  const addInv = new Set((sc && sc.add_invariants) || []);
  const addDc = new Set(((sc && sc.add_domain_criteria) || []).flatMap(d => [d.id, d.description]));
  const l3over = new Set(Object.keys((sc && sc.layer3) || {}));
  // Which rules actually fired somewhere in THIS flow → badge them so a long L1/L2 list shows at a
  // glance which constraints tripped (the answer to "which of the 30 did the harness catch?").
  const firedIn = (layer) => new Set(Object.values(state.steps)
    .filter(st => st.flow_id === f.flow_id && st.decision_layer === layer && st.decision_criterion)
    .map(st => st.decision_criterion));
  const firedInv = firedIn(1), firedDc = firedIn(2);
  const tag = (isProj) => el("span", "prov " + (isProj ? "proj" : "glob"), isProj ? "이 프로젝트" : "전역");
  const row = (isProj, text, hit) => {
    const it = el("div","it" + (hit ? " fired warn" : "")); it.append(tag(isProj), document.createTextNode(" " + text));
    if (hit) it.append(el("span","firedtag", " ⏸ 이 세션에서 걸림"));
    return it;
  };

  const l1 = el("div"); l1.append(el("h4",null,"Layer 1 — 절대 규칙 (안전·위반 금지)"));
  if ((e.invariants||[]).length) e.invariants.forEach(r => l1.append(row(addInv.has(r), r, firedInv.has(r))));
  else l1.append(el("div","it muted","(없음)"));
  const l2 = el("div"); l2.append(el("h4",null,"Layer 2 — 행동 기준 (매 단계 품질 심사)"));
  if ((e.domain_criteria||[]).length) e.domain_criteria.forEach(dc =>
    l2.append(row(addDc.has(dc.id) || addDc.has(dc.description),
      (dc.description||dc.id||"") + (dc.weight!=null ? " (w"+dc.weight+")" : ""), firedDc.has(dc.id))));
  else l2.append(el("div","it muted","(없음)"));
  const l3 = el("div"); l3.append(el("h4",null,"Layer 3 — 품질 한계선 (자동 임계값)"));
  Object.entries(e.layer3||{}).forEach(([k,v]) =>
    l3.append(row(l3over.has(k), k + ": " + v + (l3over.has(k) ? "  (이 프로젝트 오버라이드)" : ""))));
  body.append(l1, l2, l3);
}

// (A) service-harness panel — the external scaffolding (CLAUDE.md/AGENTS.md/skills/workflows/
// settings/MCP/hooks + .cursor/rules) applied to this session's project, scanned on demand.
function renderServicePanel(f) {
  const p = el("div","harnesspanel service");
  const head = el("div","hp-head");
  head.append(el("span",null,"🧩 서비스 하네스 (스캐폴딩)"),
    el("span","chip project","project:"+projectLabel(f)),
    el("span","chip","platform:"+srcName(f)));
  const toggle = el("button","iconbtn","▾"); toggle.title = "접기/펼치기";
  head.append(el("span","grow"), toggle);
  p.append(head);
  const body = el("div","hp-body"); p.append(body);
  toggle.onclick = () => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "";
    toggle.textContent = open ? "▸" : "▾";
    if (!open) loadServiceInto(body, f);
  };
  loadServiceInto(body, f);  // shown expanded by default — it's the answer to "what applied"
  return p;
}
async function loadServiceInto(body, f) {
  const id = f.flow_id;
  let e = state.serviceHarness[id];
  if (e === undefined) {
    body.replaceChildren(el("p","muted","스캐폴딩 스캔 중…"));
    const pr = api("/api/flows/"+encodeURIComponent(id)+"/service_harness").then(r=>r.json());
    state.serviceHarness[id] = pr;
    e = await pr; state.serviceHarness[id] = e;
  } else if (e && typeof e.then === "function") {
    e = await e;
  }
  body.replaceChildren();
  const comps = (e && e.components) || [];
  if (!comps.length) { body.append(el("p","muted","감지된 서비스 스캐폴딩이 없습니다.")); return; }
  const groups = {};
  comps.forEach(c => { (groups[c.scope] = groups[c.scope] || []).push(c); });
  Object.entries(groups).forEach(([scope, items]) => {
    const g = el("div"); g.append(el("h4",null, scope));
    items.forEach(c => {
      const row = el("div","it"); row.title = c.path;
      row.append(el("span","chip uchip", c.kind), document.createTextNode(
        " " + baseName(c.path) + (c.detail ? " — " + c.detail : "") + (c.editable ? "  ✎AHE" : "")));
      g.append(row);
    });
    body.append(g);
  });
}

function renderTrajectory(t) {
  const wrap = el("div");
  // Chronological: ascending seq → earliest at top, latest at the bottom.
  const steps = Object.values(state.steps).filter(s => s.task_id===t.task_id).sort((a,b)=>a.seq-b.seq);
  const kids = Object.values(state.tasks).filter(x => x.parent_task_id===t.task_id).sort((a,b)=>a.seq-b.seq);
  if (!steps.length && !kids.length) { wrap.append(el("p","muted","아직 동작이 없습니다.")); return wrap; }
  if (state.trajMode === "time") {
    // Pure chronological — one list, earliest top → latest bottom (no regrouping).
    const chart = el("div","flowchart");
    steps.forEach(s => chart.append(renderStep(s)));
    wrap.append(chart);
  } else {
    // Group steps into big categories (ordered by first appearance; chronological within each) so
    // the trajectory reads as 탐색 / 구현 / 검증 … instead of a flat wall of Bash calls.
    const groups = new Map();
    steps.forEach(s => { const c = stepCategory(s); if (!groups.has(c)) groups.set(c, []); groups.get(c).push(s); });
    for (const [cat, list] of groups) {
      const sec = el("div","catsec");
      sec.append(el("div","cathead", (CAT_LABEL[cat]||cat) + "  ·  " + list.length));
      const chart = el("div","flowchart");
      list.forEach(s => chart.append(renderStep(s)));
      sec.append(chart); wrap.append(sec);
    }
  }
  if (kids.length) {
    const sec = el("div","catsec");
    sec.append(el("div","cathead", CAT_LABEL.agent + "  ·  " + kids.length));
    const chart = el("div","flowchart");
    kids.forEach(x => chart.append(renderTask(x)));
    sec.append(chart); wrap.append(sec);
  }
  return wrap;
}
// A nested task (subagent) inside the trajectory.
function renderTask(t) {
  const wrap = el("div","tasknode");
  const hdr = el("div","taskhdr");
  const isSub = t.kind==="subagent";
  const req = isSub ? "" : cleanTitle(t.title);
  const label = el("span",null,isSub ? ("🤖 "+(t.agent_name||"subagent")) : ("🧑 "+(req||"요청 미관측").slice(0,46)));
  if (req) label.title = req;
  hdr.append(label);
  if (t.retry_count>0) hdr.append(el("span","chip","⟳"+t.retry_count));
  hdr.append(el("span","chip muted",t.status));
  wrap.append(hdr);
  if (req) { const p = el("div","prompt"); p.append(el("span","reqicon","🧑 "), document.createTextNode(req)); wrap.append(p); }
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
  // Main row: tool + the actual command/argument inline, so Bash steps are scannable at a glance.
  const main = el("div","step-main");
  main.append(el("span","tname",toolIcon(s.tool_name)+" "+toolLabel(s.tool_name)));
  const sum = stepSummary(s);
  if (sum) { const sp = el("span","cmdsum", sum); sp.title = sum; main.append(sp); }
  if (s.duration_ms!=null) main.append(el("span","chip",s.duration_ms+"ms"));
  if (s.tokens!=null) main.append(el("span","chip",s.tokens+"t"));
  if (s.judge_score!=null) main.append(el("span","chip","L2 "+Number(s.judge_score).toFixed(2)));
  // (B) 3-Layer — every call is evaluated, so show L1/L2/L3 badges; click one to see that layer's
  // rules + this step's verdict. Faint by default; the layer that denied/escalated is highlighted.
  const lb = el("span","lyrbadges");
  ["L1","L2","L3"].forEach((tag, idx) => {
    const acted = s.decision_layer === (idx + 1);
    const b = el("span","lyr " + tag.toLowerCase() + (acted ? (" acted " + (s.decision==="deny" ? "bad" : "warn")) : ""), tag);
    b.title = acted && s.decision_reason ? (s.decision + ": " + s.decision_reason) : (tag + " 규칙·판정 보기");
    b.onclick = (ev) => { ev.stopPropagation(); state.sel = s.step_id; showDetail(s.step_id, tag); renderCanvas(); };
    lb.append(b);
  });
  main.append(lb);
  if (s.status === "pending_approval") main.append(renderApprovalInline(s));
  n.append(main);
  // WHY a layer acted — show the reason (rule + matched evidence) inline, not just on hover/click,
  // so the step is self-explanatory: deny=차단(red), escalate=승인대기(amber), allow-with-a-layer
  // =승인/관측(green, e.g. an approved escalation still shows the original cause).
  if (s.decision_layer != null && s.decision_reason) {
    const dec = s.decision;
    const cls = dec === "deny" ? "bad" : dec === "escalate" ? "warn" : "ok";
    const icon = dec === "deny" ? "⛔" : dec === "escalate" ? "⏸" : "✅";
    const verb = dec === "deny" ? "차단" : dec === "escalate" ? "승인대기" : "통과";
    const v = el("div","step-verdict " + cls);
    v.append(el("span","vmark", icon + " L" + s.decision_layer + " " + verb + " — "),
      document.createTextNode(s.decision_reason));
    v.title = s.decision_reason;
    v.onclick = (ev) => { ev.stopPropagation(); state.sel = s.step_id; showDetail(s.step_id, "L" + s.decision_layer); renderCanvas(); };
    n.append(v);
  }
  // (A) service harness this action exercised — its own indented sub-row so it stays visible
  // regardless of how long the command is.
  if (s.harness_usage && s.harness_usage.length) {
    const cwd = (state.flows[s.flow_id] || {}).cwd;
    const u = el("div","step-usage");
    s.harness_usage.forEach(x => u.append(usageChip(x, cwd)));
    n.append(u);
  }
  n.onclick = () => { state.sel = s.step_id; showDetail(s.step_id); renderCanvas(); };
  return n;
}
function renderApprovalInline(s) {
  const appr = Object.values(state.approvals).find(a => a.step_id===s.step_id && !a.resolved_at);
  if (!appr) return el("span","chip","대기");
  const card = el("span","chip"); card.textContent = "승인대기"; return card;
}

// ---- detail panel ----
function dcard(title) { const c = el("div","dcard"); if (title) c.append(el("div","dcard-h", title)); return c; }
function collapsible(label, text, open) {
  const c = el("div","dcard");
  const h = el("div","dcard-h clickable", (open ? "▾ " : "▸ ") + label);
  const pre = el("pre"); pre.textContent = (typeof text === "string" ? text : JSON.stringify(text, null, 2));
  pre.style.display = open ? "" : "none";
  h.onclick = () => { const o = pre.style.display !== "none"; pre.style.display = o ? "none" : ""; h.textContent = (o ? "▸ " : "▾ ") + label; };
  c.append(h, pre); return c;
}
function layerRow(tag, name, s, rules, kind, focus) {
  const acted = s.decision_layer === ({L1:1, L2:2, L3:3}[tag]);
  const fired = acted ? (s.decision_criterion || null) : null;  // which specific rule decided
  const row = el("div","lyrrow" + (focus === tag ? " focus" : ""));
  const head = el("div","lyrrow-h");
  head.append(el("span","lyr " + tag.toLowerCase() + (acted ? (" acted " + (s.decision==="deny"?"bad":"warn")) : ""), tag),
    el("span","lyrname", name));
  let verdict = "통과 (위반 없음)", cls = "ok";
  if (acted) { verdict = (s.decision||"") + (s.decision_reason ? " — " + s.decision_reason : ""); cls = s.decision==="deny" ? "bad" : "warn"; }
  else if (kind === "l3") { verdict = "런 전체 모니터링 (개별 차단 아님)"; cls = "muted"; }
  else if (kind === "dc") { verdict = "구조 검사 통과 · 자연어 기준은 비동기 Judge 채점"; cls = "ok"; }
  head.append(el("span","grow"), el("span","verdict " + cls, verdict));
  row.append(head);
  const ul = el("div","lyrrules");
  // Normalise to {text, id}; mark the one rule that actually fired so it stands out among many.
  const items = (rules.length ? rules : [{text:"(없음)", id:null}]).map(r =>
    (typeof r === "string" ? {text:r, id:r} : r));
  items.forEach(r => {
    const hit = fired != null && r.id === fired;
    const it = el("div","it" + (hit ? " fired " + (s.decision==="deny"?"bad":"warn") : ""), "• " + r.text);
    if (hit) it.append(el("span","firedtag", s.decision==="deny" ? " ⛔ 걸림" : " ⏸ 걸림"));
    ul.append(it);
  });
  row.append(ul);
  return row;
}
async function showDetail(stepId, focusLayer) {
  const body = $("#detail-body"); body.replaceChildren(el("p","muted","불러오는 중…"));
  const s = await (await api("/api/steps/" + stepId)).json();
  const f = state.flows[s.flow_id];
  // The rules in force for this flow (cached) — so the L1/L2/L3 card can list them.
  let e = state.effective[s.flow_id];
  if (!e && f) {
    try { e = await (await api("/api/criteria/effective?flow_id=" + encodeURIComponent(s.flow_id))).json(); state.effective[s.flow_id] = e; }
    catch (_) { e = null; }
  }
  body.replaceChildren();

  // Header: tool + command + status.
  const hd = el("div","dh");
  hd.append(el("span","dh-tool", toolIcon(s.tool_name) + " " + toolLabel(s.tool_name)),
    el("span","dh-status st-" + s.status, s.status));
  if (s.duration_ms != null) hd.append(el("span","muted", s.duration_ms + "ms"));
  body.append(hd);
  const cmd = stepSummary(s);
  if (cmd) { const cb = el("div","dcmd"); cb.textContent = cmd; cb.title = cmd; body.append(cb); }

  // 3-Layer evaluation card — L1/L2/L3 rules + this step's verdict.
  const mode = f ? harnessMode(f) : "?";
  const card = dcard("🛡 3-Layer 평가  ·  " + (f ? harnessName(f) : "?") + " · " + mode
    + (mode === "observe" ? " (기록만)" : " (차단/승인)"));
  // Pinpoint callout: when a rule fired, name WHICH one (id + full wording) up top, so among dozens
  // of L2 criteria the operator sees exactly the one that gated — without scanning the list.
  if (s.decision_layer != null && s.decision_criterion) {
    const dc2 = ((e && e.domain_criteria) || []).find(d => d.id === s.decision_criterion);
    const ruleText = dc2 ? (dc2.description || dc2.id) : s.decision_criterion;
    const dec = s.decision;
    const fc = el("div","firedcallout " + (dec==="deny" ? "bad" : "warn"));
    fc.append(el("span","firedbadge", (dec==="deny"?"⛔ ":"⏸ ") + "L" + s.decision_layer + " 걸린 규칙"),
      el("span","firedid", s.decision_criterion),
      el("span","firedtext", ruleText));
    card.append(fc);
  }
  card.append(layerRow("L1", "절대 규칙", s, ((e && e.invariants) || []).map(r => ({text:r, id:r})), "inv", focusLayer));
  card.append(layerRow("L2", "행동 기준", s, ((e && e.domain_criteria) || []).map(d => ({
    text: (d.description||d.id||"") + (d.weight!=null?" (w"+d.weight+")":""), id: d.id})), "dc", focusLayer));
  card.append(layerRow("L3", "품질 한계선", s, e && e.layer3 ? Object.entries(e.layer3).map(([k,v]) => ({text:k + ": " + v, id:k})) : [], "l3", focusLayer));
  body.append(card);

  // Service harness exercised here — chips clickable → show the prompt.
  const sh = dcard("🧩 서비스 하네스 (이 동작)");
  const ucwd = f ? f.cwd : null;
  if (s.harness_usage && s.harness_usage.length) {
    const box = el("div","hp-head"); s.harness_usage.forEach(u => box.append(usageChip(u, ucwd))); sh.append(box);
    sh.append(el("div","muted","칩 클릭 → 실제 프롬프트"));
  } else { sh.append(el("div","muted","감지된 스캐폴딩 없음 (일반 셸/툴 동작)")); }
  body.append(sh);

  // Input / output / judge — collapsed by default to keep the panel clean.
  if (s.tool_input) body.append(collapsible("입력", s.tool_input, false));
  if (s.tool_output) body.append(collapsible("출력", s.tool_output, false));
  if (s.judge_reason) body.append(collapsible("Judge", s.judge_reason, false));
  const appr = Object.values(state.approvals).find(a => a.step_id === stepId && !a.resolved_at);
  if (appr) body.append(approvalCard(appr, s));
}
function field(label, text) { const d = el("div"); d.append(el("h2",null,label)); const p = el("pre"); p.textContent = typeof text==="string"?text:JSON.stringify(text,null,2); d.append(p); return d; }

// Show a service-harness component's actual prompt (the skill/instruction/rule file) in the detail pane.
async function showComponent(kind, name, cwd) {
  const body = $("#detail-body"); body.replaceChildren(el("p","muted","불러오는 중…"));
  const q = "?kind=" + encodeURIComponent(kind) + "&name=" + encodeURIComponent(name)
    + (cwd ? "&cwd=" + encodeURIComponent(cwd) : "");
  let c;
  try { c = await (await api("/api/harness/component" + q)).json(); }
  catch (e) { c = { found: false, note: "불러오기 실패" }; }
  body.replaceChildren();
  const _hd = el("div"); _hd.append(el("b",null, (USAGE_ICON[kind]||"•") + " " + name)); body.append(_hd);
  body.append(el("div","muted", "서비스 하네스 · " + kind + (c.path ? " · " + c.path : "")));
  if (c.found && c.content) { const pre = el("pre"); pre.style.maxHeight="70vh"; pre.textContent = c.content; body.append(pre); }
  else body.append(el("p","muted", c.note || "프롬프트 내용을 찾지 못했습니다."));
}

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
  b.onclick = () => {
    const first = open[0]; if (!first) return;
    state.sel = first.step_id; showDetail(first.step_id);
    // Focus the session + request that owns the awaiting step.
    const s = state.steps[first.step_id];
    if (s) { const f = state.flows[s.flow_id]; if (f) state.expandedProjects.add(projKey(f));
      state.expanded.add(s.flow_id); state.selFlow = s.flow_id; if (s.task_id) state.selTask = s.task_id;
      renderSidebar(); renderCanvas(); }
  };
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
function pathHasPrefix(cwd, prefix) {
  const norm = s => (s||"").replace(/\/+$/,"");
  const c = norm(cwd), p = norm(prefix);
  return !!c && !!p && (c === p || c.startsWith(p + "/"));
}
function scopeMatchesFlow(s, f) {
  if (!s || !f || !s.match) return false;
  return (s.match.session_id && s.match.session_id === f.flow_id) ||
    (s.match.cwd_prefix && pathHasPrefix(f.cwd, s.match.cwd_prefix));
}
function scopeDraftForFlow(f) {
  return { name: projectLabel(f), match:{ cwd_prefix:f.cwd || "" }, mode:"", layer3:{},
    add_invariants:[], add_domain_criteria:[] };
}
function scopeDcRow(dc) {
  dc = dc || {};
  const r = el("div","scopedcrow");
  r.dataset.prompt = dc.judge_prompt || "";
  const id = el("input"); id.placeholder="id"; id.value=dc.id||""; id.dataset.dc="id";
  const desc = el("input"); desc.placeholder="추가 Layer-2 기준"; desc.value=dc.description||""; desc.dataset.dc="description";
  const w = el("input"); w.type="number"; w.step="any"; w.placeholder="weight"; w.value=dc.weight!=null?dc.weight:1; w.dataset.dc="weight";
  r.append(id, desc, w, delBtn(() => r.remove()));
  return r;
}
function scopeRow(s) {
  s = s || { name:"", match:{}, mode:"", layer3:{}, add_invariants:[], add_domain_criteria:[] };
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
  const l4 = el("div","line");
  l4.append(el("label",null,"추가 L2"));
  const addDc = el("button",null,"+ 기준");
  const dcBox = el("div","scopedc");
  (s.add_domain_criteria||[]).forEach(dc => dcBox.append(scopeDcRow(dc)));
  addDc.onclick = () => dcBox.append(scopeDcRow());
  l4.append(addDc, dcBox);
  row.append(l1, l2, el("div","muted","전역 base는 그대로 적용되고, 이 scope의 항목만 추가/오버라이드됩니다."), l3, l4);
  return row;
}
function collectScopes() {
  return [...document.querySelectorAll("#scopeList .scoperow")].map((row, rowIndex) => {
    const get = f => row.querySelector('[data-f="'+f+'"]');
    const matchValue = get("matchValue").value.trim();
    const match = {}; if (matchValue) match[get("matchType").value] = matchValue;
    const layer3 = {};
    row.querySelectorAll("[data-l3]").forEach(i => { if (i.value !== "") layer3[i.dataset.l3] = Number(i.value); });
    const add_invariants = get("invariants").value.split("\n").map(x=>x.trim()).filter(Boolean);
    const add_domain_criteria = [...row.querySelectorAll(".scopedcrow")].map((r, dcIndex) => {
      const dc = f => r.querySelector('[data-dc="'+f+'"]');
      const description = dc("description").value.trim();
      const item = {
        id: dc("id").value.trim() || `SC-${rowIndex+1}-${String(dcIndex+1).padStart(3,"0")}`,
        description,
        weight: Number(dc("weight").value || 1),
      };
      if (r.dataset.prompt) item.judge_prompt = r.dataset.prompt;
      return item;
    }).filter(d => d.description);
    const out = { name:get("name").value.trim(), match, layer3, add_invariants, add_domain_criteria };
    if (get("mode").value) out.mode = get("mode").value;
    return out;
  });
}
async function openScopes(seedFlow=null) {
  const data = await (await api("/api/scopes")).json();
  const list = $("#scopeList"); list.replaceChildren();
  const scopes = data.scopes || [];
  let matched = false;
  scopes.forEach(s => { if (scopeMatchesFlow(s, seedFlow)) matched = true; list.append(scopeRow(s)); });
  if (seedFlow && seedFlow.cwd && !matched) {
    list.prepend(scopeRow(scopeDraftForFlow(seedFlow)));
    $("#scopeMsg").textContent = "현재 Flow의 cwd로 새 프로젝트 하네스 초안을 추가했습니다.";
  } else if (seedFlow) {
    $("#scopeMsg").textContent = "현재 Flow에 매칭되는 프로젝트 하네스가 목록에 있습니다.";
  } else {
    $("#scopeMsg").textContent = "";
  }
  $("#scopeModal").style.display = "flex";
}
$("#scopes").onclick = () => openScopes();
$("#scopeClose").onclick = () => { $("#scopeModal").style.display = "none"; };
$("#scopeModal").onclick = e => { if (e.target.id === "scopeModal") $("#scopeModal").style.display = "none"; };
$("#scopeAdd").onclick = () => { $("#scopeList").append(scopeRow()); };
$("#scopeSave").onclick = async () => {
  const r = await api("/api/scopes", { method:"POST", body: JSON.stringify({ scopes: collectScopes() }) });
  if (r.ok) {
    const d = await r.json();
    $("#scopeMsg").textContent = "저장됨 — " + d.scopes.length + "개 스코프 적용 (즉시 반영)";
    _scopes = d.scopes || [];
    loadSnapshot();  // mode/harness chips may change for affected flows
    if ($("#harnessModal").style.display !== "none") renderHarness();
  } else {
    $("#scopeMsg").textContent = "저장 실패 (" + r.status + ")";
  }
};

// ---- harness (3-Layer base + project scopes) editor ----
let _crit = null, _scopes = [], _l1open = false, _hpProject = "__global__";
const L3HELP = {
  retry_threshold: "한 단계 재시도 허용 횟수 — 넘으면 경보",
  latency_multiplier: "평소 대비 몇 배 느리면 경보",
  failure_count_trigger: "연속 실패 몇 번에 경보/진화",
  quality_threshold: "Layer-2 품질 점수 하한 (0~1)",
};
const collectL1 = () => [...document.querySelectorAll('#hl-l1 .hl-item input.t')].map(i=>i.value);
const collectL2 = () => [...document.querySelectorAll('#hl-l2 .hl-item')].map(r => ({
  id: r.dataset.id || "", description: r.querySelector('input.t').value,
  weight: Number(r.querySelector('input.w').value || 1) }));
const collectL3 = () => { const o={}; document.querySelectorAll('#hl-l3 input.w').forEach(i => { if (i.value!=="") o[i.dataset.k]=Number(i.value); }); return o; };

async function saveLayer(layer, body) {
  const m = $("#harnessMsg"); m.textContent = "저장 중…"; m.className = "muted";
  const r = await api("/api/criteria/"+layer, { method:"POST", body: JSON.stringify(body) });
  if (r.ok) { _crit = await r.json(); _l1open = false; renderHarness(); m.textContent = "저장됨 · 다음 단계부터 즉시 적용"; m.className = "ok"; }
  else { const d = await r.json().catch(()=>({})); m.textContent = "거부: " + (d.detail || r.status); m.className = "warn"; }
}
function layerBox(id, title, help) { const b = el("div","hl-layer"); b.id = id; const h = el("h3",null,title); b.append(h, el("div","hl-help",help)); return b; }
function actions(...btns){ const a = el("div","hl-actions"); a.append(...btns); return a; }
function delBtn(onDel){ const d = el("button","hl-del","삭제"); d.onclick = onDel; return d; }
// The 3-Layer editor is project-first: pick a project (or 전역 기본) and edit the harness that
// applies to it in one place. A project's harness = the global base + that folder's own additions.
function scopeForCwd(cwd) { return _scopes.find(s => s.match && s.match.cwd === cwd) || null; }
function hpProjects() {
  const seen = new Set(), out = [];
  Object.values(state.flows).filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0))
    .forEach(f => { if (!seen.has(f.cwd)) { seen.add(f.cwd); out.push(f.cwd); } });
  return out;
}
function projectPicker() {
  const wrap = el("div","hp-picker");
  wrap.append(el("span","muted","프로젝트:"));
  const mk = (key, label, title) => {
    const b = el("button","hp-proj"+(_hpProject===key?" cur":""), label);
    if (title) b.title = title;
    b.onclick = () => { _hpProject = key; _l1open = false; $("#harnessMsg").textContent=""; renderHarness(); };
    return b;
  };
  wrap.append(mk("__global__","🌐 전역 기본","모든 프로젝트 공통"));
  hpProjects().forEach(cwd => wrap.append(mk(cwd, baseName(cwd) || cwd, cwd)));
  return wrap;
}

function renderHarness() {
  const body = $("#harnessBody"); body.replaceChildren();
  body.append(projectPicker());
  if (_hpProject === "__global__") renderGlobalEditor(body);
  else renderProjectEditor(body, _hpProject);
}

// ---- 전역 기본 (base) editor — applies to every project ----
function renderGlobalEditor(body) {
  body.append(el("div","hl-help","🌐 전역 기본 — 모든 프로젝트에 공통으로 적용되는 3-Layer 입니다."));
  // Layer 1 — invariants (unlock-gated)
  const b1 = layerBox("hl-l1","Layer 1 — 절대 규칙 (안전·위반 금지)","어떤 경우에도 어기면 안 되는 규칙. AHE 자동진화는 절대 못 바꿉니다.");
  const lock = el("button","hl-lock", _l1open ? "🔓 편집 중 — 잠그기" : "🔒 잠금 — 클릭해 편집");
  lock.onclick = () => { if (_l1open) _crit.invariants = collectL1(); _l1open = !_l1open; renderHarness(); };
  b1.querySelector("h3").append(lock);
  if (!_l1open) {
    (_crit.invariants.length ? _crit.invariants : ["(없음)"]).forEach(r => b1.append(el("div",null,"• "+r)));
  } else {
    b1.append(el("div","warn","⚠ 안전 규칙입니다. 저장 시 criteria.yaml 백업 후 즉시 반영됩니다."));
    _crit.invariants.forEach(r => {
      const it = el("div","hl-item"); const i = el("input","t"); i.type="text"; i.value=r; i.placeholder="규칙 내용";
      it.append(i, delBtn(() => { const v=collectL1(); v.splice([...b1.querySelectorAll('.hl-item')].indexOf(it),1); _crit.invariants=v; renderHarness(); }));
      b1.append(it);
    });
    const add = el("button",null,"+ 규칙 추가"); add.onclick = () => { _crit.invariants=collectL1(); _crit.invariants.push(""); renderHarness(); };
    const save = el("button","hl-save","저장"); save.onclick = () => saveLayer("layer1", { invariants: collectL1() });
    b1.append(actions(add, save));
  }
  body.append(b1);
  // Layer 2 — domain criteria
  const b2 = layerBox("hl-l2","Layer 2 — 행동 기준 (매 단계 품질 심사)","LLM 심사관이 매 단계 점수를 매기는 기준. weight = 중요도.");
  _crit.domain_criteria.forEach(dc => {
    const it = el("div","hl-item"); it.dataset.id = dc.id || "";
    it.append(el("span","cid", dc.id || "(new)"));
    const i = el("input","t"); i.type="text"; i.value=dc.description||""; i.placeholder="기준 설명";
    const w = el("input","w"); w.type="number"; w.step="any"; w.min="0"; w.title="weight"; w.value = dc.weight!=null?dc.weight:1;
    it.append(i, w, delBtn(() => { const v=collectL2(); v.splice([...b2.querySelectorAll('.hl-item')].indexOf(it),1); _crit.domain_criteria=v; renderHarness(); }));
    b2.append(it);
  });
  const add2 = el("button",null,"+ 기준 추가"); add2.onclick = () => { _crit.domain_criteria=collectL2(); _crit.domain_criteria.push({id:"",description:"",weight:1}); renderHarness(); };
  const save2 = el("button","hl-save","저장"); save2.onclick = () => saveLayer("layer2", { domain_criteria: collectL2() });
  b2.append(actions(add2, save2));
  body.append(b2);
  // Layer 3 — QA thresholds (the only AHE-evolvable layer)
  const b3 = layerBox("hl-l3","Layer 3 — 품질 한계선 (자동 임계값)","넘으면 경보/자동진화. AHE가 자동으로 조정하는 유일한 층.");
  Object.entries(_crit.layer3).forEach(([k,v]) => {
    const it = el("div","hl-item"); it.append(el("span","cid",k));
    const i = el("input","w"); i.type="number"; i.step="any"; i.dataset.k=k; i.value=v;
    it.append(i, el("span","hl-help", L3HELP[k]||"")); b3.append(it);
  });
  const save3 = el("button","hl-save","저장"); save3.onclick = () => saveLayer("layer3", { layer3: collectL3() });
  b3.append(actions(save3));
  body.append(b3);
}

// ---- per-project editor — the global base PLUS this folder's own additions/overrides ----
function renderProjectEditor(body, cwd) {
  const scope = scopeForCwd(cwd) || {};
  const label = baseName(cwd) || cwd;
  body.append(el("div","hl-help","📁 "+label+" — 전역 기본은 그대로 적용되고, 여기서는 이 폴더에만 더할 규칙·한계선을 정합니다. (정확한 폴더 경로로 매칭: "+cwd+")"));

  // Mode
  const mb = layerBox("hp-mode-box","실행 모드","이 폴더 세션만 observe/enforce 로 고정합니다. 비우면 전역 모드를 따릅니다.");
  const mrow = el("div","hl-item"); mrow.append(el("span","cid","mode"));
  const md = el("select"); md.id = "hp-mode";
  [["","전역 모드 상속"],["observe","observe (관측만)"],["enforce","enforce (차단/승인)"]].forEach(([v,l])=>{
    const o = el("option",null,l); o.value=v; md.append(o); });
  md.value = scope.mode || "";
  mrow.append(md); mb.append(mrow); body.append(mb);

  // Layer 1
  const b1 = layerBox("hp-l1","Layer 1 — 절대 규칙 (안전·위반 금지)","전역 규칙은 그대로 적용됩니다. 이 폴더 전용 규칙만 여기서 더하세요.");
  (_crit.invariants||[]).forEach(t => { const d=el("div","hl-item muted"); d.append(el("span",null,"• "+t), el("span","cid","전역")); b1.append(d); });
  (scope.add_invariants||[]).forEach(t => addProjInvRow(b1, t));
  const add1 = el("button",null,"+ 이 폴더 규칙"); add1.onclick = () => addProjInvRow(b1, "");
  b1.append(actions(add1)); body.append(b1);

  // Layer 2
  const b2 = layerBox("hp-l2","Layer 2 — 행동 기준 (매 단계 품질 심사)","전역 기준 위에 이 폴더 전용 기준을 더합니다.");
  (_crit.domain_criteria||[]).forEach(dc => { const d=el("div","hl-item muted");
    d.append(el("span","cid","전역"), el("span",null,dc.description||""), el("span","cid","w"+(dc.weight!=null?dc.weight:1))); b2.append(d); });
  (scope.add_domain_criteria||[]).forEach(dc => addProjDcRow(b2, dc));
  const add2 = el("button",null,"+ 이 폴더 기준"); add2.onclick = () => addProjDcRow(b2, {});
  b2.append(actions(add2)); body.append(b2);

  // Layer 3 — override (blank = inherit the global value)
  const b3 = layerBox("hp-l3","Layer 3 — 품질 한계선 (자동 임계값)","비우면 전역 값을 그대로 씁니다. 값을 넣으면 이 폴더만 그 값으로 덮어씁니다.");
  L3KEYS.forEach(([k,lab]) => {
    const it = el("div","hl-item"); it.append(el("span","cid",lab));
    const i = el("input","w"); i.type="number"; i.step="any"; i.dataset.k=k; i.id="hpL3-"+k;
    const base = (_crit.layer3||{})[k];
    if (scope.layer3 && scope.layer3[k]!=null) i.value = scope.layer3[k];
    i.placeholder = base!=null ? ("전역 "+base) : "";
    it.append(i, el("span","hl-help",(L3HELP[k]||"") + (base!=null?(" · 전역 "+base):"")));
    b3.append(it);
  });
  body.append(b3);

  const save = el("button","hl-save","이 프로젝트 저장"); save.onclick = () => saveProject(cwd);
  body.append(actions(save));
}
function addProjInvRow(box, text) {
  const it = el("div","hl-item proj-inv"); const i = el("input","t"); i.type="text"; i.value=text||""; i.placeholder="이 폴더 전용 규칙";
  it.append(i, delBtn(()=>it.remove())); box.insertBefore(it, box.querySelector(".hl-actions")); return it;
}
function addProjDcRow(box, dc) {
  dc = dc || {};
  const it = el("div","hl-item proj-dc"); it.dataset.id = dc.id || "";
  const i = el("input","t"); i.type="text"; i.value=dc.description||""; i.placeholder="이 폴더 전용 기준";
  const w = el("input","w"); w.type="number"; w.step="any"; w.min="0"; w.value=dc.weight!=null?dc.weight:1;
  it.append(i, w, delBtn(()=>it.remove())); box.insertBefore(it, box.querySelector(".hl-actions")); return it;
}
function collectProjInv() { return [...document.querySelectorAll('#hp-l1 .proj-inv input.t')].map(i=>i.value.trim()).filter(Boolean); }
function collectProjDc() {
  return [...document.querySelectorAll('#hp-l2 .proj-dc')].map(r => ({
    id: r.dataset.id || "", description: r.querySelector('input.t').value.trim(),
    weight: Number(r.querySelector('input.w').value || 1),
  })).filter(d => d.description);
}
function collectProjL3() {
  const o = {};
  L3KEYS.forEach(([k]) => { const i = document.getElementById("hpL3-"+k); if (i && i.value!=="") o[k]=Number(i.value); });
  return o;
}
async function saveProject(cwd) {
  const scope = { name: baseName(cwd)||cwd, match:{ cwd },
    add_invariants: collectProjInv(), add_domain_criteria: collectProjDc(), layer3: collectProjL3() };
  const mode = $("#hp-mode").value; if (mode) scope.mode = mode;
  const others = _scopes.filter(s => !(s.match && s.match.cwd === cwd));  // replace this folder's scope, keep the rest
  const m = $("#harnessMsg"); m.textContent="저장 중…"; m.className="muted";
  const r = await api("/api/scopes", { method:"POST", body: JSON.stringify({ scopes: [...others, scope] }) });
  if (r.ok) { _scopes = (await r.json()).scopes || []; renderHarness(); loadSnapshot();
    m.textContent="저장됨 · "+(baseName(cwd)||cwd)+" 의 다음 단계부터 적용"; m.className="ok"; }
  else { const d=await r.json().catch(()=>({})); m.textContent="저장 실패: "+(d.detail||r.status); m.className="warn"; }
}
async function refreshHarnessModal(crit=null) {
  const [freshCrit, scopes] = await Promise.all([
    crit ? Promise.resolve(crit) : (await api("/api/criteria")).json(),
    (await api("/api/scopes")).json(),
  ]);
  _crit = freshCrit; _scopes = scopes.scopes || [];
  renderHarness();
}
async function openHarness(project) {
  _l1open = false;
  _hpProject = project || "__global__";
  $("#harnessMsg").textContent = "";
  await refreshHarnessModal();
  $("#harnessModal").style.display = "flex";
}
$("#harness").onclick = () => openHarness("__global__");
// "프로젝트 하네스" header button jumps straight to the most recent project in the same editor.
$("#scopes").onclick = () => openHarness(hpProjects()[0] || "__global__");
$("#harnessClose").onclick = () => { $("#harnessModal").style.display = "none"; };
$("#harnessModal").onclick = e => { if (e.target.id === "harnessModal") $("#harnessModal").style.display = "none"; };

renderConn(); loadSnapshot().then(connect);
</script>
</body>
</html>
"""


def render_page(token: str) -> str:
    # Token is embedded for the page's own same-origin API/WS calls (loopback single-user model).
    return _PAGE.replace("__HL_TOKEN__", token)
