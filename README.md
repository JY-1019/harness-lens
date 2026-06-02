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
| `harness-lens install [--platform NAME]` | wire hooks + MCP, init runtime |
| `harness-lens show [--fail] [--limit N]` | recent Flows (Layer-2 + gap ratio) |
| `harness-lens diagnose` | Pillar 2 — Debugger agent (needs API key) |
| `harness-lens evolve [--apply ID --yes]` | Pillar 3 — proposals; apply a candidate |
| `harness-lens verify` | verify predictions → confirm / roll back |
| `harness-lens review [--sample ID --label 0..1]` | Judge labelling |
| `harness-lens rollback` | revert the last applied change |
| `harness-lens status` | 3-Layer + hit-rate + Judge + gap ratio |
| `harness-lens serve` | run the MCP server (used by the harness) |

Gap-dominated patterns (over 50% unobserved, i.e. mostly Codex gaps) are held
back from evolution — a prediction built on missing evidence isn't trustworthy.
