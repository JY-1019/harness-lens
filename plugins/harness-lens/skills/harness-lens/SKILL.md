---
name: harness-lens
description: Observe and evolve the agentic harness. Use when the user wants to inspect Flows/Tasks/Steps, see the project's harness, diagnose failure patterns, propose or apply harness evolutions, verify predictions, or check harness status.
---

# harness-lens

Observe and evolve the agentic harness. Use when the user wants to inspect Flows/Tasks/Steps, see the project's harness, diagnose failure patterns, propose or apply harness evolutions, verify predictions, or check harness status.

harness-lens watches this harness through plugin hooks and an MCP server and reconstructs each session into a Flow / Task / Step trajectory, governed by a 3-Layer harness (Layer 1 invariants · Layer 2 domain criteria · Layer 3 QA thresholds). A local daemon holds the ledger and serves a live web UI at **http://127.0.0.1:7700/ui**. Run the relevant command and relay its output; do not invent Flow data.

The launcher `hl` is provided by this plugin at `${CLAUDE_PLUGIN_ROOT}/bin/hl` (on Codex, `${CODEX_PLUGIN_ROOT}/bin/hl`). If `hl` is not on PATH, call it by that full path.

## Commands

- `hl show [--fail] [--limit N]` — recent Flows with Layer-2 scores and gap ratio
- `hl harness [--project DIR]` — inspect the harness applied to this project (components + 3-Layer)
- `hl status` — 3-Layer state, prediction hit-rate, Judge recommendation, gap ratio
- `hl diagnose` — Pillar 2 — diagnose recurring failure patterns (needs an LLM backend)
- `hl evolve` — Pillar 3 — propose harness fixes, each with a falsifiable prediction
- `hl evolve --apply ID --yes` — apply a proposed candidate (backs up the original first)
- `hl verify` — check applied predictions → confirm hits, roll back misses
- `hl review [--sample ID --label 0..1]` — label Judge samples to calibrate Layer-2
- `hl rollback` — revert the last applied change
- `hl daemon status` — is the control-plane daemon running; `hl mode <observe|enforce>` switches mode
- `hl approvals` — resolve pending escalations from the terminal (or use the web UI)

## Guidance

- For "what happened" / "show recent work" use `show`; add `--fail` for only failed Flows.
- For "what's wrong with the harness" run `diagnose`, then `evolve` to get fix proposals.
- Only apply an evolution with `evolve --apply` after the user confirms; it edits real files (CLAUDE.md / AGENTS.md / hooks / qa.py) and is reverted by `rollback`.
- `diagnose` / `evolve` need an LLM backend; if none is configured, say so instead of guessing.
- To watch live, open the web UI at http://127.0.0.1:7700/ui (the SessionStart hook keeps the daemon running).
