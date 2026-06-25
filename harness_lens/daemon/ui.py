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
<meta name="theme-color" content="#6366f1">
<title>harness-lens · live</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%236366f1'/><circle cx='16' cy='16' r='7.5' fill='none' stroke='white' stroke-width='2.5'/><circle cx='16' cy='16' r='2.6' fill='white'/></svg>">
<style>
  :root {
    /* Light Minimal + Indigo. color-scheme:light forces Canvas=white / CanvasText=near-black
       even under an OS dark theme, so every color-mix() below resolves to the light palette. */
    color-scheme: light;
    --blue:#6366f1; --accent:#6366f1; --accent-hover:#4f46e5;
    --green:#16a34a; --red:#dc2626; --amber:#d97706;
    --line:#ececef;        --line: color-mix(in srgb, CanvasText 9%, transparent);
    --line-soft:#f1f1f3;   --line-soft: color-mix(in srgb, CanvasText 5%, transparent);
    --line-strong:#d4d4d8; --line-strong: color-mix(in srgb, CanvasText 18%, transparent);
    --surface:#fbfbfc;  --surface: color-mix(in srgb, CanvasText 2%, Canvas);
    --surface-2:#f4f4f5; --surface-2: color-mix(in srgb, CanvasText 4.5%, Canvas);
    --surface-3:#ececef; --surface-3: color-mix(in srgb, CanvasText 7%, Canvas);
    --muted:#71717a;        --muted: color-mix(in srgb, CanvasText 54%, transparent);
    --accent-weak: color-mix(in srgb, var(--blue) 12%, Canvas);
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Apple SD Gothic Neo", "Malgun Gothic", "Helvetica Neue", Arial, sans-serif;
    --radius:8px; --radius-sm:6px; --radius-lg:12px;
    --shadow: 0 1px 2px color-mix(in srgb, CanvasText 7%, transparent);
    --shadow-pop: 0 16px 48px color-mix(in srgb, CanvasText 20%, transparent), 0 4px 12px color-mix(in srgb, CanvasText 8%, transparent);
  }
  * { box-sizing: border-box; }
  body { font: 13px/1.55 var(--sans); margin:0; height:100vh; display:flex; flex-direction:column;
         background:Canvas; color:CanvasText; -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
  code, kbd, samp { font-family:var(--mono); }
  ::selection { background:color-mix(in srgb, var(--blue) 28%, transparent); }
  /* thin, unobtrusive scrollbars — reads like a desktop console, not a default web page */
  * { scrollbar-width:thin; scrollbar-color:var(--line-strong) transparent; }
  ::-webkit-scrollbar { width:9px; height:9px; }
  ::-webkit-scrollbar-thumb { background:var(--line-strong); border-radius:99px; border:2px solid transparent; background-clip:padding-box; }
  ::-webkit-scrollbar-thumb:hover { background:color-mix(in srgb, CanvasText 38%, transparent); background-clip:padding-box; }
  ::-webkit-scrollbar-track { background:transparent; }
  header { display:flex; align-items:center; gap:.45rem; padding:.5rem 1.05rem; border-bottom:1px solid var(--line);
           background:color-mix(in srgb, CanvasText 1.5%, Canvas); }
  header h1 { font-size:.9rem; margin:0; font-weight:600; letter-spacing:-.01em; display:flex; align-items:center; gap:.45rem; }
  header h1 .logo { width:1.1rem; height:1.1rem; display:inline-grid; place-items:center; border-radius:5px; flex:none;
    background:var(--blue); color:#fff; }
  header h1 .logo svg { display:block; }
  header h1 .tag { font-size:.6rem; font-weight:600; letter-spacing:.09em; text-transform:uppercase; color:var(--muted);
    border:1px solid var(--line); border-radius:4px; padding:.06rem .3rem; margin-left:.1rem; }
  .grow { flex:1; }
  .badge { border:1px solid var(--line); border-radius:6px; padding:.16rem .5rem; font-size:.72rem; color:var(--muted);
    display:inline-flex; align-items:center; gap:.28rem; }
  .badge.alert { background:var(--amber); color:#fff; border-color:transparent; cursor:pointer; font-weight:600;
    box-shadow:0 0 0 3px color-mix(in srgb,var(--amber) 18%, transparent); }
  #conn.bad { color:var(--red); } #conn.ok { color:var(--green); }
  button { font:inherit; font-size:.8rem; padding:.32rem .72rem; border:1px solid var(--line); border-radius:var(--radius-sm);
           background:var(--surface); color:inherit; cursor:pointer; font-weight:500; letter-spacing:-.005em;
           transition:background .12s ease, border-color .12s ease; }
  button:hover:not(:disabled) { background:var(--surface-2); border-color:var(--line-strong); }
  button:active:not(:disabled) { background:var(--surface-3); }
  button:focus-visible { outline:2px solid var(--accent-weak); outline-offset:1px; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  #mode { font-weight:600; }
  main { --left-w:16.5rem; --right-w:24rem; flex:1; display:grid;
         grid-template-columns: var(--left-w) 6px minmax(20rem, 1fr) 6px var(--right-w); min-height:0; }
  aside, #detail { overflow:auto; padding:.65rem .7rem; min-width:0; }
  aside { border-right:1px solid var(--line); background:var(--surface); }
  #canvas { overflow:auto; padding:1rem 1.1rem; }
  #detail { border-left:1px solid var(--line); background:var(--surface); }
  .panehead { display:flex; align-items:center; gap:.45rem; margin-bottom:.55rem; position:sticky; top:-.65rem;
              z-index:5; background:var(--surface); padding:.15rem 0 .45rem; }
  .panehead h2 { flex:1; margin:0; }
  .pane-toggle, .iconbtn { width:1.65rem; height:1.65rem; display:inline-grid; place-items:center; padding:0;
    border-color:transparent; background:transparent; border-radius:var(--radius-sm); color:var(--muted); line-height:1; }
  .pane-toggle svg, .iconbtn svg { display:block; transition:transform .16s ease; }
  .pane-toggle:hover:not(:disabled), .iconbtn:hover:not(:disabled) {
    color:CanvasText; background:var(--surface-2); border-color:transparent;
  }
  /* disclosure chevron — one shape, rotated by an .open ancestor (replaces the → / ↓ text arrows) */
  .caret { width:.95rem; height:.95rem; display:inline-grid; place-items:center; flex:none; color:var(--muted);
    transition:transform .16s ease; }
  .caret svg { display:block; }
  .projgroup.open > .projhead .caret, .turnsec.open .turnhead .caret,
  .askblock.open .askhead .caret { transform:rotate(90deg); }
  .resize-handle { position:relative; cursor:col-resize; touch-action:none; }
  .resize-handle::before { content:""; position:absolute; top:0; bottom:0; left:50%; border-left:1px solid var(--line-soft); }
  .resize-handle:hover::before, .resize-handle.dragging::before {
    border-left-color:var(--blue); box-shadow:0 0 0 2px color-mix(in srgb, var(--blue) 18%, transparent);
  }
  main.left-collapsed #leftResize, main.right-collapsed #rightResize { cursor:default; pointer-events:none; }
  main.left-collapsed #leftResize::before, main.right-collapsed #rightResize::before { display:none; }
  main.left-collapsed aside, main.right-collapsed #detail { padding:.45rem .25rem; overflow:hidden; }
  main.left-collapsed #sessions, main.right-collapsed #detail-body { display:none; }
  main.left-collapsed aside .panehead, main.right-collapsed #detail .panehead {
    flex-direction:column; align-items:center; gap:.35rem; margin-bottom:0; padding:.1rem 0;
  }
  main.left-collapsed aside h2, main.right-collapsed #detail h2 {
    writing-mode:vertical-rl; text-orientation:upright; transform:none; margin:.1rem 0; letter-spacing:0;
  }
  h2 { font-size:.72rem; color:var(--muted); margin:.15rem .15rem .55rem; text-transform:uppercase; letter-spacing:.06em; font-weight:600; }
  .sess { display:flex; gap:.4rem; align-items:center; padding:.2rem; border-radius:5px; cursor:pointer; }
  .sess:hover { background:#8881; }
  .dot { width:.6rem; height:.6rem; border-radius:50%; display:inline-block; }
  .flowcard { border:1px solid var(--line); border-top:3px solid var(--line); border-radius:12px;
              padding:.6rem .85rem .75rem; background:var(--surface-2); box-shadow:0 1px 3px #0000001a;
              display:flex; flex-direction:column; min-width:0; }
  .flowhead { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
              margin:-.05rem -.1rem .45rem; padding-bottom:.4rem; border-bottom:1px solid var(--line); }
  .flowhead b { font-size:.95rem; flex:1 1 12rem; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .flowtools { display:flex; gap:.35rem; align-items:center; margin-left:auto; }
  .iconbtn { flex:none; }
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
  .prompt { display:block; margin:.25rem 0 .15rem .2rem; padding:.35rem .6rem; border-left:2px solid var(--blue);
            background:var(--surface-2); border-radius:6px; white-space:pre-wrap; word-break:break-word;
            max-height:9rem; overflow:auto; font-size:.84rem; }
  .children { display:grid; gap:.1rem; margin-left:.7rem; padding-left:.4rem; }
  .step { position:relative; display:flex; flex-direction:column; align-items:flex-start; width:max-content; max-width:100%; gap:.18rem;
          border:1px solid var(--line); border-radius:6px;
          padding:.16rem .5rem; margin:.2rem 0 .2rem .65rem; cursor:pointer; background:Canvas; }
  .step::before { content:""; position:absolute; left:-.65rem; top:1rem; width:.65rem; border-top:1px solid var(--line); }
  .step-main { display:flex; gap:.45rem; align-items:center; max-width:100%; }
  .step-usage { display:flex; gap:.3rem; flex-wrap:wrap; margin-left:1.35rem; }
  .step.sel { outline:2px solid var(--blue); }
  .st-running { border-color:var(--blue); box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--blue) 35%, transparent);
    animation:pulse 1.4s ease-in-out infinite; }
  .st-ok { border-color:color-mix(in srgb,var(--green) 55%, var(--line)); } .st-failed { border-color:var(--red); color:var(--red); }
  .st-denied { opacity:.5; text-decoration:line-through; }
  .st-pending_approval { border-color:var(--amber); background:color-mix(in srgb,var(--amber) 13%, Canvas); color:inherit;
    animation:pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.72} }
  .tname { white-space:nowrap; }
  /* WHY a step was caught — inline reason under a denied/escalated step */
  .step-verdict { margin-left:1.35rem; font-size:.78rem; white-space:pre-wrap; word-break:break-word; cursor:pointer; }
  .step-verdict.bad { color:var(--red); }
  .step-verdict.warn { color:var(--amber); }
  .step-verdict.ok { color:var(--green); opacity:.8; }
  .step-verdict .vmark { font-weight:700; }
  .cmdsum { flex:0 1 auto; min-width:0; max-width:34rem; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; opacity:.62; font-size:.78rem; font-family:var(--mono); }
  .step.sel .cmdsum { opacity:.85; }
  .chip { font-size:.69rem; border:1px solid var(--line); border-radius:4px; padding:.05rem .42rem; color:var(--muted);
          background:var(--surface); white-space:nowrap; font-variant-numeric:tabular-nums; }
  .muted { opacity:.6; } .mono { white-space:pre-wrap; word-break:break-word; }
  .appr { border:1px solid var(--amber); border-radius:9px; padding:.6rem .65rem; margin-bottom:.6rem; background:color-mix(in srgb,var(--amber) 8%, Canvas); }
  .bar { height:5px; background:#8883; border-radius:3px; overflow:hidden; margin:.4rem 0; }
  .bar > i { display:block; height:100%; background:var(--amber); }
  .row { display:flex; gap:.4rem; }
  pre { white-space:pre-wrap; word-break:break-word; background:var(--surface-2); border:1px solid var(--line-soft);
        border-radius:7px; padding:.5rem .6rem; max-height:18rem; overflow:auto; font-family:var(--mono); font-size:.8rem; }
  input[type=text] { font:inherit; width:100%; padding:.2rem; }
  /* scope editor modal */
  .modal { position:fixed; inset:0; background:color-mix(in srgb, CanvasText 38%, transparent); backdrop-filter:blur(2px);
           display:flex; align-items:center; justify-content:center; z-index:50; }
  .modalbox { background:Canvas; color:CanvasText; border:1px solid var(--line-strong); border-radius:var(--radius-lg);
              width:min(48rem,94vw); max-height:88vh; display:flex; flex-direction:column; box-shadow:var(--shadow-pop); }
  .modalhead, .modalfoot { display:flex; align-items:center; gap:.5rem; padding:.6rem .85rem; }
  .modalhead { border-bottom:1px solid var(--line); } .modalfoot { border-top:1px solid var(--line); }
  #scopeList { overflow:auto; padding:.6rem .8rem; display:flex; flex-direction:column; gap:.7rem; }
  #scopeList:empty::after { content:""; }
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
  /* rule compiler (LLM classification) */
  .hl-compile { border:1px solid var(--blue); color:var(--blue); border-radius:6px; padding:.2rem .7rem; }
  .hl-cwrap { display:grid; gap:.3rem; margin:.2rem 0 .6rem; }
  .hl-crow { border:1px solid var(--line); border-radius:7px; padding:.4rem .55rem; }
  .hl-crow .top { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; }
  .hl-crow .rtext { flex:1; min-width:10rem; font-size:.82rem; }
  .hl-tag { font-size:.7rem; border-radius:999px; padding:.05rem .5rem; border:1px solid var(--line); white-space:nowrap; }
  .hl-tag.det { color:var(--green); border-color:var(--green); }
  .hl-tag.adv { color:#e3b341; border-color:#e3b341; }
  .hl-tag.unk { color:var(--muted); }
  .hl-regex { font-family:monospace; font-size:.74rem; background:var(--surface-2); border-radius:5px; padding:.12rem .4rem; display:inline-block; margin-top:.25rem; word-break:break-all; }
  /* compile result: left = input (semantic) rules · right = compiled verdict */
  .hl-split { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.25fr); gap:.35rem .55rem; align-items:start; }
  .hl-split .col-h { font-size:.74rem; opacity:.7; border-bottom:1px solid var(--line); padding-bottom:.2rem; }
  .hl-cin { font-size:.82rem; padding:.3rem .4rem; border:1px solid var(--line); border-radius:6px; background:var(--surface-2); }
  .hl-cout { padding:.3rem .4rem; border:1px solid var(--line); border-radius:6px; }
  .hl-cbar { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin:.6rem 0 .2rem; }
  .hl-cbar select, .hl-cbar input.t { padding:.2rem .35rem; border:1px solid var(--line); border-radius:5px; background:transparent; color:inherit; }
  .scopecards { display:grid; gap:.4rem; }
  .scopecard { border:1px solid var(--line); border-radius:7px; padding:.45rem .55rem; display:grid; gap:.25rem; }
  .scopecard b { font-size:.82rem; }
  .warn { color:var(--red); font-size:.76rem; margin:.2rem 0; }
  /* sidebar: each session = a [source] folder accordion → dropdown of the user's requests */
  #sessions { display:flex; flex-direction:column; }
  /* project group (folder) — sessions stack beneath it, Codex-style */
  .projgroup { border-radius:9px; margin-bottom:.12rem; }
  .projgroup.open { background:var(--surface-2); padding-bottom:.2rem; }
  .projhead { padding:.42rem .45rem .36rem; border-radius:9px; cursor:pointer; }
  .projhead:hover { background:var(--surface-2); }
  .projtop { display:flex; gap:.45rem; align-items:center; min-width:0; }
  .ico { display:inline-grid; place-items:center; color:var(--muted); flex:none; }
  .ico svg { display:block; }
  .projgroup.open .ico { color:var(--accent); }
  .pname { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; font-size:.88rem; letter-spacing:-.01em; }
  .projmeta { display:flex; align-items:center; gap:.4rem; margin:.28rem 0 0 1.55rem; }
  .cnt { font-size:.7rem; color:var(--muted); white-space:nowrap; }
  /* compact per-project mode pill (native select styled as a chip) */
  .modectl { display:inline-flex; }
  .modesel { font:inherit; font-size:.68rem; line-height:1.4; border-radius:var(--radius-sm); padding:.12rem 1.25rem .12rem .55rem;
    border:1px solid var(--line); color:var(--muted); cursor:pointer; -webkit-appearance:none; appearance:none;
    background:var(--surface) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='8' viewBox='0 0 8 8'%3E%3Cpath d='M1 2.5 4 5.5 7 2.5' stroke='%23999' fill='none' stroke-width='1.2'/%3E%3C/svg%3E") no-repeat right .45rem center; }
  .modesel:hover { border-color:var(--line-strong); }
  .modesel.enf { border-color:var(--amber); color:var(--amber); font-weight:700; background-color:color-mix(in srgb,var(--amber) 13%, Canvas); }
  .modesel.obs { color:var(--muted); }
  .projsessions { margin:.05rem 0 .1rem 1.1rem; padding-left:.5rem; border-left:1px solid var(--line); }
  .sessitem { padding:.04rem 0; }
  .sesshead { display:flex; gap:.4rem; align-items:center; padding:.27rem .4rem; border-radius:7px; cursor:pointer; }
  .sesshead:hover { background:var(--surface-2); } .sesshead.cur { background:color-mix(in srgb,var(--blue) 15%, Canvas); }
  .srctag { font-size:.6rem; border-radius:4px; padding:.07rem .4rem; color:#fff; font-weight:600; letter-spacing:.02em;
    text-transform:uppercase; white-space:nowrap; flex:none; }
  .srctag.claude { background:#d97706; } .srctag.codex { background:#2563eb; }
  .sname { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .smeta { font-size:.75rem; color:var(--muted); }
  /* delete-conversation button — hidden until the row is hovered */
  .convdel { flex:none; border:0; background:transparent; color:var(--muted); padding:.1rem .2rem; line-height:0;
             border-radius:5px; opacity:0; cursor:pointer; display:inline-grid; place-items:center; }
  .sesshead:hover .convdel { opacity:.6; }
  .convdel:hover { opacity:1; color:var(--red); background:color-mix(in srgb,var(--red) 14%, transparent); }
  /* conversation view — header once, then a collapsible section per turn (request) */
  .reqview { max-width:62rem; }
  .reqhead { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin-bottom:.3rem; }
  .reqask { padding:.5rem .7rem; border-left:3px solid var(--blue); background:var(--surface-2); border-radius:7px;
            white-space:pre-wrap; word-break:break-word; max-height:11rem; overflow:auto; margin:.2rem 0 .2rem; line-height:1.5; }
  .turnsec { border:1px solid var(--line); border-radius:10px; margin:.55rem 0; background:var(--surface); overflow:hidden; }
  .turnsec.open { border-color:var(--line-strong); }
  .turnhead { display:flex; gap:.5rem; align-items:center; padding:.5rem .65rem; cursor:pointer; }
  .turnhead:hover { background:var(--surface-2); }
  .turnsec.open .turnhead { border-bottom:1px solid var(--line); background:var(--surface-2); }
  .turnask { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; font-size:.88rem; }
  .turnbody { padding:.45rem .65rem .6rem; }
  /* full user request at the top of an opened turn — collapsible so a long prompt isn't truncated */
  .askblock { border:1px solid var(--line); border-left:3px solid var(--blue); border-radius:7px;
              background:var(--surface-2); margin:.1rem 0 .55rem; }
  .askhead { display:flex; align-items:center; gap:.4rem; padding:.3rem .5rem; cursor:pointer; font-size:.78rem; }
  .askhead:hover { background:var(--surface-2); }
  .asklabel { font-weight:600; color:var(--muted); letter-spacing:.02em; }
  .askhint { font-size:.7rem; }
  .asktext { padding:0 .6rem .45rem 1rem; white-space:pre-wrap; word-break:break-word; line-height:1.5; }
  .asktext.clamp { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; padding-bottom:.4rem; }
  /* (A) service-harness usage chip on a step; (B) 3-Layer decision chip */
  .uchip { border-color:var(--green); color:var(--green); opacity:1; cursor:pointer; }
  .uchip:hover { background:#22a35a22; }
  .catsec { margin:.1rem 0 .45rem; }
  .cathead { font-size:.78rem; font-weight:600; opacity:.82; margin:.35rem 0 .1rem; padding:.05rem .4rem; border-left:3px solid var(--blue); }
  .trajhead { display:flex; align-items:center; gap:.5rem; }
  .trajhead h2 { flex:1; margin-bottom:.2rem; }
  .trajbtn { flex:none; white-space:nowrap; font-size:.74rem; padding:.18rem .6rem; }
  button { white-space:nowrap; }
  .hp-title { font-weight:600; }
  .hp-title:hover { text-decoration:underline; }
  .prov { font-size:.66rem; border-radius:4px; padding:.02rem .3rem; border:1px solid var(--line); white-space:nowrap; }
  .prov.proj { color:var(--blue); border-color:var(--blue); }
  .prov.repo { color:#8b5cf6; border-color:#8b5cf6; font-weight:600; }
  .prov.glob { opacity:.55; }
  .chip.repo { border-color:#8b5cf6; color:#8b5cf6; }
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
  .dcmd { background:var(--surface-2); border-left:2px solid var(--blue); border-radius:5px; padding:.45rem .65rem; margin-bottom:.6rem;
          white-space:pre-wrap; word-break:break-word; min-height:18rem; max-height:min(34rem, 52vh);
          overflow:auto; font-size:.8rem; font-family:var(--mono); }
  .dcard { border:1px solid var(--line); border-radius:8px; padding:.4rem .55rem; margin-bottom:.6rem; }
  .dcard-h { font-size:.78rem; font-weight:600; opacity:.85; margin-bottom:.3rem; }
  .dcard-h.clickable { cursor:pointer; opacity:.72; margin-bottom:0; display:flex; align-items:center; gap:.25rem; }
  .dcard-h.clickable:hover { opacity:1; }
  .dcard pre { margin:.35rem 0 0; max-height:24rem; }
  .dlog pre { max-height:min(44rem, 66vh); }
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
  /* per-session context: Service harness + 3-Layer harness, side by side, collapsed by default */
  .hpanels { display:flex; flex-direction:column; gap:.5rem; margin:.5rem 0 .85rem; }
  .harnesspanel { border:1px solid var(--line); border-radius:var(--radius); background:var(--surface); min-width:0; }
  .hp-ico { color:var(--muted); flex:none; }
  .harnesspanel.service .hp-ico { color:var(--green); }
  .harnesspanel.layer .hp-ico { color:var(--accent); }
  .hp-name { font-weight:600; font-size:.82rem; white-space:nowrap; }
  .hp-chips { display:flex; gap:.35rem; flex-wrap:wrap; }
  .hp-acts { display:flex; gap:.4rem; flex-wrap:wrap; }
  .hl-compilebox { border:1px solid var(--line); border-radius:var(--radius); padding:.65rem .7rem; margin-bottom:.75rem; background:var(--surface); }
  /* compact top controls (run mode + rule compiler) so the Layer editors below are the focus */
  .hl-controls { display:flex; gap:.7rem; align-items:center; flex-wrap:wrap; padding:.5rem .65rem; border:1px solid var(--line);
    border-radius:var(--radius); background:var(--surface); margin-bottom:.3rem; }
  .hl-controls .ctl { display:flex; align-items:center; gap:.4rem; }
  .hl-controls .ctl label { font-size:.74rem; color:var(--muted); font-weight:600; white-space:nowrap; }
  .hl-controls select { padding:.2rem .45rem; border:1px solid var(--line); border-radius:var(--radius-sm); background:Canvas; color:inherit; font:inherit; }
  .hl-controls .sep { width:1px; align-self:stretch; background:var(--line); margin:0 .05rem; }
  .hl-secthead { font-size:.66rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); font-weight:700;
    margin:.7rem 0 .35rem; padding-bottom:.25rem; border-bottom:1px solid var(--line); }
  /* project-first 3-Layer editor: a project picker selects whose harness you edit */
  .hp-picker { display:flex; gap:.35rem; align-items:center; flex-wrap:wrap; margin:.1rem 0 .7rem; }
  .hp-proj { font-size:.8rem; padding:.18rem .6rem; border-radius:999px; }
  .hp-proj.cur { background:var(--blue); color:#fff; border-color:var(--blue); }
  .harnesspanel .hp-head { display:flex; gap:.4rem; align-items:center; flex-wrap:wrap; padding:.5rem .6rem; cursor:pointer; border-radius:var(--radius); }
  .harnesspanel .hp-head:hover { background:var(--surface-2); }
  .hp-body { padding:.05rem .6rem .6rem; display:grid; gap:.5rem; }
  .hp-body h4 { margin:.1rem 0; font-size:.75rem; opacity:.75; text-transform:uppercase; letter-spacing:.03em; }
  .hp-body .it { font-size:.82rem; margin:.12rem 0 .12rem .3rem; }
  @media (max-width: 900px) {
    main { grid-template-columns: 1fr; }
    .resize-handle { display:none; }
    aside, #detail { max-height:16rem; border:0; border-bottom:1px solid var(--line); }
    main.left-collapsed aside, main.right-collapsed #detail { max-height:3.4rem; }
    main.left-collapsed aside .panehead, main.right-collapsed #detail .panehead {
      flex-direction:row; align-items:center; gap:.45rem; padding:.15rem 0 .35rem;
    }
    main.left-collapsed aside h2, main.right-collapsed #detail h2 {
      writing-mode:horizontal-tb; transform:none; margin:0;
    }
    #canvas { grid-template-columns: 1fr; }
    .scopedcrow { grid-template-columns: 1fr; }
  }
  /* ===== monochrome line-icons + status dots (replaces all emoji) ===== */
  .ico-i { display:inline-flex; align-items:center; justify-content:center; vertical-align:-.12em; flex:none; color:inherit; }
  .ico-i svg { display:block; width:1em; height:1em; }
  .tname { display:inline-flex; align-items:center; gap:.34rem; white-space:nowrap; }
  .vmark { display:inline-flex; align-items:center; gap:.3rem; }
  .dot { width:.55rem; height:.55rem; }
  .badge .dot, #mode .dot { width:.5rem; height:.5rem; }
  #mode { display:inline-flex; align-items:center; gap:.35rem; }
  #mode.enf { color:var(--amber); border-color:color-mix(in srgb,var(--amber) 45%,var(--line));
    background:color-mix(in srgb,var(--amber) 9%, Canvas); }
  .langbtn { font-weight:600; min-width:2.3rem; text-align:center; display:inline-flex; align-items:center; gap:.3rem; justify-content:center; }
  /* ===== top bar minimal → harness/scope controls live in the sidebar footer ===== */
  #sidebar { display:flex; flex-direction:column; }
  #sessions { flex:1 1 auto; }
  .sidetop { padding:.15rem .15rem .5rem; }
  .sidetop button { display:inline-flex; align-items:center; gap:.4rem; justify-content:center; width:100%; font-weight:600; }
  main.left-collapsed .sidetop { display:none; }
  /* ===== harness settings modal — two-pane (target rail + editor) ===== */
  #harnessModal .modalbox { width:min(60rem,95vw); }
  #harnessBody.hl-shell { display:grid; grid-template-columns:13.5rem 1fr; min-height:0; flex:1; overflow:hidden; padding:0; }
  .hl-rail { border-right:1px solid var(--line); background:var(--surface); padding:.5rem; overflow:auto;
    display:flex; flex-direction:column; gap:.12rem; }
  .hl-railhead { font-size:.62rem; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); padding:.35rem .5rem .2rem; }
  .hl-tgt { display:flex; align-items:center; gap:.45rem; padding:.42rem .55rem; border-radius:var(--radius-sm); cursor:pointer;
    font-size:.82rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .hl-tgt:hover { background:var(--surface-2); }
  .hl-tgt.cur { background:var(--accent-weak); color:var(--blue); font-weight:600; }
  .hl-tgt .ico-i { color:var(--muted); } .hl-tgt.cur .ico-i { color:var(--blue); }
  .hl-tgt .nm { min-width:0; overflow:hidden; text-overflow:ellipsis; }
  .hl-content { padding:.8rem .95rem 1.1rem; overflow:auto; min-width:0; }
  .hl-content .hl-layer { background:Canvas; border-radius:var(--radius); padding:.75rem .85rem; }
  .hl-lhead { display:flex; align-items:center; gap:.5rem; flex-wrap:wrap; }
  .hl-lbadge { font-size:.6rem; font-weight:700; letter-spacing:.04em; border:1px solid var(--line-strong);
    border-radius:4px; padding:.06rem .36rem; color:var(--muted); }
  .hl-lbadge.l1 { color:var(--red); border-color:color-mix(in srgb,var(--red) 40%,var(--line)); }
  .hl-lbadge.l2 { color:var(--blue); border-color:color-mix(in srgb,var(--blue) 40%,var(--line)); }
  .hl-lbadge.l3 { color:var(--amber); border-color:color-mix(in srgb,var(--amber) 40%,var(--line)); }
  .hl-lbadge.mode { color:var(--green); border-color:color-mix(in srgb,var(--green) 40%,var(--line)); }
  .hl-ltitle { font-size:.85rem; font-weight:600; }
  .hl-lock { display:inline-flex; align-items:center; gap:.3rem; }
  .hl-adv { border:1px solid var(--line); border-radius:var(--radius); margin-top:.2rem; overflow:hidden; }
  .hl-adv > summary { cursor:pointer; padding:.5rem .7rem; font-size:.8rem; font-weight:600; color:var(--muted);
    list-style:none; display:flex; align-items:center; gap:.4rem; }
  .hl-adv > summary::-webkit-details-marker { display:none; }
  .hl-adv[open] > summary { border-bottom:1px solid var(--line); }
  .hl-adv[open] > summary .caret { transform:rotate(90deg); }
  .hl-advbody { padding:.6rem .7rem .7rem; }
  @media (max-width:760px){
    #harnessBody.hl-shell { grid-template-columns:1fr; }
    .hl-rail { border-right:0; border-bottom:1px solid var(--line); flex-direction:row; flex-wrap:wrap; }
  }
</style>
</head>
<body>
<header>
  <h1><span class="logo"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.3"><circle cx="12" cy="12" r="6.5"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/></svg></span>harness-lens<span class="tag">live</span></h1>
  <span class="grow"></span>
  <button id="mode"></button>
  <button id="lang" class="langbtn">EN</button>
  <span id="pending" class="badge" style="display:none"></span>
  <span id="conn" class="badge"></span>
</header>
<main id="layout">
  <aside id="sidebar">
    <div class="sidetop"><button id="harness"></button></div>
    <div class="panehead"><h2 id="sessTitle">세션</h2><button id="leftToggle" class="pane-toggle" aria-expanded="true"><svg style="transform:rotate(180deg)" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg></button></div>
    <div id="sessions"></div>
  </aside>
  <div id="leftResize" class="resize-handle" role="separator" aria-orientation="vertical" aria-label="세션 패널 폭 조절" title="드래그해 세션 패널 폭 조절"></div>
  <div id="canvas"></div>
  <div id="rightResize" class="resize-handle" role="separator" aria-orientation="vertical" aria-label="상세 패널 폭 조절" title="드래그해 상세 패널 폭 조절"></div>
  <div id="detail">
    <div class="panehead"><h2 id="detailTitle">상세</h2><button id="rightToggle" class="pane-toggle" aria-expanded="true"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg></button></div>
    <div id="detail-body" class="muted">노드를 선택하세요.</div>
  </div>
</main>
<div id="scopeModal" class="modal" style="display:none">
  <div class="modalbox">
    <div class="modalhead"><b id="scopeTitle">정책 스코프</b><span id="scopeSub" class="muted">프로젝트(cwd)/세션별로 전역 base 위에 덮어씀</span>
      <span class="grow"></span><button id="scopeAdd">+ 추가</button><button id="scopeClose" class="iconbtn"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg></button></div>
    <div id="scopeList"></div>
    <div class="modalfoot"><span id="scopeMsg" class="muted"></span><span class="grow"></span><button id="scopeSave">저장</button></div>
  </div>
</div>
<div id="harnessModal" class="modal" style="display:none">
  <div class="modalbox">
    <div class="modalhead"><b id="harnessTitle">하네스 (3-Layer)</b>
      <span class="hl-badge" id="harnessBadge">사람이 직접 편집 · AHE 자동진화는 Layer 3만</span>
      <span class="grow"></span><button id="harnessClose" class="iconbtn"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg></button></div>
    <div id="harnessBody" class="hl-shell"></div>
    <div class="modalfoot"><span id="harnessMsg" class="muted"></span></div>
  </div>
</div>
<script>
const TOKEN = "__HL_TOKEN__";
const H = { "X-HL-Token": TOKEN, "Content-Type": "application/json" };
const $ = s => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const api = (p, opt={}) => fetch(p, { ...opt, headers: { ...H, ...(opt.headers||{}) } });

// One disclosure chevron shape, reused everywhere instead of the old "→ / ↓ / ←" text arrows.
const CHEV_SVG = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>';
function chevron(){ const s = el("span","caret"); s.innerHTML = CHEV_SVG; return s; }       // rotated by an .open ancestor via CSS
function chevHTML(deg){ return '<svg style="transform:rotate('+deg+'deg)" viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4l4 4-4 4"/></svg>'; }
function setChev(btn, deg){ btn.innerHTML = chevHTML(deg); }                                // for buttons that point a specific way

// ===== i18n (KO / EN) — every visible string flows through t(); switchable at runtime =====
let LANG = (function(){ try { return localStorage.getItem("hl-lang") || ((navigator.language||"").toLowerCase().startsWith("ko") ? "ko" : "en"); } catch(_) { return "ko"; } })();
const I18N = {
 ko: {
  "mode.toggle":"모드 전환","mode.enforce":"enforce","mode.observe":"observe","mode.observeNote":"기록만","mode.enforceNote":"차단/승인",
  "conn.connected":"연결됨","conn.disconnected":"끊김","lang.switch":"언어 / Language",
  "pane.sessions":"세션","pane.detail":"상세",
  "pane.sessions.collapse":"세션 패널 접기","pane.sessions.expand":"세션 패널 펼치기",
  "pane.detail.collapse":"상세 패널 접기","pane.detail.expand":"상세 패널 펼치기",
  "pane.sessions.resize":"세션 패널 폭 조절","pane.detail.resize":"상세 패널 폭 조절","pane.resize.drag":"드래그해 패널 폭 조절",
  "detail.empty":"노드를 선택하세요.",
  "footer.harness":"하네스","footer.harness.tip":"전역 base와 프로젝트별 하네스 보기",
  "footer.scopes":"프로젝트 하네스","footer.scopes.tip":"프로젝트/세션별 하네스 오버레이",
  "common.loading":"불러오는 중…","common.save":"저장","common.saving":"저장 중…","common.close":"닫기","common.delete":"삭제",
  "common.none":"(없음)","common.add":"추가","common.toggle":"접기/펼치기","common.input":"입력","common.output":"출력",
  "common.failPrefix":"실패: ","common.errorPrefix":"오류: ","common.loadFail":"불러오기 실패",
  "unit.sessions":"세션","unit.turns":"턴","unit.steps":"단계",
  "time.justNow":"방금","time.minAgo":"분 전","time.hourAgo":"시간 전","time.dayAgo":"일 전",
  "status.running":"실행 중","status.completed":"완료","status.failed":"실패","status.aborted":"중단","status.pending_approval":"승인 대기",
  "sidebar.empty":"아직 추적된 세션이 없습니다.","mode.global":"전역",
  "tip.projPinned":"이 프로젝트 고정: ","tip.projFollowsGlobal":"전역 모드 따름","tip.deleteConv":"이 대화 삭제",
  "confirm.deleteConv":"이 대화를 삭제할까요?","cwd.none":"cwd 미관측",
  "canvas.emptyNoSessions":"아직 추적된 세션이 없습니다 — Claude Code / Codex 에서 작업하면 대화가 여기에 나타납니다.",
  "canvas.selectConv":"왼쪽에서 대화를 선택하세요.","canvas.loadingConv":"대화를 불러오는 중…",
  "traj.heading":"동작 내역 — 턴(요청)별","traj.time":"시간순","traj.category":"카테고리","traj.toggleTip":"보기 전환: 시간순 ↔ 카테고리별",
  "traj.noActions":"아직 동작이 없습니다.","ask.label":"요청","ask.collapse":"접기","ask.expandAll":"전체 보기","req.none":"(요청 미관측)",
  "cat.search":"탐색·읽기","cat.impl":"구현·수정","cat.verify":"테스트·검증","cat.external":"외부도구","cat.run":"실행·기타","cat.agent":"서브에이전트","cat.other":"기타",
  "hp.title":"3-Layer 하네스","hp.titleTip":"클릭해 적용 규칙 보기","hp.genDetector":"생성 detector",
  "hp.genDetector.tip":"컴파일러가 만든 검증된 정규식 detector — 매칭 시 hook 에서 escalate(승인 큐)",
  "hp.repoPolicy":"repo 정책","hp.repoPolicy.tip":"이 repo 의 .harness-lens/policy.yaml — git 으로 팀과 공유되는 거버넌스 (PR 로 편집)",
  "hp.l3Breach":"L3 임계 초과","hp.l3Breach.tip":"Layer 3 한계선 초과 — 차단은 아니고 경보/자동진화 트리거 대상",
  "hp.l3.fail":"실패","hp.l3.slow":"지연","hp.l3.lowq":"저품질",
  "hp.detailTip":"적용 규칙 자세히 보기","hp.editProject":"이 프로젝트 하네스 편집","hp.globalBase":"전역 기본",
  "hp.effective":"적용 하네스","hp.projRules":"이 프로젝트 규칙","hp.override":"오버라이드","hp.firedThisSession":"이 세션에서 걸림",
  "prov.repo":"repo","prov.proj":"이 프로젝트","prov.glob":"전역",
  "layer1.full":"Layer 1 — 절대 규칙 (안전·위반 금지)","layer1.short":"절대 규칙",
  "layer2.full":"Layer 2 — 행동 기준 (매 단계 품질 심사)","layer2.short":"행동 기준",
  "layer3.full":"Layer 3 — 품질 한계선 (자동 임계값)","layer3.short":"품질 한계선",
  "svc.title":"서비스 하네스 (스캐폴딩)","svc.scanning":"스캐폴딩 스캔 중…","svc.none":"감지된 서비스 스캐폴딩이 없습니다.",
  "svc.aheEditable":"AHE 편집가능","svc.chipTip":"클릭 — {0} 프롬프트 보기: {1}",
  "lyr.tip":"규칙·판정 보기","verdict.deny":"차단","verdict.escalate":"승인대기","verdict.pass":"통과",
  "appr.waiting":"대기","appr.waitingFor":"승인 대기 — ","appr.approve":"승인","appr.deny":"거부","appr.denyReason":"거부 사유(선택)","appr.pendingBadge":"승인 대기 ",
  "detail.evalTitle":"3-Layer 평가","detail.firedRule":"걸린 규칙","fired.deny":"걸림","fired.escalate":"걸림",
  "verdict.passNoViol":"통과 (위반 없음)","verdict.l3monitor":"런 전체 모니터링 (개별 차단 아님)","verdict.dcStruct":"구조 검사 통과 · 자연어 기준은 비동기 Judge 채점",
  "detail.svcThisAction":"서비스 하네스 (이 동작)","detail.chipClickPrompt":"칩 클릭 → 실제 프롬프트","detail.noScaffold":"감지된 스캐폴딩 없음 (일반 셸/툴 동작)",
  "detail.svcPrefix":"서비스 하네스","detail.noPrompt":"프롬프트 내용을 찾지 못했습니다.",
  "scope.title":"정책 스코프","scope.subtitle":"프로젝트(cwd)/세션별로 전역 base 위에 덮어씀","scope.empty":"스코프가 없습니다. '+ 추가'로 만드세요.",
  "scope.name":"이름","scope.matchCwd":"cwd 경로","scope.matchSession":"세션 ID","scope.matchPlaceholder":"/path/to/project 또는 세션 ID","scope.modeInherit":"mode: 상속",
  "scope.addInvPlaceholder":"추가 invariant (한 줄에 하나, 전역 규칙 위에 가산)","scope.addL2":"추가 L2","scope.addCriterion":"+ 기준","scope.addL2Placeholder":"추가 Layer-2 기준",
  "scope.note":"전역 base는 그대로 적용되고, 이 scope의 항목만 추가/오버라이드됩니다.",
  "scope.msgDraft":"현재 Flow의 cwd로 새 프로젝트 하네스 초안을 추가했습니다.","scope.msgMatched":"현재 Flow에 매칭되는 프로젝트 하네스가 목록에 있습니다.",
  "scope.saved":"저장됨 — {0}개 스코프 적용 (즉시 반영)","scope.saveFail":"저장 실패 ({0})","scope.matchLabel":"match",
  "harness.title":"하네스 (3-Layer)","harness.badge":"사람이 직접 편집 · AHE 자동진화는 Layer 3만",
  "harness.savedImmediate":"저장됨 · 다음 단계부터 즉시 적용","harness.rejected":"거부: ","harness.projects":"대상",
  "harness.globalBase":"전역 기본","harness.globalBaseTip":"모든 프로젝트 공통","harness.globalIntro":"전역 기본 — 모든 프로젝트에 공통으로 적용되는 3-Layer 입니다.","harness.layersSection":"레이어 편집",
  "harness.l1Locked":"잠금 — 클릭해 편집","harness.l1Unlocked":"편집 중 — 잠그기","harness.l1Warn":"안전 규칙입니다. 저장 시 criteria.yaml 백업 후 즉시 반영됩니다.",
  "harness.rulePlaceholder":"규칙 내용","harness.addRule":"+ 규칙 추가","harness.criterionPlaceholder":"기준 설명","harness.addCriterion":"+ 기준 추가",
  "harness.l1Help":"어떤 경우에도 어기면 안 되는 규칙. AHE 자동진화는 절대 못 바꿉니다.","harness.l2Help":"LLM 심사관이 매 단계 점수를 매기는 기준. weight = 중요도.",
  "harness.l3Help":"넘으면 경보/자동진화. AHE가 자동으로 조정하는 유일한 층.",
  "harness.projIntro":"{0} — 전역 기본은 그대로 적용되고, 여기서는 이 폴더에만 더할 규칙·한계선을 정합니다.","harness.projMatch":"정확한 폴더 경로로 매칭: {0}",
  "harness.modeTitle":"실행 모드","harness.modeHelp":"이 폴더 세션만 observe/enforce 로 고정합니다. 비우면 전역 모드를 따릅니다.",
  "harness.modeInherit":"전역 모드 상속","harness.modeObserve":"observe (관측만)","harness.modeEnforce":"enforce (차단/승인)",
  "harness.l1ProjHelp":"전역 규칙은 그대로 적용됩니다. 이 폴더 전용 규칙만 여기서 더하세요.","harness.l2ProjHelp":"전역 기준 위에 이 폴더 전용 기준을 더합니다.",
  "harness.l3ProjHelp":"비우면 전역 값을 그대로 씁니다. 값을 넣으면 이 폴더만 그 값으로 덮어씁니다.",
  "harness.addFolderRule":"+ 이 폴더 규칙","harness.folderRulePlaceholder":"이 폴더 전용 규칙","harness.addFolderCriterion":"+ 이 폴더 기준","harness.folderCriterionPlaceholder":"이 폴더 전용 기준",
  "harness.saveProject":"이 프로젝트 저장","harness.savedProject":"저장됨 · {0} 의 다음 단계부터 적용","harness.saveFail":"저장 실패: ","harness.globalTag":"전역",
  "compile.section":"고급 — 규칙 컴파일 (LLM 분류)","compile.btn":"규칙 컴파일 (LLM 분류)",
  "compile.btnTip":"각 규칙이 결정론 detector로 강제 가능한지, semantic(→advisory/CI/Judge)인지 LLM이 분류합니다",
  "compile.hookNote":"훅은 LLM을 못 부르므로 데몬이 claude -p / codex exec 로 대신 호출 — 세션과 무관.",
  "compile.classifying":"분류 중… (LLM 호출)","compile.calling":"LLM 호출 중 — 호스트 CLI(claude/codex) 응답을 기다립니다… (수초 소요)",
  "compile.fail":"컴파일 실패: ","compile.error":"컴파일 요청 오류: ",
  "compile.tag.det":"결정론 detector","compile.tag.unk":"미분류","compile.tag.adv":"semantic → advisory",
  "compile.summary":"분류 결과 — detector {0} · advisory {1} / 총 {2}","compile.llmUsed":" · LLM 사용됨","compile.builtinOnly":" · 내장 매칭만",
  "compile.colInput":"입력 (semantic 하네스)","compile.colOutput":"컴파일 결과","compile.builtin":"내장 detector: ",
  "compile.verifyOk":" ✓ 예시검증 통과","compile.verifyFail":" ✗ 예시검증 실패 → advisory 유지",
  "compile.exportYaml":"YAML 내보내기","compile.toProject":"→ 프로젝트:","compile.pathPlaceholder":"프로젝트 경로 (예: /Users/me/work/app)","compile.toPath":"→ 경로:",
  "compile.import":"이 프로젝트로 import","compile.importTip":"<프로젝트>/.harness-lens/policy.yaml 로 커밋(팀 공유). compiled 주석도 함께 기록됩니다.",
  "compile.needPath":"프로젝트 경로가 필요합니다","compile.writing":"쓰는 중…","compile.savedTo":"저장됨: ",
  "l3help.retry_threshold":"한 단계 재시도 허용 횟수 — 넘으면 경보","l3help.latency_multiplier":"평소 대비 몇 배 느리면 경보",
  "l3help.failure_count_trigger":"연속 실패 몇 번에 경보/진화","l3help.quality_threshold":"Layer-2 품질 점수 하한 (0~1)",
 },
 en: {
  "mode.toggle":"Toggle mode","mode.enforce":"enforce","mode.observe":"observe","mode.observeNote":"log only","mode.enforceNote":"block/approve",
  "conn.connected":"connected","conn.disconnected":"disconnected","lang.switch":"Language / 언어",
  "pane.sessions":"Sessions","pane.detail":"Detail",
  "pane.sessions.collapse":"Collapse sessions panel","pane.sessions.expand":"Expand sessions panel",
  "pane.detail.collapse":"Collapse detail panel","pane.detail.expand":"Expand detail panel",
  "pane.sessions.resize":"Resize sessions panel","pane.detail.resize":"Resize detail panel","pane.resize.drag":"Drag to resize panel",
  "detail.empty":"Select a node.",
  "footer.harness":"Harness","footer.harness.tip":"View global base + per-project harness",
  "footer.scopes":"Project harness","footer.scopes.tip":"Per-project / per-session harness overlay",
  "common.loading":"Loading…","common.save":"Save","common.saving":"Saving…","common.close":"Close","common.delete":"Delete",
  "common.none":"(none)","common.add":"Add","common.toggle":"Collapse / expand","common.input":"Input","common.output":"Output",
  "common.failPrefix":"Failed: ","common.errorPrefix":"Error: ","common.loadFail":"Failed to load",
  "unit.sessions":"sessions","unit.turns":"turns","unit.steps":"steps",
  "time.justNow":"just now","time.minAgo":"m ago","time.hourAgo":"h ago","time.dayAgo":"d ago",
  "status.running":"running","status.completed":"completed","status.failed":"failed","status.aborted":"aborted","status.pending_approval":"pending approval",
  "sidebar.empty":"No tracked sessions yet.","mode.global":"global",
  "tip.projPinned":"Pinned for this project: ","tip.projFollowsGlobal":"Follows global mode","tip.deleteConv":"Delete this conversation",
  "confirm.deleteConv":"Delete this conversation?","cwd.none":"cwd unknown",
  "canvas.emptyNoSessions":"No tracked sessions yet — work in Claude Code / Codex and conversations show up here.",
  "canvas.selectConv":"Select a conversation on the left.","canvas.loadingConv":"Loading conversation…",
  "traj.heading":"Activity — by turn (request)","traj.time":"Chronological","traj.category":"By category","traj.toggleTip":"Toggle view: chronological ↔ by category",
  "traj.noActions":"No activity yet.","ask.label":"Request","ask.collapse":"Collapse","ask.expandAll":"Show all","req.none":"(request not observed)",
  "cat.search":"Search · Read","cat.impl":"Implement · Edit","cat.verify":"Test · Verify","cat.external":"External tools","cat.run":"Run · Misc","cat.agent":"Sub-agents","cat.other":"Other",
  "hp.title":"3-Layer harness","hp.titleTip":"Click to view active rules","hp.genDetector":"generated detectors",
  "hp.genDetector.tip":"Verified regex detectors built by the compiler — on match the hook escalates (approval queue)",
  "hp.repoPolicy":"repo policy","hp.repoPolicy.tip":"This repo's .harness-lens/policy.yaml — governance shared with the team via git (edit by PR)",
  "hp.l3Breach":"L3 threshold exceeded","hp.l3Breach.tip":"Layer 3 limit exceeded — not a block; triggers alerts / auto-evolution",
  "hp.l3.fail":"failures","hp.l3.slow":"slow","hp.l3.lowq":"low-quality",
  "hp.detailTip":"View applied rules in detail","hp.editProject":"Edit this project's harness","hp.globalBase":"Global base",
  "hp.effective":"Effective harness","hp.projRules":"this project's rules","hp.override":"override","hp.firedThisSession":"fired in this session",
  "prov.repo":"repo","prov.proj":"this project","prov.glob":"global",
  "layer1.full":"Layer 1 — Invariants (safety · never violate)","layer1.short":"Invariants",
  "layer2.full":"Layer 2 — Domain criteria (per-step quality review)","layer2.short":"Domain criteria",
  "layer3.full":"Layer 3 — QA thresholds (automatic limits)","layer3.short":"QA thresholds",
  "svc.title":"Service harness (scaffolding)","svc.scanning":"Scanning scaffolding…","svc.none":"No service scaffolding detected.",
  "svc.aheEditable":"AHE-editable","svc.chipTip":"Click — view {0} prompt: {1}",
  "lyr.tip":"View rules · verdict","verdict.deny":"blocked","verdict.escalate":"awaiting approval","verdict.pass":"passed",
  "appr.waiting":"waiting","appr.waitingFor":"Awaiting approval — ","appr.approve":"Approve","appr.deny":"Deny","appr.denyReason":"Reason for denial (optional)","appr.pendingBadge":"Pending approvals ",
  "detail.evalTitle":"3-Layer evaluation","detail.firedRule":"fired rule","fired.deny":"fired","fired.escalate":"fired",
  "verdict.passNoViol":"Passed (no violation)","verdict.l3monitor":"Run-wide monitoring (not a per-step block)","verdict.dcStruct":"Structural check passed · NL criteria scored async by the Judge",
  "detail.svcThisAction":"Service harness (this action)","detail.chipClickPrompt":"Click a chip → actual prompt","detail.noScaffold":"No scaffolding detected (plain shell/tool action)",
  "detail.svcPrefix":"Service harness","detail.noPrompt":"Could not find the prompt content.",
  "scope.title":"Policy scopes","scope.subtitle":"Override the global base per project (cwd) / session","scope.empty":"No scopes. Create one with '+ Add'.",
  "scope.name":"Name","scope.matchCwd":"cwd path","scope.matchSession":"Session ID","scope.matchPlaceholder":"/path/to/project or session ID","scope.modeInherit":"mode: inherit",
  "scope.addInvPlaceholder":"Extra invariants (one per line, added on top of global rules)","scope.addL2":"Extra L2","scope.addCriterion":"+ Criterion","scope.addL2Placeholder":"Extra Layer-2 criterion",
  "scope.note":"The global base still applies; only this scope's items are added/overridden.",
  "scope.msgDraft":"Added a new project-harness draft for the current Flow's cwd.","scope.msgMatched":"A project harness matching the current Flow is in the list.",
  "scope.saved":"Saved — {0} scope(s) applied (effective immediately)","scope.saveFail":"Save failed ({0})","scope.matchLabel":"match",
  "harness.title":"Harness (3-Layer)","harness.badge":"Human-edited · AHE auto-evolves Layer 3 only",
  "harness.savedImmediate":"Saved · effective from the next step","harness.rejected":"Rejected: ","harness.projects":"Targets",
  "harness.globalBase":"Global base","harness.globalBaseTip":"Shared by all projects","harness.globalIntro":"Global base — the 3-Layer harness applied to every project.","harness.layersSection":"Layer editing",
  "harness.l1Locked":"Locked — click to edit","harness.l1Unlocked":"Editing — lock","harness.l1Warn":"Safety rules. On save, criteria.yaml is backed up and applied immediately.",
  "harness.rulePlaceholder":"Rule text","harness.addRule":"+ Add rule","harness.criterionPlaceholder":"Criterion description","harness.addCriterion":"+ Add criterion",
  "harness.l1Help":"Rules that must never be broken. AHE auto-evolution can never change them.","harness.l2Help":"Criteria an LLM judge scores at every step. weight = importance.",
  "harness.l3Help":"Crossing these alerts / triggers evolution. The only layer AHE adjusts automatically.",
  "harness.projIntro":"{0} — the global base still applies; here you set rules/limits added for this folder only.","harness.projMatch":"Matched by exact folder path: {0}",
  "harness.modeTitle":"Run mode","harness.modeHelp":"Pin only this folder's sessions to observe/enforce. Leave blank to follow the global mode.",
  "harness.modeInherit":"Inherit global mode","harness.modeObserve":"observe (monitor only)","harness.modeEnforce":"enforce (block/approve)",
  "harness.l1ProjHelp":"Global rules still apply. Add only this folder's own rules here.","harness.l2ProjHelp":"Add this folder's own criteria on top of the global ones.",
  "harness.l3ProjHelp":"Blank = use the global value. Set a value to override it for this folder only.",
  "harness.addFolderRule":"+ Folder rule","harness.folderRulePlaceholder":"Rule for this folder only","harness.addFolderCriterion":"+ Folder criterion","harness.folderCriterionPlaceholder":"Criterion for this folder only",
  "harness.saveProject":"Save this project","harness.savedProject":"Saved · effective for {0} from the next step","harness.saveFail":"Save failed: ","harness.globalTag":"global",
  "compile.section":"Advanced — rule compiler (LLM classification)","compile.btn":"Compile rules (LLM classify)",
  "compile.btnTip":"Classifies whether each rule is enforceable as a deterministic detector or is semantic (→advisory/CI/Judge)",
  "compile.hookNote":"Hooks can't call an LLM, so the daemon calls claude -p / codex exec instead — independent of any session.",
  "compile.classifying":"Classifying… (LLM call)","compile.calling":"Calling the LLM — waiting for the host CLI (claude/codex)… (a few seconds)",
  "compile.fail":"Compile failed: ","compile.error":"Compile request error: ",
  "compile.tag.det":"deterministic detector","compile.tag.unk":"unclassified","compile.tag.adv":"semantic → advisory",
  "compile.summary":"Result — detector {0} · advisory {1} / total {2}","compile.llmUsed":" · LLM used","compile.builtinOnly":" · built-in matching only",
  "compile.colInput":"Input (semantic harness)","compile.colOutput":"Compiled result","compile.builtin":"built-in detector: ",
  "compile.verifyOk":" ✓ example-check passed","compile.verifyFail":" ✗ example-check failed → kept advisory",
  "compile.exportYaml":"Export YAML","compile.toProject":"→ Project:","compile.pathPlaceholder":"Project path (e.g. /Users/me/work/app)","compile.toPath":"→ Path:",
  "compile.import":"Import into this project","compile.importTip":"Commits to <project>/.harness-lens/policy.yaml (team-shared). The compiled annotation is recorded too.",
  "compile.needPath":"A project path is required","compile.writing":"Writing…","compile.savedTo":"Saved: ",
  "l3help.retry_threshold":"Retries allowed per step — alert when exceeded","l3help.latency_multiplier":"How many× slower than usual triggers an alert",
  "l3help.failure_count_trigger":"Consecutive failures that trigger an alert/evolution","l3help.quality_threshold":"Lower bound for the Layer-2 quality score (0–1)",
 },
};
function t(k){ const d = I18N[LANG] || I18N.ko; let s = (k in d) ? d[k] : (k in I18N.ko ? I18N.ko[k] : k);
  for (let i=1;i<arguments.length;i++) s = s.replace("{"+(i-1)+"}", arguments[i]); return s; }
function statusLabel(s){ return (("status."+s) in I18N[LANG] || ("status."+s) in I18N.ko) ? t("status."+s) : (s||""); }
const tr = t;  // alias: use inside functions whose parameter is named `t` (the task), where `t` is shadowed
function qty(n, key){ return LANG === "ko" ? (n + t(key)) : (n + " " + t(key)); }  // "4턴" / "4 turns"

// ===== monochrome line-icons (one set, currentColor, sized 1em) — replaces every emoji =====
const ICONS = {
  dot:'<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>',
  search:'<circle cx="11" cy="11" r="6"/><path d="M20 20l-3.6-3.6"/>',
  terminal:'<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 9.5l3 2.5-3 2.5M12.5 14.5h4.5"/>',
  edit:'<path d="M4 20h4l10-10-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  book:'<path d="M5 4h11a1 1 0 0 1 1 1v15H6a1 1 0 0 1-1-1z"/><path d="M5 17h12"/>',
  globe:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3c2.7 2.6 2.7 15.4 0 18M12 3c-2.7 2.6-2.7 15.4 0 18"/>',
  cpu:'<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 2v3M14.5 2v3M9.5 19v3M14.5 19v3M2 9.5h3M2 14.5h3M19 9.5h3M19 14.5h3"/>',
  list:'<path d="M8.5 6h12M8.5 12h12M8.5 18h12"/><path d="M4 6h.01M4 12h.01M4 18h.01"/>',
  check:'<path d="M20 6.5L9.5 17 4 11.5"/>',
  puzzle:'<path d="M14 3a2 2 0 0 0-4 0c0 .6-.5 1-1 1H6a1 1 0 0 0-1 1v3c0 .5-.4 1-1 1a2 2 0 0 0 0 4c.6 0 1 .5 1 1v3a1 1 0 0 0 1 1h3c.5 0 1-.4 1-1a2 2 0 0 1 4 0c0 .6.5 1 1 1h3a1 1 0 0 0 1-1v-3c0-.5.4-1 1-1a2 2 0 0 0 0-4c-.6 0-1-.5-1-1V5a1 1 0 0 0-1-1h-3c-.5 0-1-.4-1-1z"/>',
  command:'<rect x="4" y="4.5" width="16" height="15" rx="2.5"/><path d="M9 14l3-5"/>',
  route:'<circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><path d="M6.5 9v4.5a3 3 0 0 0 3 3H15"/>',
  plug:'<path d="M9 3v5M15 3v5"/><path d="M7 8h10v3a5 5 0 0 1-10 0z"/><path d="M12 16v5"/>',
  file:'<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
  box:'<path d="M21 7.5l-9-4.5-9 4.5v9l9 4.5 9-4.5z"/><path d="M3 7.5l9 4.5 9-4.5M12 12v9.5"/>',
  scroll:'<path d="M7 4h9a2 2 0 0 1 2 2v11a3 3 0 0 0 3 3H9a2 2 0 0 1-2-2z"/><path d="M7 4a2 2 0 0 0-2 2v2.5h2"/>',
  folder:'<path d="M3 7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1 1a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v7.2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  trash:'<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>',
  shield:'<path d="M12 3l7 3v5.5c0 4.2-3 7.4-7 8.5-4-1.1-7-4.3-7-8.5V6z"/>',
  package:'<path d="M21 7.5l-9-4.5-9 4.5v9l9 4.5 9-4.5z"/><path d="M3 7.5l9 4.5 9-4.5M12 12v9.5M7.5 5.2l9 4.6"/>',
  gear:'<circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v2.4M12 19.1v2.4M21.5 12h-2.4M4.9 12H2.5M18.7 5.3l-1.7 1.7M7 17l-1.7 1.7M18.7 18.7L17 17M7 7L5.3 5.3"/>',
  lock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 8 0V11"/>',
  unlock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V7.5a4 4 0 0 1 7.7-1.5"/>',
  retry:'<path d="M21 12a9 9 0 1 1-2.6-6.3"/><path d="M21 4v5h-5"/>',
  alert:'<path d="M10.3 4l-7.5 13A1.5 1.5 0 0 0 4 19.3h16a1.5 1.5 0 0 0 1.3-2.3l-7.6-13a1.5 1.5 0 0 0-2.6 0z"/><path d="M12 9.5v4M12 16.4h.01"/>',
  plus:'<path d="M12 5.5v13M5.5 12h13"/>',
  x:'<path d="M5.5 5.5l13 13M18.5 5.5l-13 13"/>',
  lang:'<path d="M3 5.5h8M7 3.5v2c0 3.5-1.8 6.3-4 8M4.5 9c.9 2.2 2.7 3.8 5 5"/><path d="M13 19l3.5-9 3.5 9M14.4 16h4.2"/>',
  tool:'<circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/>',
};
function svgStr(name){ const p = ICONS[name] || ICONS.tool;
  return '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+p+'</svg>'; }
function ico(name, cls){ const s = el("span", "ico-i" + (cls ? " "+cls : "")); s.innerHTML = svgStr(name); return s; }
function dotEl(color){ const d = el("span","dot"); if (color) d.style.background = color; return d; }

const LAYOUT_KEY = "harness-lens-live-layout";
const layoutState = { leftW:264, rightW:384, leftCollapsed:false, rightCollapsed:false };
const COLLAPSED_PANE_W = 42;
function clamp(n, min, max){ return Math.max(min, Math.min(max, n)); }
function panelMax(side){
  const vw = window.innerWidth || document.documentElement.clientWidth || 1200;
  const floor = side === "left" ? 176 : 288;
  if (vw <= 900) return side === "left" ? 560 : 820;
  const other = side === "left"
    ? (layoutState.rightCollapsed ? COLLAPSED_PANE_W : layoutState.rightW)
    : (layoutState.leftCollapsed ? COLLAPSED_PANE_W : layoutState.leftW);
  const centerMin = 320, handles = 12;
  const viewportMax = vw - other - centerMin - handles;
  const hardMax = side === "left" ? 560 : 820;
  return Math.max(floor, Math.min(hardMax, viewportMax));
}
function restoreLayout(){
  try {
    const saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    if (saved && typeof saved === "object") Object.assign(layoutState, {
      leftW: Number.isFinite(saved.leftW) ? saved.leftW : layoutState.leftW,
      rightW: Number.isFinite(saved.rightW) ? saved.rightW : layoutState.rightW,
      leftCollapsed: !!saved.leftCollapsed,
      rightCollapsed: !!saved.rightCollapsed,
    });
  } catch (_) {}
}
function saveLayout(){
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutState)); } catch (_) {}
}
function applyLayout(){
  const root = $("#layout"); if (!root) return;
  if (!layoutState.leftCollapsed) layoutState.leftW = clamp(layoutState.leftW, 176, panelMax("left"));
  if (!layoutState.rightCollapsed) layoutState.rightW = clamp(layoutState.rightW, 288, panelMax("right"));
  root.style.setProperty("--left-w", layoutState.leftCollapsed ? "2.65rem" : layoutState.leftW + "px");
  root.style.setProperty("--right-w", layoutState.rightCollapsed ? "2.65rem" : layoutState.rightW + "px");
  root.classList.toggle("left-collapsed", layoutState.leftCollapsed);
  root.classList.toggle("right-collapsed", layoutState.rightCollapsed);
  const left = $("#leftToggle"), right = $("#rightToggle");
  if (left) {
    setChev(left, layoutState.leftCollapsed ? 0 : 180);   // collapsed → points right (expand); open → points left (collapse)
    left.title = t(layoutState.leftCollapsed ? "pane.sessions.expand" : "pane.sessions.collapse");
    left.setAttribute("aria-expanded", String(!layoutState.leftCollapsed));
  }
  if (right) {
    setChev(right, layoutState.rightCollapsed ? 180 : 0);  // collapsed → points left (expand); open → points right (collapse)
    right.title = t(layoutState.rightCollapsed ? "pane.detail.expand" : "pane.detail.collapse");
    right.setAttribute("aria-expanded", String(!layoutState.rightCollapsed));
  }
}
function togglePane(side){
  if (side === "left") layoutState.leftCollapsed = !layoutState.leftCollapsed;
  else layoutState.rightCollapsed = !layoutState.rightCollapsed;
  applyLayout(); saveLayout();
}
function startPanelResize(ev, side){
  if (ev.button != null && ev.button !== 0) return;
  if ((side === "left" && layoutState.leftCollapsed) || (side === "right" && layoutState.rightCollapsed)) return;
  ev.preventDefault();
  const startX = ev.clientX;
  const startW = side === "left" ? layoutState.leftW : layoutState.rightW;
  const handle = ev.currentTarget;
  handle.classList.add("dragging");
  if (handle.setPointerCapture) handle.setPointerCapture(ev.pointerId);
  const move = e => {
    const dx = e.clientX - startX;
    if (side === "left") layoutState.leftW = clamp(startW + dx, 176, panelMax("left"));
    else layoutState.rightW = clamp(startW - dx, 288, panelMax("right"));
    applyLayout();
  };
  const up = () => {
    handle.classList.remove("dragging");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    saveLayout();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up, { once:true });
}
function initLayout(){
  restoreLayout(); applyLayout();
  $("#leftToggle").onclick = () => togglePane("left");
  $("#rightToggle").onclick = () => togglePane("right");
  $("#leftResize").addEventListener("pointerdown", ev => startPanelResize(ev, "left"));
  $("#rightResize").addEventListener("pointerdown", ev => startPanelResize(ev, "right"));
  window.addEventListener("resize", applyLayout);
}

const state = { flows:{}, tasks:{}, steps:{}, approvals:{}, effective:{}, serviceHarness:{},
  expandedProjects:new Set(), expandedTurns:new Set(), expandedAsks:new Set(), loaded:new Set(), scopes:[],
  selFlow:null, sel:null,
  mode:"observe", connected:false, snapRev:0, trajMode:"time" };
const SRC_NAME = { claude_code:"claude", codex:"codex" };

// Each tool maps to a monochrome line-icon (see ICONS) so a step reads as an action, not a bare name.
const TOOL_ICON = { Read:"book", Glob:"search", Grep:"search", Bash:"terminal", Write:"edit", Edit:"edit", MultiEdit:"edit",
  NotebookEdit:"edit", apply_patch:"edit", WebSearch:"globe", WebFetch:"globe", Task:"cpu", Agent:"cpu", TodoWrite:"list", Skill:"puzzle", SlashCommand:"command" };
function toolIconName(name){ if(!name) return "dot"; if(name.startsWith("mcp__")) return "plug"; return TOOL_ICON[name] || "tool"; }
function toolLabel(name){ return name && name.startsWith("mcp__") ? name.replace(/^mcp__/,"").replace(/__/g," · ") : (name||"?"); }
function toolNameEl(name){ const sp = el("span","tname"); sp.append(ico(toolIconName(name)), document.createTextNode(" " + toolLabel(name))); return sp; }
// Service-harness (scaffolding) a step exercised — surfaced as chips so a sea of "Bash" steps
// still shows which skill/workflow/rule/MCP/instruction governed each action.
const USAGE_ICON = { skill:"puzzle", command:"command", workflow:"route", invoke:"puzzle", mcp:"plug", mcp_config:"plug",
  instruction:"file", cursor_rule:"book", plugin:"box", import:"file", script:"scroll" };
function usageLabel(u){ return (u.kind==="mcp" ? toolLabel(u.name) : u.name); }
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
  const c = el("span","chip uchip");
  c.append(ico(USAGE_ICON[u.kind]||"tool"), document.createTextNode(" " + usageLabel(u)));
  c.title = t("svc.chipTip", u.kind, u.name);
  c.onclick = (ev) => { ev.stopPropagation(); state.sel = null; showComponent(u.kind, u.name, cwd); };
  return c;
}
// Coarse category for grouping the trajectory (icon name per category + a localized label).
const CAT_ICON = { search:"search", impl:"edit", verify:"check", external:"globe", run:"terminal", agent:"cpu", other:"dot" };
function catLabel(cat){ return t("cat."+cat); }
function catHead(cat, n){ const h = el("div","cathead"); h.append(ico(CAT_ICON[cat]||"dot"), document.createTextNode(" " + catLabel(cat) + "  ·  " + n)); return h; }
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
function projectLabel(f){ return f.cwd ? baseName(f.cwd) || f.cwd : t("cwd.none"); }
// A project = one cwd folder; sessions of the same folder stack under it in the sidebar.
function projKey(f){ return (f && f.cwd) ? (""+f.cwd).replace(/\/+$/,"") : "__nocwd__"; }
function projName(cwd){ return baseName(cwd) || cwd || t("cwd.none"); }
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
  if (sec < 60) return t("time.justNow");
  if (sec < 3600) return qty(Math.floor(sec/60), "time.minAgo");
  if (sec < 86400) return qty(Math.floor(sec/3600), "time.hourAgo");
  return qty(Math.floor(sec/86400), "time.dayAgo");
}
// The user's requests inside a session = top-level 'turn' tasks (subagents are nested below them).
function turnTasks(flowId){
  return Object.values(state.tasks)
    .filter(t => t.flow_id === flowId && t.kind !== "subagent" && !t.parent_task_id)
    .sort((a,b) => a.seq - b.seq);
}
function requestLabel(t){ return cleanTitle(t && t.title) || tr("req.none"); }
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
  const flows = await (await api("/api/flows?limit=500&has_cwd=true")).json();
  for (const f of flows) state.flows[f.flow_id] = f;
  // Open the most recent session and select its latest request, so the page isn't empty.
  const recent = [...flows].filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0))[0];
  if (recent && !state.selFlow) {
    state.expandedProjects.add(projKey(recent));  // expand the project this conversation belongs to
    await loadTree(recent.flow_id);
    state.selFlow = recent.flow_id;
    expandLatestTurn(recent.flow_id);
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
  } else if (m.op === "delete" && m.entity === "flow") {
    const id = m.data.flow_id;
    delete state.flows[id]; state.loaded.delete(id); delete seenSidebar[id];
    Object.values(state.tasks).forEach(t => { if (t.flow_id===id) delete state.tasks[t.task_id]; });
    Object.values(state.steps).forEach(s => { if (s.flow_id===id) delete state.steps[s.step_id]; });
    if (state.selFlow === id) state.selFlow = null;
    renderSidebar(); renderCanvas();
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
  const b = $("#mode"); const enf = state.mode === "enforce";
  b.replaceChildren(dotEl(enf ? "var(--amber)" : "var(--muted)"),
    document.createTextNode("mode: " + t(enf ? "mode.enforce" : "mode.observe")));
  b.classList.toggle("enf", enf);
}
function renderConn() { const c = $("#conn");
  c.replaceChildren(dotEl(state.connected ? "var(--green)" : "var(--red)"),
    document.createTextNode(t(state.connected ? "conn.connected" : "conn.disconnected")));
  c.className = "badge " + (state.connected ? "ok" : "bad"); }
