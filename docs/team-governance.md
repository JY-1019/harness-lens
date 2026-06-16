# Team governance — repo-committed policy (Phase 1)

harness-lens is otherwise rooted in each developer's `~/.harness-lens` (per-machine, per-user). But
governance is a *team* concern. Phase 1 lets a project ship its 3-Layer harness **in the repo** so it
is distributed by git and reviewed via PR, instead of being wired up locally by every developer.

## How it works

Commit a `.harness-lens/policy.yaml` at your repo root:

```yaml
name: payments-governance
invariants:                       # Layer 1 — governance / safety
  - "프로덕션 DB에 직접 DELETE 를 실행하지 않는다"
domain_criteria:                  # Layer 2 — process / behaviour
  - id: BIZ-001
    description: "결제·정산 로직 변경 시 회귀 테스트를 동반한다"
    weight: 1.5
layer3:                           # Layer 3 — QA thresholds
  failure_count_trigger: 2
mode: enforce                     # optional — pin this repo to observe/enforce
```

The daemon discovers it by walking up from each session's `cwd` and applies it to **every session
inside that repo**, composed as:

```
personal base (~/.harness-lens)  →  repo policy (.harness-lens/policy.yaml)  →  personal scope overlay
```

`apply_scope` is additive: the repo's invariants/criteria are *added*, its `layer3` overrides, and its
optional `mode` pins the project. Edits to the committed file hot-reload (mtime-cached) — no daemon
restart. In the GUI the repo policy shows a purple `📦 repo 정책` chip and tags its rules `📦 repo`.

## Why this shape

- **Distribution + review for free** — a PR to `.harness-lens/policy.yaml` (with `CODEOWNERS` on that
  path and branch protection) is the governance change process. Everyone who clones gets the same
  harness; history/blame give an audit trail.
- **Maps to the layer split** — L1 = org/safety governance, L2 = business/process rules, L3 = QA
  thresholds. Org-wide L1 that spans many repos is a later phase (`extends:` shared bundles).

## Honest boundary

The local daemon is **fast feedback, not the authoritative gate**: a developer owns their machine and
can edit the file, set `observe`, or stop the daemon. So team enforcement that an individual cannot
weaken must live where they can't override it — **CI / pre-merge** (run the same `invariant.py` /
`policy.py` detectors over the agent's diff) and, later, a **team control plane** (authoritative
policy + central audit ledger + shared approval queue). This phase delivers the distribution, review
and version-control story; non-bypassable authority is the next phase.
