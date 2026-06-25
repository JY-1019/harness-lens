# harness-lens — Codex CLI integration

harness-lens ships a Codex plugin manifest at [`.codex-plugin/plugin.json`](../.codex-plugin/plugin.json).
If your Codex build supports plugin auto-load (`codex plugin install …`), installing the plugin is
enough — it reuses the same `bin/hl` launcher and the `harness-lens hook-relay codex` / `serve`
entrypoints described below, with `${CODEX_PLUGIN_ROOT}` pointing at the installed plugin.

If your Codex version does **not** yet auto-load plugin hooks/MCP, wire it manually once with the two
snippets below. Replace `PLUGIN_DIR` with this repo/plugin's absolute path (the directory that holds
`bin/hl` and `pyproject.toml`); under a real plugin install that is `${CODEX_PLUGIN_ROOT}`.

The launcher resolves the platform itself: `bin/hl relay` becomes `hook-relay codex` whenever
`CODEX_PLUGIN_ROOT` is set or `HARNESS_LENS_PLATFORM=codex`, so the commands below are explicit for
the manual (non-plugin) case.

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