function statusColor(s) { return ({running:"var(--accent)",completed:"var(--green)",failed:"var(--red)",aborted:"var(--muted)",pending_approval:"var(--amber)"})[s] || "var(--muted)"; }
// Apply the current language to all static chrome, then re-render the dynamic views (which use t()).
function applyLang(){
  document.documentElement.lang = LANG;
  $("#sessTitle").textContent = t("pane.sessions");
  $("#detailTitle").textContent = t("pane.detail");
  $("#harness").replaceChildren(ico("gear"), document.createTextNode(t("footer.harness"))); $("#harness").title = t("footer.harness.tip");
  $("#mode").title = t("mode.toggle");
  $("#lang").replaceChildren(ico("lang"), document.createTextNode(LANG === "ko" ? "EN" : "한")); $("#lang").title = t("lang.switch");
  $("#scopeTitle").textContent = t("scope.title"); $("#scopeSub").textContent = t("scope.subtitle");
  $("#scopeAdd").textContent = "+ " + t("common.add"); $("#scopeSave").textContent = t("common.save");
  $("#scopeClose").title = t("common.close"); $("#harnessClose").title = t("common.close");
  $("#leftResize").title = $("#rightResize").title = t("pane.resize.drag");
  $("#leftResize").setAttribute("aria-label", t("pane.sessions.resize"));
  $("#rightResize").setAttribute("aria-label", t("pane.detail.resize"));
  applyLayout(); renderMode(); renderConn(); renderSidebar(); renderCanvas(); renderPending();
  if (!state.sel) { const db = $("#detail-body"); if (db) db.replaceChildren(el("p","muted", t("detail.empty"))); }
  if ($("#harnessModal").style.display !== "none" && _crit) renderHarness();
}

