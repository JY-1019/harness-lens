# governance-suite

A **complex, tiered governance** example. Where `enforce-playground` shows one simple scope, this
models an org with **governance zones** that each get a *different* 3-Layer harness, and shows how
scope resolution routes every session to the right one. `governance.py` is both a demo and a
self-checking test (prints expected vs actual + PASS/FAIL) and populates the live GUI.

Non-destructive: it installs 5 additive **scopes** on top of your global base (no base edits) and
removes them at the end. Use `--keep-scopes` to leave them, `--cleanup` to remove them.

## Two lenses

- **Layer 1 = governance / compliance** — data protection (PCI: no PAN export), production safety
  (no direct prod DELETE), change-control (no direct prod apply/destroy), audit (don't disable logs).
- **Layer 2 = business logic + process** — payment amount/refund rules, ordering invariants,
  "regression tests required", "read a file before editing it" (DC-001).

## Zones (scope precedence: session-pin > exact folder > path-prefix > global)

| Zone | Folder | Match | Mode | Governance highlights |
|------|--------|-------|------|------------------------|
| **Production** | `infra/prod/` | exact `cwd` | enforce, strictest L3 | L1 change-control + audit; L2 plan-then-approve |
| **Payments** | `services/payments/` | exact `cwd` | enforce | L1 PCI (no PAN export); L2 amount/refund/regression rules |
| **App** | `services/app/` | *(none)* → prefix baseline | enforce | global L1 + GOV-000 traceability |
| **Sandbox** | `sandbox/` | exact `cwd` | **observe** | governance watches, never blocks |
| **Session pin** | (any folder) | `session_id` | enforce | overrides the folder — proves session > cwd |

## Run

```bash
harness-lens daemon start
uv run --project . python examples/governance-suite/governance.py
```

Flags: `--keep-scopes`, `--manual-approval` (resolve the L2 escalation yourself in the GUI),
`--source claude_code`, `--cleanup`.

### Verify a *complex* harness actually gates: `--coverage`

```bash
uv run --project . python examples/governance-suite/governance.py --coverage
```

Installs a deliberately complex enforce harness and probes **each rule** with a real violating
action, printing whether it is **enforced** (deny/escalate) or **advisory** (allow). This is how you
confirm "complex 3-Layer + enforce = actually hooked." What gates deterministically today:

| Layer-1 rule (by wording) | Detector | Result |
|---|---|---|
| prod `DELETE` | prod-delete | **deny** |
| personal data → external | pii-external | **deny** |
| hardcoded secret/credential | secret-shape | **deny** |
| `rm -rf` / `mkfs` / `dd` | destructive-shell | **deny** |
| `git push --force` to main | force-push (allows `--force-with-lease`) | **deny** |
| `chmod 777` | world-writable | **deny** |
| disabling audit logs | audit-off | **deny** |
| `curl … \| bash` | pipe-to-shell | **deny** |
| free-text rule (e.g. "no TODO") | *(none)* | allow (advisory) |

**Layer 2:** the structural DC-001 (read-before-edit) escalates in real time; other natural-language
L2 criteria are scored by the async Judge (sampled), not gated synchronously. **Layer 3** never gates
(monitoring/auto-evolution). So enforcement = *detector-backed L1 + DC-001*; everything else is
advisory/Judge — a rule only blocks when its wording maps to a detector (see
`harness_lens/criteria/invariant.py`).

## What it proves (9 scenarios, each PASS/FAIL)

- **Production** denies prod DELETE (L1) and PII→external (L1); `terraform destroy` is **allowed**
  (the change-control rule has no deterministic detector → advisory, scored by the async Judge — a
  documented Layer-1 boundary).
- **Payments** escalates an edit-to-an-unread-file (L2 / DC-001), allows it after a read. Its
  business-logic L2 criteria (amount/refund/regression) show in the harness and are Judge-scored.
- **App** has no exact scope, so the suite-wide **prefix** baseline applies (enforce): global L1
  deny + DC-001 escalation.
- **Sandbox** is **observe**: the *same* prod-DELETE that's denied in prod is merely recorded here.
- **Session pin**: a session running in `sandbox/` (observe by folder) is forced to **enforce** by a
  `session_id` scope and denies the delete — demonstrating `session > cwd` resolution.

## In the GUI

Open each `[codex] <zone>` session and click **⚙ 3-Layer 하네스** to see the applied rules with
`[전역]` vs `[이 프로젝트]` provenance — the zones show visibly different L1/L2/L3 and mode. Denied
steps are struck through with a red `L1`; the escalated step shows the approval card. The
**서비스 하네스** panel attributes each zone's own `AGENTS.md` + `.cursor/rules/security.mdc`.

## Optional: the Stop completion-gate (global)

Add a top-level `completion_criteria` to `~/.harness-lens/criteria.yaml` (applies in enforce only):

```yaml
completion_criteria:
  - id: CC-001
    description: no failed steps left
    check: no_failed_steps        # or: min_steps:N
```
