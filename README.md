# harness-lens

Observe agentic coding harnesses (Claude Code, Codex CLI) via hooks + an MCP
server, reconstruct each session into a **Flow / Task / Step** trajectory, and
apply **AHE** (Agentic Harness Engineering) to evolve the *external* scaffolding
the harness controls — never the black-box internals of the agent itself.

Reference: AHE design concept (arXiv:2604.25850).

## Design stance — AHE over a black box

Claude Code and Codex are black boxes: their internal system prompts, tools, and
middleware are not directly accessible. `harness-lens` positions **itself** as
the external harness, and limits AHE evolution to components it can actually
control and revert:

- `CLAUDE.md` / `AGENTS.md` (agent instructions ≈ system prompt)
- hook configuration (execution flow ≈ middleware)
- `harness-lens`'s own code (reconstructor, QA criteria, skills)

The agent's internal prompt/tools/middleware are **never** modification targets.

## Three observability pillars

1. **Component** — file-level, revertible edits (originals backed up first).
2. **Experience** — 3-tier trajectory compression consumed by a Debugger agent.
3. **Decision** — every change ships with a falsifiable prediction, verified the
   next round; a miss auto-rolls-back.

## Three-Layer criteria — enforced and monitored

The harness is managed as three layers (see `criteria.yaml`):

- **Layer 1 — Invariants**: absolute rules, never AHE-editable.
- **Layer 2 — Domain criteria**: behavioural rules scored by an LLM Judge.
- **Layer 3 — QA thresholds**: the only AHE-evolvable layer (retries, latency, …).

`install` (or `harness-lens enforce`) renders these layers into the agent
instruction file (`CLAUDE.md` / `AGENTS.md`) as a managed block — the closest
AHE-controllable analogue of the agent's system prompt — so Claude Code / Codex
operate under them. The block sits between markers, so re-enforcement refreshes
only the managed region and leaves your own instructions intact. View the layers
with `harness-lens layers`; `show` annotates each Flow with its Layer 1/2/3
status, and `status` summarises the layers currently in force.

## Install

`harness-lens` is a Python package (3.10+). The hooks and MCP server run it via
`uvx`, so no global install is required — `uvx` builds the env on demand.

```bash
uvx --from "harness-lens[all]" harness-lens install
```

`install` auto-detects the harness, wires its hooks, and initialises the runtime
at `~/.harness-lens/` (`ledger.db`, `criteria.yaml`, `components/`, `backups/`).
The `[all]` extra pulls in `anthropic` (a direct-API Judge / diagnose-evolve
backend) and `mcp` (the server).

### Invoke as a skill, not only a CLI

`install` also drops a **SKILL wrapper** so the host agent can reach harness-lens
without you typing the CLI. Both harnesses load skills the same way — a `SKILL.md`
with `name` + `description` frontmatter under `<config>/skills/<name>/` — so the
wrapper lands at `~/.claude/skills/harness-lens/SKILL.md` (Claude Code) or
`~/.codex/skills/harness-lens/SKILL.md` (Codex), triggerable as `harness-lens`. Its
body wraps the same subcommands (`show`, `harness`, `diagnose`, `evolve`, …) and is
generated with the same invocation prefix the hooks use, so it works whether or not
the package is installed globally. Re-emit or refresh it any time with
`harness-lens skill` (`--print` to inspect without writing).

### LLM backend — no API key required

Observation (`show`, `status`, hook recording, Layer 1) needs no LLM at all. The
LLM-backed features — `diagnose`, `evolve`, and the Layer-2 Judge — resolve a
backend in this order:

1. `HARNESS_LENS_LLM_BACKEND` (`api` \| `claude` \| `codex`) if set;
2. `ANTHROPIC_API_KEY` → call the Anthropic API directly;
3. otherwise → **delegate to the detected host CLI** (`claude -p` / `codex exec`),
   reusing the host's existing login.