// Sidebar: grouped by PROJECT (folder). Each project is an accordion whose sessions stack beneath
// it (like a Codex-style project → conversations list). A session expands into the user's requests
// (turn tasks); picking one focuses the canvas. Each project header also pins its own enforce/observe.
function renderSidebar() {
  const box = $("#sessions"); box.replaceChildren();
  // cwd-less subagent/tool sessions are excluded from tracking.
  const flows = Object.values(state.flows).filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0));
  if (!flows.length) { box.append(el("p","muted", t("sidebar.empty"))); return; }
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
// A clean monochrome folder glyph (inherits currentColor) — replaces the tacky 📁 emoji.
const FOLDER_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5a2 2 0 0 1 2-2h3.6a2 2 0 0 1 1.4.6l1 1a2 2 0 0 0 1.4.6H19a2 2 0 0 1 2 2v7.2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const TRASH_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>';
function svgIcon(svg, cls){ const s = el("span", cls || "ico"); s.innerHTML = svg; return s; }
function renderProject(g) {
  const open = state.expandedProjects.has(g.key);
  const wrap = el("div","projgroup" + (open ? " open" : ""));
  const head = el("div","projhead");
  // Row 1: caret + folder + name (name gets the full width, no longer squeezed by the mode control).
  const top = el("div","projtop");
  top.append(chevron(), svgIcon(FOLDER_SVG));
  const name = el("span","pname", projName(g.cwd)); name.title = g.cwd || "";
  top.append(name);
  if (g.flows.some(f => f.status === "running")) { const d = el("span","dot"); d.style.background = statusColor("running"); top.append(d); }
  head.append(top);
  // Row 2: session count + compact mode pill.
  const meta = el("div","projmeta");
  meta.append(el("span","cnt", qty(g.flows.length, "unit.sessions")), el("span","grow"), projModeControl(g.cwd));
  head.append(meta);
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
  [["", t("mode.global")], ["observe", "observe"], ["enforce", "enforce"]].forEach(([v, label]) => {
    const o = el("option", null, label); o.value = v; if ((ov || "") === v) o.selected = true; sel.append(o);
  });
  sel.title = ov ? (t("tip.projPinned") + ov) : (t("tip.projFollowsGlobal") + " (" + state.mode + ")");
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
  const flows = await (await api("/api/flows?limit=500&has_cwd=true")).json();
  for (const f of flows) state.flows[f.flow_id] = { ...state.flows[f.flow_id], ...f };
  renderSidebar(); renderCanvas();
}
function toggleProject(key) {
  if (state.expandedProjects.has(key)) state.expandedProjects.delete(key);
  else state.expandedProjects.add(key);
  renderSidebar();
}
// One conversation (= a Codex/Claude Code chat thread) inside a project group. The CONVERSATION is
// the unit: clicking it opens the whole thread (all turns) in the canvas — there is no per-request
// sub-level in the sidebar anymore.
function renderSession(f) {
  const item = el("div","sessitem");
  const head = el("div","sesshead" + (state.selFlow===f.flow_id ? " cur" : ""));
  head.append(el("span","srctag " + (f.source==="codex" ? "codex" : "claude"), srcName(f)));
  const sub = el("span","sname smeta", relTime(f.started_at) + " · " + statusLabel(f.status));
  sub.title = f.cwd || "";
  head.append(sub);
  const nturns = state.loaded.has(f.flow_id) ? turnTasks(f.flow_id).length : 0;
  if (nturns) head.append(el("span","chip", qty(nturns, "unit.turns")));
  const dot = el("span","dot"); dot.style.background = statusColor(f.status);
  head.append(dot);
  const del = el("span","convdel"); del.innerHTML = TRASH_SVG; del.title = t("tip.deleteConv");
  del.onclick = (ev) => { ev.stopPropagation(); deleteConversation(f); };
  head.append(del);
  head.onclick = () => selectConversation(f.flow_id);
  item.append(head);
  return item;
}
async function deleteConversation(f) {
  if (!confirm(t("confirm.deleteConv") + "\n" + projName(f.cwd) + " · " + srcName(f) + " · " + relTime(f.started_at))) return;
  const r = await api("/api/flows/" + encodeURIComponent(f.flow_id), { method:"DELETE" });
  if (!r.ok) return;
  // The WS 'delete' patch prunes state + re-renders; do it locally too for immediate feedback.
  delete state.flows[f.flow_id]; state.loaded.delete(f.flow_id);
  if (state.selFlow === f.flow_id) state.selFlow = null;
  renderSidebar(); renderCanvas();
}
// Expand only the latest turn by default, so opening a long conversation isn't a wall of steps.
function expandLatestTurn(flowId) {
  const t = defaultTurn(flowId);
  state.expandedTurns = new Set(t ? [t.task_id] : []);
}
async function selectConversation(flowId) {
  if (!state.loaded.has(flowId)) await loadTree(flowId);
  state.selFlow = flowId;
  expandLatestTurn(flowId);
  renderSidebar(); renderCanvas();
}

