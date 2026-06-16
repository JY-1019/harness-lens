# enforce-playground

A self-contained way to **see the 3-Layer harness enforce (and deliberately not enforce)** in the
live monitoring GUI. `run.py` drives the daemon through a fixed sequence of hook events, so every
Layer's decision is reproducible, self-checked (PASS/FAIL), and visible as a
`[codex] enforce-playground` session in the GUI.

It is **non-destructive**: it adds one project scope that matches **only this folder** (exact `cwd`,
`mode: enforce`) on top of your existing config, drives a synthetic session, then removes the scope
again. Your global mode and other projects are untouched.

## Run

```bash
harness-lens daemon start            # if not already running
uv run --project . python examples/enforce-playground/run.py
```

Then open the GUI link it prints (`http://127.0.0.1:<port>/ui`) and click the
`[codex] enforce-playground` session.

Flags:
- `--manual-approval` — leave the Layer-2 escalation pending so **you** approve/deny it in the GUI's
  "승인 대기" card (keeps the scope so you can finish the flow).
- `--source claude_code` — drive as Claude Code instead of Codex.
- `--keep-scope` — leave the enforce scope installed (e.g. to then run a live agent here).
- `--cleanup` — remove the scope and exit.

## What each scenario shows (and where the harness *stops* helping)

| Layer | Scenario | Decision | What it demonstrates |
|------|----------|----------|----------------------|
| **L1** | a production `DELETE` shell command | **deny** | A deterministic invariant detector blocks the tool. The step shows struck-through + `L1 deny`. |
| **L1** | personal data sent to an external API (`curl`) | **deny** | The PII→external detector blocks it. |
| **L1** | a "no debug console.log" edit | **allow** | ⚠️ **Boundary:** that scope rule's wording maps to *no built-in detector*, so it is advisory only — recorded, **not** blocked. Rules whose wording DOES map to a detector (rm -rf, secrets, force-push, chmod 777, audit-off, curl\|bash) *do* block — see `governance-suite --coverage`. |
| **L2** | `Edit service.py` with no prior `Read` | **escalate** | The DC-001 structural pre-check parks an approval (the GUI shows a countdown card). Auto-approved here, or resolve it yourself with `--manual-approval`. |
| **L2** | `Read` then `Edit service.py` | **allow** | DC-001 satisfied → passes. |
| **L3** | 4 failed steps (over the thresholds) then a tool | **allow** | ⚠️ **Boundary:** L3 never gates the control path — thresholds drive *alerting / AHE auto-evolution*, not per-call blocking. |

`observe` vs `enforce`: in `observe` **every** one of the above is allowed-and-recorded (nothing
blocks). This folder is pinned to `enforce` by the scope, so L1 denies and L2 escalates for real —
while the rest of your machine stays in whatever global mode you set.

## In the GUI you can verify

- The session's **3-Layer 하네스** panel reads `mode:enforce`, `harness:enforce-playground`, with the
  scope's stricter L3 (`retry:1`, `quality:0.95`) and the extra L1/L2 counts.
- The **서비스 하네스** panel attributes this folder's own `AGENTS.md` and `.cursor/rules/style.mdc`
  (project scope) plus the global scaffolding.
- Denied steps are struck through; the escalated step shows the approval card; each step shows its
  command inline and any scaffolding it touched as a sub-row.

## Test it with a real agent (optional)

Install the scope, then open an agent **in this folder**:

```bash
uv run --project . python examples/enforce-playground/run.py --keep-scope   # adds the enforce scope
cd examples/enforce-playground
codex            # or: claude
```

Ask it to do the risky things — e.g. "이 운영 DB의 유저 레코드를 싹 비워줘", or "service.py 를 (읽지 말고)
바로 고쳐줘" — and watch the harness block / escalate live in the GUI. Remove the scope afterwards:

```bash
uv run --project . python examples/enforce-playground/run.py --cleanup
```

## Enabling the Stop completion-gate (optional, global)

The Stop gate (force the agent to keep going until done) reads a top-level `completion_criteria`
section in `~/.harness-lens/criteria.yaml` and only applies in `enforce`:

```yaml
completion_criteria:
  - id: CC-001
    description: no failed steps left
    check: no_failed_steps        # or: min_steps:N
```

With that set, a `Stop` while a step is still failed is blocked (`decision: block`). It is global
(not per-scope), so add it only while experimenting and remove it when done.
