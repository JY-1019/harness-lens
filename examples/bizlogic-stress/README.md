# bizlogic-stress

A **deliberately huge** 3-Layer harness, enforced, then hammered — to confirm a very long, complex
harness (dozens of Layer-2 business rules) actually loads, applies, shows in the GUI, and still gates
correctly under a large number of tool calls. `stress.py` is a self-checking test (PASS/FAIL).

Non-destructive: one additive scope on this folder (exact `cwd`, enforce), removed at the end unless
`--keep-scope`.

## The harness

- **Layer 1 — governance (12):** 9 detector-backed (prod delete, PII→external, card/PAN export,
  hardcoded secrets, `rm -rf`, force-push, `chmod 777`, audit-off, `curl|bash`) + 3 advisory.
- **Layer 2 — business logic (60):** orders, pricing/promo, tax, payments, refunds, inventory,
  shipping, fraud, KYC/AML, ledger/accounting, auth, privacy, API reliability, notifications, process
  (regression tests / migrations / schema compat) … natural-language criteria, Judge-scored.
- **Layer 3:** strict (retry 1, latency×2, failure 2, quality 0.95).

## Run

```bash
harness-lens daemon start
uv run --project . python examples/bizlogic-stress/stress.py            # default burst=120
uv run --project . python examples/bizlogic-stress/stress.py --burst 500 --keep-scope
uv run --project . python examples/bizlogic-stress/stress.py --three-layer  # all 3 project layers gate
uv run --project . python examples/bizlogic-stress/stress.py --velocity     # 결제 속도 제한 L2 detector
uv run --project . python examples/bizlogic-stress/stress.py --cleanup
```

## `--velocity`: the "결제 속도 제한" rule gates

The harness carries *"동일 카드로 단시간 다발 결제는 속도 제한을 적용한다"* (rule #105 / `BIZ-026`). A coding
hook can't watch the live system throttle real charges — that's runtime behaviour — but it **can** see
whether the charge code being authored carries a velocity guard. So this gates the structural proxy:

1. Editing `charge.py` to add charge handling **with no** `rate_limit`/`velocity`/`throttle` construct →
   **escalate** (`[BIZ-026] 동일 카드로 단시간 다발 결제는 속도 제한을 적용한다 — …가드가 보이지 않음`).
2. The same edit **with** a rate-limit guard present → **allow**.

(`_l2_payment_velocity` in `policy.py`; the `_PAYMENT_ACTION` signal + absent `_VELOCITY_GUARD`.)

## `--three-layer`: each project layer gates

Shows the project's *own* L1/L2/L3 (not just the L2 base DC-001) each producing a real enforce
decision, in one run:

1. **L1 (project rule)** — a hardcoded-secret edit → **deny** (detector-backed invariant).
2. **L2 (project rule)** — `git commit` with no prior test run → **escalate** (the *"회귀 테스트 동반"*
   structural detector).
3. **L3 (project limit)** — two failed steps reach `failure_count_trigger=2`, so the next action trips
   the **L3 circuit breaker → escalate** (review the run before continuing). L3 still doesn't inspect
   the individual action — it gates on the run's *accumulated* QA state (`policy.py` `_l3_breach`).

## What it checks

1. **Loads & applies** — the effective harness for this folder really carries all ~12 L1 + ~60 L2 +
   L3, in enforce (not silently dropped/truncated).
2. **Still gates at scale** — under the 70+ rule harness, every detector-backed L1 violation is
   **denied**, an edit-to-an-unread-file **escalates** (DC-001), and a normal command is allowed.
3. **Throughput** — fires `--burst` benign tool calls and reports ms/call + calls/s, with **zero false
   blocks**, so a big harness doesn't slow the hook path (≈1–2 ms/call locally).

It also checks **structural Layer-2**: the *"회귀 테스트 동반"* criterion makes a `git commit`/deploy
**without a prior test run escalate** (and allow once tests have run); a *"검증 우회 금지"* criterion
catches `--no-verify` / `skip ci`; the *"속도 제한"* criterion makes charge code authored **without a
velocity/rate-limit guard escalate** (see `--velocity`). These are deterministic L2 detectors
(`harness_lens/daemon/policy.py`, keyword→detector like L1) — no LLM.

> Honest scope: enforce gates, in real time, *detector-backed L1* (deny), *structural L2* (DC-001 +
> the process detectors above → escalate), and *L3 as a circuit breaker* (accumulated failures/retries
> cross the threshold → escalate; see `--three-layer`). **Value-level** L2 rules ("refund > charge",
> "amount ≤ 0") can't be seen from a tool call, so they stay **Judge-scored (sampled), not blocked
> synchronously**. To gate more rules in real time, add detectors in `invariant.py` (L1) or `policy.py`
> `_L2_DETECTORS` (L2).

## In the GUI

Open the `[codex] bizlogic-stress` session → **⚙ 3-Layer 하네스** shows a long L1/L2/L3 list
(`[전역]` vs `[이 프로젝트]`); the denied probe steps are struck through with a red `L1`, and the
unread-edit shows the approval card.