// The canvas shows the WHOLE selected conversation: the project/harness header once, then each turn
// (a user request) as a collapsible section with its trajectory. The conversation — not a single
// request — is the unit, matching how Codex/Claude Code present a chat thread.
function renderCanvas() {
  const c = $("#canvas"); c.replaceChildren();
  if (!state.selFlow) {
    const empty = Object.keys(state.flows).length === 0;
    c.append(el("p","muted", t(empty ? "canvas.emptyNoSessions" : "canvas.selectConv")));
    return;
  }
  const f = state.flows[state.selFlow]; if (!f) { c.append(el("p","muted", t("canvas.loadingConv"))); return; }

  const view = el("div","reqview");
  const head = el("div","reqhead");
  head.append(el("span","srctag " + (f.source==="codex" ? "codex" : "claude"), srcName(f)));
  const proj = el("span","chip project","project:"+projectLabel(f)); proj.title = f.cwd || t("cwd.none");
  const turns = turnTasks(f.flow_id);
  head.append(proj, el("span","chip",statusLabel(f.status)),
    el("span","chip",(f.total_tokens||0).toLocaleString()+" tok"), el("span","chip", qty(turns.length, "unit.turns")));
  view.append(head);

  // The harnesses apply to the whole conversation's project — shown once at the top, side by side.
  const ctx = el("div","hpanels");
  ctx.append(renderServicePanel(f), renderHarnessPanel(f));   // (A) service scaffolding · (B) our 3-Layer
  view.append(ctx);

  const th = el("div","trajhead");
  th.append(el("h2",null, t("traj.heading")));
  const modeBtn = el("button","trajbtn", t(state.trajMode==="time" ? "traj.time" : "traj.category"));
  modeBtn.title = t("traj.toggleTip");
  modeBtn.onclick = () => { state.trajMode = state.trajMode==="time" ? "category" : "time"; renderCanvas(); };
  th.append(el("span","grow"), modeBtn);
  view.append(th);

  // Each turn = one user request in the thread, NEWEST on top; only the open ones render their
  // (potentially long) trajectory.
  if (!turns.length) view.append(el("p","muted", t("traj.noActions")));
  else [...turns].reverse().forEach(t => view.append(renderTurnSection(f, t)));
  c.append(view);
}
function renderTurnSection(f, t) {
  const open = state.expandedTurns.has(t.task_id);
  const sec = el("div","turnsec" + (open ? " open" : ""));
  const head = el("div","turnhead");
  head.append(chevron());
  const ask = el("span","turnask", requestLabel(t)); ask.title = requestLabel(t);
  head.append(ask);
  const n = stepCountForTask(t.task_id); if (n) head.append(el("span","chip", qty(n, "unit.steps")));
  if (t.retry_count > 0) { const rc = el("span","chip"); rc.append(ico("retry"), document.createTextNode(" "+t.retry_count)); head.append(rc); }
  const dot = el("span","dot"); dot.style.background = statusColor(t.status); head.append(dot);
  head.onclick = () => toggleTurn(t.task_id);
  sec.append(head);
  if (open) {
    const body = el("div","turnbody");
    body.append(renderAskBlock(t));     // the full user request at the top — collapsed→preview, open→full
    body.append(renderTrajectory(t));
    sec.append(body);
  }
  return sec;
}
function toggleTurn(taskId) {
  if (state.expandedTurns.has(taskId)) state.expandedTurns.delete(taskId);
  else state.expandedTurns.add(taskId);
  renderCanvas();
}
// The user's request, shown at the top of an opened turn. Collapsed it clamps to a few lines (so a
// very long prompt does not flood the view); expanded it shows the whole thing.
function renderAskBlock(t) {
  const req = requestLabel(t);
  const expanded = state.expandedAsks.has(t.task_id);
  const block = el("div","askblock" + (expanded ? " open" : ""));
  const head = el("div","askhead");
  head.append(chevron(), el("span","asklabel", tr("ask.label")),
    el("span","grow"), el("span","muted askhint", tr(expanded ? "ask.collapse" : "ask.expandAll")));
  head.onclick = (ev) => { ev.stopPropagation(); toggleAsk(t.task_id); };
  block.append(head);
  const txt = el("div","asktext" + (expanded ? "" : " clamp")); txt.textContent = req;
  block.append(txt);
  return block;
}
function toggleAsk(taskId) {
  if (state.expandedAsks.has(taskId)) state.expandedAsks.delete(taskId);
  else state.expandedAsks.add(taskId);
  renderCanvas();
}

