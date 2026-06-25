# harness-lens — Codex CLI integration

**Recommended (Codex 0.140+ plugin system — verified on 0.140.0):** install the plugin. It carries
the hooks, MCP server, and skill, plus a SessionStart hook that auto-starts the daemon.

```sh
codex plugin marketplace add JY-1019/harness-lens     # or a local path to this repo
codex plugin add harness-lens@harness-lens
```

The Codex plugin lives at [`plugins/harness-lens/`](../plugins/harness-lens) (manifest
`.codex-plugin/plugin.json`, hooks `hooks.json`, MCP `.mcp.json`, launcher `bin/hl`); the marketplace
manifest is [`.agents/plugins/marketplace.json`](../.agents/plugins/marketplace.json). Codex resolves
`${CODEX_PLUGIN_ROOT}` to the installed copy, and the launcher fetches the package from git via
`uvx` (the plugin installs as an isolated directory, so it can't reuse the repo's bundled venv).
Command hooks must be trusted once via `/hooks` (or run Codex with `--dangerously-bypass-hook-trust`
for automation).

---

## Manual fallback (older Codex without the plugin system)

Wire `~/.codex/` by hand with the two snippets below. Replace `PLUGIN_DIR` with an absolute path to a
checkout of this repo (the directory holding `bin/hl` and `pyproject.toml`).

## 1. MCP server — `~/.codex/config.toml`

```toml
[mcp_servers.harness-lens]
command = "PLUGIN_DIR/bin/hl"
args = ["serve"]
```

## 2. Hooks — `~/.codex/hooks.json`

Codex (≥0.139) uses the Claude-Code hook schema, so this mirrors the Claude plugin's
`hooks/hooks.json` with the source set to `codex`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
        { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl daemon start >/dev/null 2>&1 || true", "timeout": 25 },
        { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 10 }
      ] }
    ],
    "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 15 } ] } ],
    "PreToolUse":       [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 90 } ] } ],
    "PostToolUse":      [ { "matcher": ".*", "hooks": [ { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 15 } ] } ],
    "SubagentStop":     [ { "hooks": [ { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 90 } ] } ],
    "Stop":             [ { "hooks": [ { "type": "command", "command": "HARNESS_LENS_PLATFORM=codex PLUGIN_DIR/bin/hl hook-relay codex", "timeout": 90 } ] } ]
  }
}
```

Codex requires you to review command hooks once via `/hooks` before they run.

## 3. Verify

```sh
PLUGIN_DIR/bin/hl daemon status      # → daemon: running …
open http://127.0.0.1:7700/ui        # live UI
```

The SessionStart hook keeps the daemon up; the relay hooks gate/record each tool call against the
3-Layer harness, exactly as in the Claude Code plugin.