So when harness-lens is attached to Claude Code or Codex, you can run
`diagnose`/`evolve` **without setting `ANTHROPIC_API_KEY`** — the host performs
the model call under its own credentials. (The nested host call runs with
`HARNESS_LENS_DISABLE=1` so it isn't re-observed.)

Force a platform when both are present:

```bash
uvx --from "harness-lens[all]" harness-lens install --platform codex
```

## Using with Claude Code

`install` merges `mcpServers` + hooks into `~/.claude/settings.json` (your
existing settings are backed up first). Claude Code's `PreToolUse` intercepts
**every** tool, so trajectories are fully observed — no gaps. The editable
instruction file is `CLAUDE.md`.

```bash
uvx --from "harness-lens[all]" harness-lens install   # detects Claude Code
# …work in Claude Code as usual; every session is tracked automatically…
harness-lens show         # recent Flows with Layer-2 scores
harness-lens status       # 3-Layer + prediction hit-rate + Judge
```

## Using with Codex CLI

Codex differs from Claude Code in ways `harness-lens` accounts for:

- **No `SessionEnd` hook.** A Flow ends on `Stop`; a new prompt within 30s
  reopens the same Flow, otherwise it's a fresh one.
- **Narrow `PreToolUse`.** Codex only intercepts `Bash`, `apply_patch`, and MCP
  tools. Anything else (e.g. web search) is recorded as a **`관측 불가` gap**
  rather than guessed — check the gap ratio in `show` / `status`.
- **Trusted projects only.** Codex loads project-local hooks only when the
  project is trusted, so `install` prints a trust reminder. Global hooks go to
  `~/.codex/hooks.json`.
- **MCP is registered separately.** `install` does not auto-edit `config.toml`;
  it prints the `codex mcp add …` command to run. If `config.toml` already has a
  `[hooks]` table, `install` writes `hooks.json` and warns you to merge manually.
- The editable instruction file is `AGENTS.md`.

```bash
uvx --from "harness-lens[all]" harness-lens install --platform codex
# …trust the project, then register MCP using the printed `codex mcp add` line…
codex "테스트 실패 고쳐줘"
harness-lens show         # Flow/Task[category]/Step + gap ratio
```

## Commands

| Command | Purpose |
| --- | --- |
| `harness-lens install [--platform NAME]` | wire hooks + MCP, init runtime (also drops the SKILL wrapper) |
| `harness-lens skill [--platform NAME] [--print]` | (re)install the SKILL wrapper for the host harness, or print it |
| `harness-lens harness [--project DIR]` | inspect the harness applied to a project (components + Flow/Task/Step + 3-Layer) |
| `harness-lens enforce [--platform NAME]` | write the 3-Layer criteria into the instruction file (≈ system prompt) |
| `harness-lens layers` | show the 3-Layer criteria currently enforced |
| `harness-lens show [--fail] [--limit N]` | recent Flows (per-flow Layer 1/2/3 + gap ratio) |
| `harness-lens diagnose` | Pillar 2 — Debugger agent (needs API key) |
| `harness-lens evolve [--apply ID --yes]` | Pillar 3 — proposals; apply a candidate |
| `harness-lens verify` | verify predictions → confirm / roll back |
| `harness-lens review [--sample ID --label 0..1]` | Judge labelling |
| `harness-lens rollback` | revert the last applied change |
| `harness-lens status` | 3-Layer + hit-rate + Judge + gap ratio |
| `harness-lens serve` | run the MCP server (used by the harness) |
| `harness-lens gui [--port N] [--no-browser]` | launch the local web GUI to monitor + edit the 3-Layer harness |
| `harness-lens benchmark` | verify the managed harness honours its 3-Layer boundaries (CI-friendly exit code) |

Gap-dominated patterns (over 50% unobserved, i.e. mostly Codex gaps) are held
back from evolution — a prediction built on missing evidence isn't trustworthy.

## Web GUI

`harness-lens gui` launches a localhost-only dashboard (Python stdlib, no extra
deps) that renders the same Flow/Task/Step trajectory and 3-Layer view as the
CLI, and lets you edit the one AHE-evolvable layer from the browser:

- **Monitor** — recent Flows with per-flow Layer 1/2/3 status and gap ratio.
- **Status** — Judge recommendation, prediction hit-rate, candidate counts.
- **Edit** — Layer 1 (invariants) and Layer 2 (domain criteria) are shown
  read-only by design; Layer 3 (QA thresholds) is editable. Saving persists to
  `criteria.yaml` (original backed up) and re-enforces the managed instruction
  block so Claude Code / Codex immediately see the new thresholds.

```bash
harness-lens gui            # opens http://127.0.0.1:8765/ in your browser
harness-lens gui --port 9000 --no-browser
```

It binds to loopback only — the Layer-3 edit endpoint is meant for the single
local user, not the network.

## Benchmark

`harness-lens benchmark` checks that the managed harness actually *behaves* the
way its 3-Layer criteria say it should. It runs synthetic Flows through the same
deterministic Layer-1 (invariant) and Layer-3 (QA threshold) machinery the live
harness uses and asserts the harness reacts exactly at its configured
boundaries:

- **Layer 1** — a clean step is never flagged; a step matching an *active*
  invariant (one your `criteria.yaml` turns on) is flagged.
- **Layer 3** — failure / retry / latency / quality patterns trip *at* the
  threshold and stay quiet just below it.

Layer-3 expectations are derived from the **effective** thresholds, so the
benchmark stays correct after you (or AHE) edit Layer 3 — it always asks "does
the harness honour its *current* boundary?". Layer 2 (the LLM Judge) is
non-deterministic and out of scope, so the benchmark runs fully offline and
exits non-zero on any failure, making it usable as a CI gate.

```bash
harness-lens benchmark
```