function renderHarnessPanel(f) {
  const p = el("div","harnesspanel layer");
  const head = el("div","hp-head");
  const toggle = el("button","iconbtn"); setChev(toggle, 0); toggle.title = t("hp.detailTip");
  head.append(ico("shield","hp-ico"), el("span","hp-name", t("hp.title")),
    el("span","chip scope","harness:"+harnessName(f)),
    el("span","chip","mode:"+harnessMode(f)));
  if (f.harness) head.append(el("span","chip","L1 "+f.harness.invariants_count+" · L2 "+f.harness.domain_criteria_count));
  // L3 breach surfaces in the header (as an alert) even while the panel is collapsed.
  const l3s = f.l3_status;
  if (l3s && l3s.breached) {
    const c = el("span","chip dchip deny"); c.append(ico("alert"), document.createTextNode(" " + t("hp.l3Breach")));
    c.title = t("hp.l3Breach.tip"); head.append(c);
  }
  head.append(el("span","grow"), toggle);
  p.append(head);
  const body = el("div","hp-body"); body.style.display = "none"; p.append(body);
  let built = false;
  head.onclick = () => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "";
    setChev(toggle, open ? 0 : 90);
    if (!open && !built) { buildHarnessBody(body, f); built = true; }
  };
  return p;
}
// Expanded 3-Layer body: extra provenance chips, edit actions, then the effective rules.
function buildHarnessBody(body, f) {
  const chips = el("div","hp-chips");
  if (f.harness) {
    const l3 = l3Summary(f.harness.layer3); if (l3) chips.append(el("span","chip", l3));
    if (f.harness.generated_detectors_count) {
      const gc = el("span","chip", t("hp.genDetector") + " " + f.harness.generated_detectors_count);
      gc.title = t("hp.genDetector.tip"); chips.append(gc);
    }
    if (f.harness.repo_policy_name) {
      const rc = el("span","chip repo");
      rc.append(ico("package"), document.createTextNode(" " + t("hp.repoPolicy") + ": " + f.harness.repo_policy_name
        + " (L1 " + (f.harness.repo_invariants_count||0) + "·L2 " + (f.harness.repo_domain_criteria_count||0) + ")"));
      rc.title = t("hp.repoPolicy.tip"); chips.append(rc);
    }
  }
  if (chips.childNodes.length) body.append(chips);
  const acts = el("div","hp-acts");
  const editBtn = el("button",null, t("hp.editProject"));
  editBtn.onclick = (ev) => { ev.stopPropagation(); openHarness(f.cwd || "__global__"); };
  const full = el("button",null, t("hp.globalBase"));
  full.onclick = (ev) => { ev.stopPropagation(); openHarness("__global__"); };
  acts.append(editBtn, full); body.append(acts);
  const rules = el("div"); body.append(rules); loadEffectiveInto(rules, f);
}
async function loadEffectiveInto(body, f) {
  body.replaceChildren(el("p","muted", t("common.loading")));
  let e = state.effective[f.flow_id];
  if (!e) {
    e = await (await api("/api/criteria/effective?flow_id="+encodeURIComponent(f.flow_id))).json();
    state.effective[f.flow_id] = e;
  }
  body.replaceChildren();
  const sc = e.scope || null, rp = e.repo_policy || null;
  body.append(el("div","muted", t("hp.effective") + ": " + (e.scope_name||"global") + " · mode: " + (e.mode||"?")
    + (rp ? "  · " + t("hp.repoPolicy") + ": " + (rp.name||"repo") : "") + (sc ? "  · " + t("hp.projRules") : "")));
  // Three-way provenance: a rule comes from the global base, the repo-committed governance policy,
  // or this developer's personal project scope.
  const addInv = new Set((sc && sc.add_invariants) || []);
  const addDc = new Set(((sc && sc.add_domain_criteria) || []).flatMap(d => [d.id, d.description]));
  const l3over = new Set(Object.keys((sc && sc.layer3) || {}));
  const repoInv = new Set((rp && rp.add_invariants) || []);
  const repoDc = new Set(((rp && rp.add_domain_criteria) || []).flatMap(d => [d.id, d.description]));
  const repoL3 = new Set(Object.keys((rp && rp.layer3) || {}));
  // Which rules actually fired somewhere in THIS flow → badge them so a long L1/L2 list shows at a
  // glance which constraints tripped (the answer to "which of the 30 did the harness catch?").
  const firedIn = (layer) => new Set(Object.values(state.steps)
    .filter(st => st.flow_id === f.flow_id && st.decision_layer === layer && st.decision_criterion)
    .map(st => st.decision_criterion));
  const firedInv = firedIn(1), firedDc = firedIn(2);
  const PROV = { repo:["repo", t("prov.repo")], proj:["proj", t("prov.proj")], glob:["glob", t("prov.glob")] };
  const tag = (src) => { const m = PROV[src]; return el("span", "prov " + m[0], m[1]); };
  const row = (src, text, hit) => {
    const it = el("div","it" + (hit ? " fired warn" : "")); it.append(tag(src), document.createTextNode(" " + text));
    if (hit) { const ft = el("span","firedtag"); ft.append(dotEl("var(--amber)"), document.createTextNode(" " + t("hp.firedThisSession"))); it.append(ft); }
    return it;
  };
  const invSrc = (r) => repoInv.has(r) ? "repo" : (addInv.has(r) ? "proj" : "glob");
  const dcSrc = (dc) => (repoDc.has(dc.id)||repoDc.has(dc.description)) ? "repo"
    : ((addDc.has(dc.id)||addDc.has(dc.description)) ? "proj" : "glob");
  const l3Src = (k) => repoL3.has(k) ? "repo" : (l3over.has(k) ? "proj" : "glob");

  const l1 = el("div"); l1.append(el("h4",null, t("layer1.full")));
  if ((e.invariants||[]).length) e.invariants.forEach(r => l1.append(row(invSrc(r), r, firedInv.has(r))));
  else l1.append(el("div","it muted", t("common.none")));
  const l2 = el("div"); l2.append(el("h4",null, t("layer2.full")));
  if ((e.domain_criteria||[]).length) e.domain_criteria.forEach(dc =>
    l2.append(row(dcSrc(dc),
      (dc.description||dc.id||"") + (dc.weight!=null ? " (w"+dc.weight+")" : ""), firedDc.has(dc.id))));
  else l2.append(el("div","it muted", t("common.none")));
  const l3 = el("div"); l3.append(el("h4",null, t("layer3.full")));
  Object.entries(e.layer3||{}).forEach(([k,v]) => { const s = l3Src(k);
    l3.append(row(s, k + ": " + v + (s !== "glob" ? "  (" + PROV[s][1] + " " + t("hp.override") + ")" : ""))); });
  body.append(l1, l2, l3);
}

