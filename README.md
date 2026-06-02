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

## Status

Scaffold only. Platform implementations land on dedicated branches:

- Claude Code platform support
- Codex CLI platform support