// (A) service-harness panel — the external scaffolding (CLAUDE.md/AGENTS.md/skills/workflows/
// settings/MCP/hooks + .cursor/rules) applied to this session's project, scanned on demand.
function renderServicePanel(f) {
  const p = el("div","harnesspanel service");
  const head = el("div","hp-head");
  const toggle = el("button","iconbtn"); setChev(toggle, 0); toggle.title = t("common.toggle");
  head.append(ico("puzzle","hp-ico"), el("span","hp-name", t("svc.title")),
    el("span","chip","platform:"+srcName(f)));
  head.append(el("span","grow"), toggle);
  p.append(head);
  const body = el("div","hp-body"); body.style.display = "none"; p.append(body);
  let built = false;
  head.onclick = () => {
    const open = body.style.display !== "none";
    body.style.display = open ? "none" : "";
    setChev(toggle, open ? 0 : 90);
    if (!open && !built) { loadServiceInto(body, f); built = true; }
  };
  return p;
}
async function loadServiceInto(body, f) {
  const id = f.flow_id;
  let e = state.serviceHarness[id];
  if (e === undefined) {
    body.replaceChildren(el("p","muted", t("svc.scanning")));
    const pr = api("/api/flows/"+encodeURIComponent(id)+"/service_harness").then(r=>r.json());
    state.serviceHarness[id] = pr;
    e = await pr; state.serviceHarness[id] = e;
  } else if (e && typeof e.then === "function") {
    e = await e;
  }
  body.replaceChildren();
  const comps = (e && e.components) || [];
  if (!comps.length) { body.append(el("p","muted", t("svc.none"))); return; }
  const groups = {};
  comps.forEach(c => { (groups[c.scope] = groups[c.scope] || []).push(c); });
  Object.entries(groups).forEach(([scope, items]) => {
    const g = el("div"); g.append(el("h4",null, scope));
    items.forEach(c => {
      const row = el("div","it"); row.title = c.path;
      const ch = el("span","chip uchip"); ch.append(ico(USAGE_ICON[c.kind]||"tool"), document.createTextNode(" " + c.kind));
      row.append(ch, document.createTextNode(
        " " + baseName(c.path) + (c.detail ? " — " + c.detail : "") + (c.editable ? "  · " + t("svc.aheEditable") : "")));
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
  if (!steps.length && !kids.length) { wrap.append(el("p","muted", tr("traj.noActions"))); return wrap; }
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
      sec.append(catHead(cat, list.length));
      const chart = el("div","flowchart");
      list.forEach(s => chart.append(renderStep(s)));
      sec.append(chart); wrap.append(sec);
    }
  }
  if (kids.length) {
    const sec = el("div","catsec");
    sec.append(catHead("agent", kids.length));
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
  const label = el("span");
  if (isSub) label.append(ico("cpu"), document.createTextNode(" " + (t.agent_name||"subagent")));
  else label.textContent = (req||tr("req.none")).slice(0,46);
  if (req) label.title = req;
  hdr.append(label);
  if (t.retry_count>0) { const rc = el("span","chip"); rc.append(ico("retry"), document.createTextNode(" "+t.retry_count)); hdr.append(rc); }
  hdr.append(el("span","chip muted", statusLabel(t.status)));
  wrap.append(hdr);
  if (req) { const p = el("div","prompt"); p.textContent = req; wrap.append(p); }
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
  main.append(toolNameEl(s.tool_name));
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
    b.title = acted && s.decision_reason ? (s.decision + ": " + s.decision_reason) : (tag + " " + t("lyr.tip"));
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
    const dcolor = cls === "bad" ? "var(--red)" : cls === "warn" ? "var(--amber)" : "var(--green)";
    const verb = t(dec === "deny" ? "verdict.deny" : dec === "escalate" ? "verdict.escalate" : "verdict.pass");
    const v = el("div","step-verdict " + cls);
    const vm = el("span","vmark"); vm.append(dotEl(dcolor), document.createTextNode(" L" + s.decision_layer + " " + verb + " — "));
    v.append(vm, document.createTextNode(s.decision_reason));
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
  if (!appr) return el("span","chip", t("appr.waiting"));
  const card = el("span","chip"); card.textContent = t("status.pending_approval"); return card;
}

// ---- detail panel ----
function dcard(title) { const c = el("div","dcard"); if (title) c.append(el("div","dcard-h", title)); return c; }
function collapsible(label, text, open, cls) {
  const c = el("div","dcard" + (cls ? " " + cls : ""));
  const h = el("div","dcard-h clickable");
  const cv = chevron(); if (open) cv.style.transform = "rotate(90deg)";
  h.append(cv, document.createTextNode(label));
  const pre = el("pre"); pre.textContent = (typeof text === "string" ? text : JSON.stringify(text, null, 2));
  pre.style.display = open ? "" : "none";
  h.onclick = () => { const o = pre.style.display !== "none"; pre.style.display = o ? "none" : ""; cv.style.transform = o ? "" : "rotate(90deg)"; };
  c.append(h, pre); return c;
}
function layerRow(tag, name, s, rules, kind, focus) {
  const acted = s.decision_layer === ({L1:1, L2:2, L3:3}[tag]);
  const fired = acted ? (s.decision_criterion || null) : null;  // which specific rule decided
  const row = el("div","lyrrow" + (focus === tag ? " focus" : ""));
  const head = el("div","lyrrow-h");
  head.append(el("span","lyr " + tag.toLowerCase() + (acted ? (" acted " + (s.decision==="deny"?"bad":"warn")) : ""), tag),
    el("span","lyrname", name));
  let verdict = t("verdict.passNoViol"), cls = "ok";
  if (acted) { verdict = (s.decision||"") + (s.decision_reason ? " — " + s.decision_reason : ""); cls = s.decision==="deny" ? "bad" : "warn"; }
  else if (kind === "l3") { verdict = t("verdict.l3monitor"); cls = "muted"; }
  else if (kind === "dc") { verdict = t("verdict.dcStruct"); cls = "ok"; }
  head.append(el("span","grow"), el("span","verdict " + cls, verdict));
  row.append(head);
  const ul = el("div","lyrrules");
  // Normalise to {text, id}; mark the one rule that actually fired so it stands out among many.
  const items = (rules.length ? rules : [{text:t("common.none"), id:null}]).map(r =>
    (typeof r === "string" ? {text:r, id:r} : r));
  items.forEach(r => {
    const hit = fired != null && r.id === fired;
    const it = el("div","it" + (hit ? " fired " + (s.decision==="deny"?"bad":"warn") : ""), "• " + r.text);
    if (hit) { const ft = el("span","firedtag"); ft.append(dotEl(s.decision==="deny"?"var(--red)":"var(--amber)"),
      document.createTextNode(" " + t(s.decision==="deny"?"fired.deny":"fired.escalate"))); it.append(ft); }
    ul.append(it);
  });
  row.append(ul);
  return row;
}
async function showDetail(stepId, focusLayer) {
  const body = $("#detail-body"); body.replaceChildren(el("p","muted", t("common.loading")));
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
  const dt = el("span","dh-tool tname"); dt.append(ico(toolIconName(s.tool_name)), document.createTextNode(" " + toolLabel(s.tool_name)));
  hd.append(dt, el("span","dh-status st-" + s.status, statusLabel(s.status)));
  if (s.duration_ms != null) hd.append(el("span","muted", s.duration_ms + "ms"));
  body.append(hd);
  const cmd = stepSummary(s);
  if (cmd) { const cb = el("div","dcmd"); cb.textContent = cmd; cb.title = cmd; body.append(cb); }

  // 3-Layer evaluation card — L1/L2/L3 rules + this step's verdict. Appended last so logs stay first.
  const mode = f ? harnessMode(f) : "?";
  const card = dcard(t("detail.evalTitle") + "  ·  " + (f ? harnessName(f) : "?") + " · " + mode
    + " (" + t(mode === "observe" ? "mode.observeNote" : "mode.enforceNote") + ")");
  // Pinpoint callout: when a rule fired, name WHICH one (id + full wording) up top, so among dozens
  // of L2 criteria the operator sees exactly the one that gated — without scanning the list.
  if (s.decision_layer != null && s.decision_criterion) {
    const dc2 = ((e && e.domain_criteria) || []).find(d => d.id === s.decision_criterion);
    const ruleText = dc2 ? (dc2.description || dc2.id) : s.decision_criterion;
    const dec = s.decision;
    const fc = el("div","firedcallout " + (dec==="deny" ? "bad" : "warn"));
    const fb = el("span","firedbadge"); fb.append(dotEl(dec==="deny"?"var(--red)":"var(--amber)"),
      document.createTextNode(" L" + s.decision_layer + " " + t("detail.firedRule")));
    fc.append(fb, el("span","firedid", s.decision_criterion), el("span","firedtext", ruleText));
    card.append(fc);
  }
  card.append(layerRow("L1", t("layer1.short"), s, ((e && e.invariants) || []).map(r => ({text:r, id:r})), "inv", focusLayer));
  card.append(layerRow("L2", t("layer2.short"), s, ((e && e.domain_criteria) || []).map(d => ({
    text: (d.description||d.id||"") + (d.weight!=null?" (w"+d.weight+")":""), id: d.id})), "dc", focusLayer));
  card.append(layerRow("L3", t("layer3.short"), s, e && e.layer3 ? Object.entries(e.layer3).map(([k,v]) => ({text:k + ": " + v, id:k})) : [], "l3", focusLayer));

  // Service harness exercised here — chips clickable → show the prompt.
  const sh = dcard(t("detail.svcThisAction"));
  const ucwd = f ? f.cwd : null;
  if (s.harness_usage && s.harness_usage.length) {
    const box = el("div","hp-head"); s.harness_usage.forEach(u => box.append(usageChip(u, ucwd))); sh.append(box);
    sh.append(el("div","muted", t("detail.chipClickPrompt")));
  } else { sh.append(el("div","muted", t("detail.noScaffold"))); }
  body.append(sh);

  // Input / output / judge — collapsed by default to keep the panel clean.
  if (s.tool_input) body.append(collapsible(t("common.input"), s.tool_input, false, "dlog"));
  if (s.tool_output) body.append(collapsible(t("common.output"), s.tool_output, false, "dlog"));
  if (s.judge_reason) body.append(collapsible("Judge", s.judge_reason, false, "dlog"));
  const appr = Object.values(state.approvals).find(a => a.step_id === stepId && !a.resolved_at);
  if (appr) body.append(approvalCard(appr, s));
  body.append(card);
  if (focusLayer) requestAnimationFrame(() => card.scrollIntoView({ block:"start" }));
}
function field(label, text) { const d = el("div"); d.append(el("h2",null,label)); const p = el("pre"); p.textContent = typeof text==="string"?text:JSON.stringify(text,null,2); d.append(p); return d; }

// Show a service-harness component's actual prompt (the skill/instruction/rule file) in the detail pane.
async function showComponent(kind, name, cwd) {
  const body = $("#detail-body"); body.replaceChildren(el("p","muted", t("common.loading")));
  const q = "?kind=" + encodeURIComponent(kind) + "&name=" + encodeURIComponent(name)
    + (cwd ? "&cwd=" + encodeURIComponent(cwd) : "");
  let c;
  try { c = await (await api("/api/harness/component" + q)).json(); }
  catch (e) { c = { found: false, note: t("common.loadFail") }; }
  body.replaceChildren();
  const _hd = el("div"); const hb = el("b"); hb.append(ico(USAGE_ICON[kind]||"tool"), document.createTextNode(" " + name)); _hd.append(hb); body.append(_hd);
  body.append(el("div","muted", t("detail.svcPrefix") + " · " + kind + (c.path ? " · " + c.path : "")));
  if (c.found && c.content) { const pre = el("pre"); pre.style.maxHeight="70vh"; pre.textContent = c.content; body.append(pre); }
  else body.append(el("p","muted", c.note || t("detail.noPrompt")));
}

// ---- approvals ----
function approvalCard(appr, step) {
  const card = el("div","appr");
  const ahdr = el("div"); ahdr.append(ico("alert"), document.createTextNode(" " + t("appr.waitingFor") + (appr.reason||""))); card.append(ahdr);
  const bar = el("div","bar"); const fill = el("i"); bar.append(fill); card.append(bar);
  if (appr.timeout_at) {
    const total = Math.max(1, appr.timeout_at - (appr._t0 || (appr._t0 = Date.now()/1000)));
    const tick = () => { const left = appr.timeout_at - Date.now()/1000; fill.style.width = Math.max(0, Math.min(100, left/total*100))+"%";
      if (left <= 0 || appr.resolved_at) clearInterval(iv); };
    const iv = setInterval(tick, 200); tick();
  }
  const row = el("div","row");
  const yes = el("button",null, t("appr.approve")); const no = el("button",null, t("appr.deny"));
  yes.disabled = no.disabled = !state.connected;  // never resolve over a dead socket
  const reason = el("input"); reason.type = "text"; reason.placeholder = t("appr.denyReason");
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
  b.style.display = ""; b.className = "badge alert"; b.textContent = t("appr.pendingBadge") + open.length;
  b.onclick = async () => {
    const first = open[0]; if (!first) return;
    state.sel = first.step_id; showDetail(first.step_id);
    // Focus the conversation that owns the awaiting step, with its turn expanded.
    const s = state.steps[first.step_id];
    if (s) {
      const f = state.flows[s.flow_id]; if (f) state.expandedProjects.add(projKey(f));
      if (!state.loaded.has(s.flow_id)) await loadTree(s.flow_id);
      state.selFlow = s.flow_id; if (s.task_id) state.expandedTurns.add(s.task_id);
      renderSidebar(); renderCanvas();
    }
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
  const desc = el("input"); desc.placeholder=t("scope.addL2Placeholder"); desc.value=dc.description||""; desc.dataset.dc="description";
  const w = el("input"); w.type="number"; w.step="any"; w.placeholder="weight"; w.value=dc.weight!=null?dc.weight:1; w.dataset.dc="weight";
  r.append(id, desc, w, delBtn(() => r.remove()));
  return r;
}
function scopeRow(s) {
  s = s || { name:"", match:{}, mode:"", layer3:{}, add_invariants:[], add_domain_criteria:[] };
  const row = el("div","scoperow");
  const l1 = el("div","line");
  const nm = el("input"); nm.className="nm"; nm.placeholder=t("scope.name"); nm.value = s.name||""; nm.dataset.f="name";
  const mt = el("select"); mt.dataset.f="matchType";
  mt.append(opt("cwd_prefix",t("scope.matchCwd")), opt("session_id",t("scope.matchSession")));
  mt.value = (s.match && s.match.session_id) ? "session_id" : "cwd_prefix";
  const mv = el("input"); mv.className="mv"; mv.dataset.f="matchValue";
  mv.placeholder = t("scope.matchPlaceholder");
  mv.value = (s.match && (s.match.cwd_prefix || s.match.session_id)) || "";
  const del = el("button","del",t("common.delete")); del.onclick = () => row.remove();
  l1.append(nm, el("label",null,t("scope.matchLabel")), mt, mv, del);
  const l2 = el("div","line");
  const md = el("select"); md.dataset.f="mode";
  md.append(opt("",t("scope.modeInherit")), opt("observe","observe"), opt("enforce","enforce"));
  md.value = s.mode || "";
  l2.append(md, el("label",null,"L3"));
  for (const [key,lab] of L3KEYS) {
    const i = el("input"); i.className="l3"; i.type="number"; i.step="any"; i.placeholder=lab; i.dataset.l3=key;
    if (s.layer3 && s.layer3[key] != null) i.value = s.layer3[key];
    l2.append(i);
  }
  const l3 = el("div","line");
  const ta = el("textarea"); ta.dataset.f="invariants"; ta.placeholder=t("scope.addInvPlaceholder");
  ta.value = (s.add_invariants||[]).join("\n");
  l3.append(ta);
  const l4 = el("div","line");
  l4.append(el("label",null,t("scope.addL2")));
  const addDc = el("button",null,t("scope.addCriterion"));
  const dcBox = el("div","scopedc");
  (s.add_domain_criteria||[]).forEach(dc => dcBox.append(scopeDcRow(dc)));
  addDc.onclick = () => dcBox.append(scopeDcRow());
  l4.append(addDc, dcBox);
  row.append(l1, l2, el("div","muted",t("scope.note")), l3, l4);
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
    $("#scopeMsg").textContent = t("scope.msgDraft");
  } else if (seedFlow) {
    $("#scopeMsg").textContent = t("scope.msgMatched");
  } else {
    $("#scopeMsg").textContent = "";
  }
  $("#scopeModal").style.display = "flex";
}
$("#scopeClose").onclick = () => { $("#scopeModal").style.display = "none"; };
$("#scopeModal").onclick = e => { if (e.target.id === "scopeModal") $("#scopeModal").style.display = "none"; };
$("#scopeAdd").onclick = () => { $("#scopeList").append(scopeRow()); };
$("#scopeSave").onclick = async () => {
  const r = await api("/api/scopes", { method:"POST", body: JSON.stringify({ scopes: collectScopes() }) });
  if (r.ok) {
    const d = await r.json();
    $("#scopeMsg").textContent = t("scope.saved", d.scopes.length);
    _scopes = d.scopes || [];
    loadSnapshot();  // mode/harness chips may change for affected flows
    if ($("#harnessModal").style.display !== "none") renderHarness();
  } else {
    $("#scopeMsg").textContent = t("scope.saveFail", r.status);
  }
};

// ---- harness (3-Layer base + project scopes) editor ----
let _crit = null, _scopes = [], _l1open = false, _hpProject = "__global__", _compileReport = null;
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
  const m = $("#harnessMsg"); m.textContent = t("common.saving"); m.className = "muted";
  const r = await api("/api/criteria/"+layer, { method:"POST", body: JSON.stringify(body) });
  if (r.ok) { _crit = await r.json(); _l1open = false; renderHarness(); m.textContent = t("harness.savedImmediate"); m.className = "ok"; }
  else { const d = await r.json().catch(()=>({})); m.textContent = t("harness.rejected") + (d.detail || r.status); m.className = "warn"; }
}

// Rule compiler — POST the current L1/L2 rules; the daemon classifies them (LLM via host CLI) into
// deterministic detectors vs semantic (advisory). Read-only preview; it does not save anything.
// Which rules to compile depends on the active editor: the global base, or a project's own additions.
function compileRules() {
  if (_hpProject === "__global__")
    return { invariants: _l1open ? collectL1() : ((_crit && _crit.invariants) || []), domain_criteria: collectL2() };
  return { invariants: collectProjInv(), domain_criteria: collectProjDc() };
}
async function runCompile(btn) {
  const wrap = $("#hl-compileWrap"); if (!wrap) return;
  const { invariants: inv, domain_criteria: dcs } = compileRules();
  if (btn) { btn.disabled = true; btn.textContent = t("compile.classifying"); }
  wrap.replaceChildren(el("div","hl-help", t("compile.calling")));
  try {
    const r = await api("/api/criteria/compile", { method:"POST",
      body: JSON.stringify({ invariants: inv, domain_criteria: dcs }) });
    const d = await r.json().catch(()=>({}));
    if (!r.ok) { wrap.replaceChildren(el("div","warn", t("compile.fail") + (d.detail || r.status))); return; }
    _compileReport = d;
    renderCompile(wrap, d);
  } catch (e) {
    wrap.replaceChildren(el("div","warn", t("compile.error") + e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("compile.btn"); }
  }
}
function compileTag(rule) {
  if (rule.enforcement === "detector") return el("span","hl-tag det", t("compile.tag.det"));
  if (rule.classification === "unknown") return el("span","hl-tag unk", t("compile.tag.unk"));
  return el("span","hl-tag adv", t("compile.tag.adv"));
}
function renderCompile(wrap, data) {
  wrap.replaceChildren();
  const c = data.counts || {};
  wrap.append(el("div","hl-help",
    t("compile.summary", c.detector||0, c.advisory||0, c.total||0)
    + (data.llm_used ? t("compile.llmUsed") : t("compile.builtinOnly"))));
  if (data.note) wrap.append(el("div","warn", data.note));
  // Two columns: LEFT = the rules the user wrote (semantic input), RIGHT = the compiled verdict.
  const split = el("div","hl-split");
  split.append(el("div","col-h", t("compile.colInput")), el("div","col-h", t("compile.colOutput")));
  (data.rules || []).forEach(rule => {
    const left = el("div","hl-cin");
    left.append(el("span","cid","L"+rule.layer + (rule.id ? " "+rule.id : "")),
      document.createTextNode(" " + rule.text));
    const right = el("div","hl-cout");
    const top = el("div","top"); top.append(compileTag(rule));
    if (rule.builtin_detector) top.append(el("span","hl-help", t("compile.builtin") + rule.builtin_detector));
    right.append(top);
    if (rule.reason) right.append(el("div","hl-help", rule.reason));
    const p = rule.proposed;
    if (p && p.regex) {
      const rx = el("div");
      rx.append(el("span","hl-regex", p.regex),
        p.verified ? el("span","hl-tag det", t("compile.verifyOk"))
                   : el("span","hl-tag adv", t("compile.verifyFail")));
      if (p.verify_detail) rx.append(el("span","hl-help"," " + p.verify_detail));
      right.append(rx);
    }
    split.append(left, right);
  });
  wrap.append(split, compileActions());
}

// Export bar — download the compiled result as YAML, and import it into a project's committed
// .harness-lens/policy.yaml (so the compiled harness is git-distributed / PR-reviewed).
function compileActions() {
  const bar = el("div","hl-cbar");
  const dl = el("button","hl-save", t("compile.exportYaml")); dl.onclick = () => exportCompiled();
  bar.append(dl);
  const projs = hpProjects();
  let target;
  if (projs.length) {
    const sel = el("select"); sel.id = "hl-importTarget";
    projs.forEach(cwd => { const o = el("option",null, baseName(cwd) || cwd); o.value = cwd; sel.append(o); });
    if (_hpProject !== "__global__") sel.value = _hpProject;
    target = () => sel.value;
    bar.append(el("span","hl-help", t("compile.toProject")), sel);
  } else {
    const inp = el("input","t"); inp.placeholder = t("compile.pathPlaceholder"); inp.style.minWidth = "16rem";
    target = () => inp.value;
    bar.append(el("span","hl-help", t("compile.toPath")), inp);
  }
  const imp = el("button","hl-compile", t("compile.import"));
  imp.title = t("compile.importTip");
  imp.onclick = () => importCompiled((target() || "").trim(), imp);
  const msg = el("span","hl-help"); msg.id = "hl-importMsg";
  bar.append(imp, msg);
  return bar;
}
function exportCompiled() {
  const y = _compileReport && _compileReport.yaml; if (!y) return;
  const name = (_compileReport.policy && _compileReport.policy.name) || "compiled-harness";
  const blob = new Blob([y], { type: "text/yaml" });
  const a = el("a"); a.href = URL.createObjectURL(blob); a.download = name + ".policy.yaml";
  document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
}
async function importCompiled(cwd, btn) {
  const msg = $("#hl-importMsg");
  const set = (t, cls) => { if (msg) { msg.textContent = t; msg.className = cls; } };
  if (!cwd) { set(t("compile.needPath"), "warn"); return; }
  if (!_compileReport || !_compileReport.policy) return;
  if (btn) btn.disabled = true; set(t("compile.writing"), "hl-help");
  try {
    const r = await api("/api/harness/apply", { method:"POST",
      body: JSON.stringify({ cwd: cwd, policy: _compileReport.policy, target: "repo" }) });
    const d = await r.json().catch(()=>({}));
    set(r.ok ? (t("compile.savedTo") + (d.path || cwd)) : (t("common.failPrefix") + (d.detail || r.status)), r.ok ? "ok" : "warn");
  } catch (e) { set(t("common.errorPrefix") + e, "warn"); }
  finally { if (btn) btn.disabled = false; }
}
function layerBox(id, title, help) {
  const b = el("div","hl-layer"); b.id = id;
  const h = el("div","hl-lhead");
  const m = /l([123])(?:$|-)/i.exec(id);
  if (/mode/i.test(id)) h.append(el("span","hl-lbadge mode","MODE"));
  else if (m) h.append(el("span","hl-lbadge l"+m[1], "L"+m[1]));
  h.append(el("span","hl-ltitle", title));
  b.append(h);
  if (help) b.append(el("div","hl-help", help));
  return b;
}
function actions(...btns){ const a = el("div","hl-actions"); a.append(...btns); return a; }
function delBtn(onDel){ const d = el("button","hl-del", t("common.delete")); d.onclick = onDel; return d; }
// The 3-Layer editor is project-first: pick a project (or 전역 기본) and edit the harness that
// applies to it in one place. A project's harness = the global base + that folder's own additions.
function scopeForCwd(cwd) { return _scopes.find(s => s.match && s.match.cwd === cwd) || null; }
function hpProjects() {
  const seen = new Set(), out = [];
  Object.values(state.flows).filter(hasCwd).sort((a,b)=>(b.started_at||0)-(a.started_at||0))
    .forEach(f => { if (!seen.has(f.cwd)) { seen.add(f.cwd); out.push(f.cwd); } });
  return out;
}
// A target row in the left rail (global base, or one project).
function railItem(key, label, title, icon){
  const b = el("div","hl-tgt" + (_hpProject===key ? " cur" : ""));
  b.append(ico(icon||"folder"), el("span","nm", label));
  if (title) b.title = title;
  b.onclick = () => { _hpProject = key; _l1open = false; $("#harnessMsg").textContent = ""; renderHarness(); };
  return b;
}

// Two-pane settings layout: a target rail on the left, the selected harness's editor on the right.
function renderHarness() {
  const body = $("#harnessBody"); body.replaceChildren();
  $("#harnessTitle").textContent = t("harness.title");
  $("#harnessBadge").textContent = t("harness.badge");
  const rail = el("div","hl-rail");
  rail.append(el("div","hl-railhead", t("harness.projects")));
  rail.append(railItem("__global__", t("harness.globalBase"), t("harness.globalBaseTip"), "globe"));
  hpProjects().forEach(cwd => rail.append(railItem(cwd, baseName(cwd) || cwd, cwd, "folder")));
  const content = el("div","hl-content");
  if (_hpProject === "__global__") renderGlobalEditor(content);
  else renderProjectEditor(content, _hpProject);
  body.append(rail, content);
}

// ---- 전역 기본 (base) editor — applies to every project ----
function renderGlobalEditor(body) {
  body.append(el("div","hl-help", t("harness.globalIntro")));
  // Top control bar: the rule compiler (classify → export YAML / import into a project's policy.yaml).
  const ctl = el("div","hl-controls");
  const cbtn = el("button","hl-compile", t("compile.btn")); cbtn.title = t("compile.btnTip");
  cbtn.onclick = () => runCompile(cbtn);
  ctl.append(cbtn, el("span","hl-help", t("compile.hookNote")));
  body.append(ctl);
  const cwrap = el("div","hl-cwrap"); cwrap.id = "hl-compileWrap"; body.append(cwrap);
  // The three Layer editors — the main content, grouped at the bottom.
  body.append(el("div","hl-secthead", t("harness.layersSection")));
  // Layer 1 — invariants (unlock-gated)
  const b1 = layerBox("hl-l1", t("layer1.full"), t("harness.l1Help"));
  const lock = el("button","hl-lock");
  lock.append(ico(_l1open ? "unlock" : "lock"), document.createTextNode(" " + t(_l1open ? "harness.l1Unlocked" : "harness.l1Locked")));
  lock.onclick = () => { if (_l1open) _crit.invariants = collectL1(); _l1open = !_l1open; renderHarness(); };
  b1.querySelector(".hl-lhead").append(lock);
  if (!_l1open) {
    (_crit.invariants.length ? _crit.invariants : [t("common.none")]).forEach(r => b1.append(el("div",null,"• "+r)));
  } else {
    const warn = el("div","warn"); warn.append(ico("alert"), document.createTextNode(" " + t("harness.l1Warn"))); b1.append(warn);
    _crit.invariants.forEach(r => {
      const it = el("div","hl-item"); const i = el("input","t"); i.type="text"; i.value=r; i.placeholder=t("harness.rulePlaceholder");
      it.append(i, delBtn(() => { const v=collectL1(); v.splice([...b1.querySelectorAll('.hl-item')].indexOf(it),1); _crit.invariants=v; renderHarness(); }));
      b1.append(it);
    });
    const add = el("button",null,t("harness.addRule")); add.onclick = () => { _crit.invariants=collectL1(); _crit.invariants.push(""); renderHarness(); };
    const save = el("button","hl-save",t("common.save")); save.onclick = () => saveLayer("layer1", { invariants: collectL1() });
    b1.append(actions(add, save));
  }
  body.append(b1);
  // Layer 2 — domain criteria
  const b2 = layerBox("hl-l2", t("layer2.full"), t("harness.l2Help"));
  _crit.domain_criteria.forEach(dc => {
    const it = el("div","hl-item"); it.dataset.id = dc.id || "";
    it.append(el("span","cid", dc.id || "(new)"));
    const i = el("input","t"); i.type="text"; i.value=dc.description||""; i.placeholder=t("harness.criterionPlaceholder");
    const w = el("input","w"); w.type="number"; w.step="any"; w.min="0"; w.title="weight"; w.value = dc.weight!=null?dc.weight:1;
    it.append(i, w, delBtn(() => { const v=collectL2(); v.splice([...b2.querySelectorAll('.hl-item')].indexOf(it),1); _crit.domain_criteria=v; renderHarness(); }));
    b2.append(it);
  });
  const add2 = el("button",null,t("harness.addCriterion")); add2.onclick = () => { _crit.domain_criteria=collectL2(); _crit.domain_criteria.push({id:"",description:"",weight:1}); renderHarness(); };
  const save2 = el("button","hl-save",t("common.save")); save2.onclick = () => saveLayer("layer2", { domain_criteria: collectL2() });
  b2.append(actions(add2, save2));
  body.append(b2);
  // Layer 3 — QA thresholds (the only AHE-evolvable layer)
  const b3 = layerBox("hl-l3", t("layer3.full"), t("harness.l3Help"));
  Object.entries(_crit.layer3).forEach(([k,v]) => {
    const it = el("div","hl-item"); it.append(el("span","cid",k));
    const i = el("input","w"); i.type="number"; i.step="any"; i.dataset.k=k; i.value=v;
    it.append(i, el("span","hl-help", t("l3help."+k))); b3.append(it);
  });
  const save3 = el("button","hl-save",t("common.save")); save3.onclick = () => saveLayer("layer3", { layer3: collectL3() });
  b3.append(actions(save3));
  body.append(b3);
}

// ---- per-project editor — the global base PLUS this folder's own additions/overrides ----
function renderProjectEditor(body, cwd) {
  const scope = scopeForCwd(cwd) || {};
  const label = baseName(cwd) || cwd;
  const intro = el("div","hl-help"); intro.append(ico("folder"), document.createTextNode(" " + t("harness.projIntro", label)));
  body.append(intro);
  body.append(el("div","hl-help muted", t("harness.projMatch", cwd)));

  // Top control bar: run mode + rule compiler, compact so the Layer editors below are the focus.
  const ctl = el("div","hl-controls");
  const modeCtl = el("span","ctl"); modeCtl.append(el("label",null, t("harness.modeTitle")));
  const md = el("select"); md.id = "hp-mode"; md.title = t("harness.modeHelp");
  [["",t("harness.modeInherit")],["observe",t("harness.modeObserve")],["enforce",t("harness.modeEnforce")]].forEach(([v,l])=>{
    const o = el("option",null,l); o.value=v; md.append(o); });
  md.value = scope.mode || "";
  modeCtl.append(md);
  const cbtn = el("button","hl-compile", t("compile.btn")); cbtn.title = t("compile.btnTip");
  cbtn.onclick = () => runCompile(cbtn);
  ctl.append(modeCtl, el("span","sep"), cbtn);
  body.append(ctl);
  body.append(el("div","hl-help", t("compile.hookNote")));
  const cwrap = el("div","hl-cwrap"); cwrap.id = "hl-compileWrap"; body.append(cwrap);
  // The three Layer editors — the main content, grouped at the bottom.
  body.append(el("div","hl-secthead", t("harness.layersSection")));

  // Layer 1
  const b1 = layerBox("hp-l1", t("layer1.full"), t("harness.l1ProjHelp"));
  (_crit.invariants||[]).forEach(inv => { const d=el("div","hl-item muted"); d.append(el("span",null,"• "+inv), el("span","cid",t("harness.globalTag"))); b1.append(d); });
  (scope.add_invariants||[]).forEach(inv => addProjInvRow(b1, inv));
  const add1 = el("button",null,t("harness.addFolderRule")); add1.onclick = () => addProjInvRow(b1, "");
  b1.append(actions(add1)); body.append(b1);

  // Layer 2
  const b2 = layerBox("hp-l2", t("layer2.full"), t("harness.l2ProjHelp"));
  (_crit.domain_criteria||[]).forEach(dc => { const d=el("div","hl-item muted");
    d.append(el("span","cid",t("harness.globalTag")), el("span",null,dc.description||""), el("span","cid","w"+(dc.weight!=null?dc.weight:1))); b2.append(d); });
  (scope.add_domain_criteria||[]).forEach(dc => addProjDcRow(b2, dc));
  const add2 = el("button",null,t("harness.addFolderCriterion")); add2.onclick = () => addProjDcRow(b2, {});
  b2.append(actions(add2)); body.append(b2);

  // Layer 3 — override (blank = inherit the global value)
  const b3 = layerBox("hp-l3", t("layer3.full"), t("harness.l3ProjHelp"));
  L3KEYS.forEach(([k,lab]) => {
    const it = el("div","hl-item"); it.append(el("span","cid",lab));
    const i = el("input","w"); i.type="number"; i.step="any"; i.dataset.k=k; i.id="hpL3-"+k;
    const base = (_crit.layer3||{})[k];
    if (scope.layer3 && scope.layer3[k]!=null) i.value = scope.layer3[k];
    i.placeholder = base!=null ? (t("prov.glob")+" "+base) : "";
    it.append(i, el("span","hl-help", t("l3help."+k) + (base!=null?(" · "+t("prov.glob")+" "+base):"")));
    b3.append(it);
  });
  body.append(b3);

  const save = el("button","hl-save",t("harness.saveProject")); save.onclick = () => saveProject(cwd);
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
  const m = $("#harnessMsg"); m.textContent=t("common.saving"); m.className="muted";
  const r = await api("/api/scopes", { method:"POST", body: JSON.stringify({ scopes: [...others, scope] }) });
  if (r.ok) { _scopes = (await r.json()).scopes || []; renderHarness(); loadSnapshot();
    m.textContent=t("harness.savedProject", baseName(cwd)||cwd); m.className="ok"; }
  else { const d=await r.json().catch(()=>({})); m.textContent=t("harness.saveFail")+(d.detail||r.status); m.className="warn"; }
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
$("#harnessClose").onclick = () => { $("#harnessModal").style.display = "none"; };
$("#harnessModal").onclick = e => { if (e.target.id === "harnessModal") $("#harnessModal").style.display = "none"; };

$("#lang").onclick = () => { LANG = (LANG === "ko" ? "en" : "ko"); try { localStorage.setItem("hl-lang", LANG); } catch(_){}; applyLang(); };
initLayout(); applyLang(); loadSnapshot().then(connect);
</script>
</body>
</html>
"""


def render_page(token: str) -> str:
    # Token is embedded for the page's own same-origin API/WS calls (loopback single-user model).
    return _PAGE.replace("__HL_TOKEN__", token)
