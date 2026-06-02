"""Small LLM client abstraction.

The Layer-2 Judge (:mod:`harness_lens.criteria.domain`) and the Pillar 2/3
agents (:mod:`harness_lens.agents`) all need an LLM. Centralising the Anthropic
call here keeps a single import boundary and a single place to swap the backend
or stub it in tests.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Protocol


class LLMUnavailable(RuntimeError):
    """Raised when no LLM backend can be constructed (missing SDK or API key)."""


class LLMClient(Protocol):
    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str: ...


# The default model is overridable via ``HARNESS_LENS_MODEL`` so a deployment can pin
# whatever model id its installed Anthropic SDK / account actually exposes, without a
# code change. ``_default_model()`` resolves the env override at call time.
DEFAULT_MODEL = "claude-opus-4-7"


def _default_model() -> str:
    return os.environ.get("HARNESS_LENS_MODEL", "").strip() or DEFAULT_MODEL


class AnthropicClient:
    """Thin wrapper over the Anthropic Messages API.

    The SDK is imported lazily so that the rest of harness-lens stays importable
    (and the hook path stays dependency-light) without ``anthropic`` installed.
    """

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model or _default_model()
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic  # noqa: WPS433 (lazy import is intentional)
        except ImportError as exc:
            raise LLMUnavailable("anthropic SDK is not installed (pip install harness-lens[agents])") from exc
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def ensure_ready(self) -> None:
        """Raise :class:`LLMUnavailable` unless an API key and the SDK are present."""
        self._ensure_client()

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        client = self._ensure_client()
        message = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [block.text for block in message.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()


# How long to wait on a host-CLI completion before giving up (a diagnose/evolve prompt is
# a single short turn; overridable for slow machines).
def _cli_timeout() -> int:
    raw = os.environ.get("HARNESS_LENS_CLI_TIMEOUT", "").strip()
    try:
        return int(raw) if raw else 180
    except ValueError:
        return 180


class _HostCLIClient:
    """Delegate a completion to the *host harness's* CLI, reusing its existing auth.

    When harness-lens is attached to Claude Code or Codex, the host is already logged in
    (subscription / OAuth / its own key). Shelling out to the host CLI in non-interactive mode
    lets diagnose/evolve/Judge run with **no separate ANTHROPIC_API_KEY** — the host performs
    the model call under its own credentials. ``HARNESS_LENS_DISABLE=1`` is exported to the
    child so the nested CLI's own harness-lens hooks are inert (no recording/Judge recursion).
    """

    binary: str = ""
    label: str = ""

    def __init__(self, model: Optional[str] = None):
        # None → let the host pick its default model (Codex models are not Claude model ids, so
        # we never force one); an explicit HARNESS_LENS_MODEL still overrides per subclass.
        self._model = model

    def ensure_ready(self) -> None:
        if shutil.which(self.binary) is None:
            raise LLMUnavailable(f"{self.label} CLI ({self.binary!r}) not found on PATH")

    def _child_env(self) -> dict:
        env = dict(os.environ)
        env["HARNESS_LENS_DISABLE"] = "1"  # make the child's harness-lens hooks a no-op
        env.pop("HARNESS_LENS_JUDGE_IN_HOOK", None)
        return env

    def _run(self, cmd: list[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=_cli_timeout(),
                env=self._child_env(), cwd=cwd, check=False,
            )
        except FileNotFoundError as exc:
            raise LLMUnavailable(f"{self.label} CLI not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMUnavailable(f"{self.label} CLI timed out after {_cli_timeout()}s") from exc


class ClaudeCodeCLIClient(_HostCLIClient):
    binary = "claude"
    label = "Claude Code"

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        self.ensure_ready()
        cmd = [
            self.binary, "-p", prompt,
            "--system-prompt", system,
            "--output-format", "text",
            "--no-session-persistence",
        ]
        # NB: never pass --bare here — it forces key-only auth and ignores the OAuth/keychain
        # login we are specifically trying to reuse.
        model = self._model or os.environ.get("HARNESS_LENS_MODEL", "").strip()
        if model:
            cmd += ["--model", model]
        result = self._run(cmd)
        if result.returncode != 0:
            raise LLMUnavailable(
                f"claude -p failed (exit {result.returncode}): {result.stderr.strip()[:300]}"
            )
        return result.stdout.strip()


class CodexCLIClient(_HostCLIClient):
    binary = "codex"
    label = "Codex CLI"

    def complete(self, system: str, prompt: str, *, max_tokens: int = 1024) -> str:
        self.ensure_ready()
        # Codex has no separate system-prompt flag, so fold it into the single prompt; the
        # agents parse JSON out of the reply regardless of surrounding text.
        full = f"{system}\n\n{prompt}" if system else prompt
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "last.txt"
            cmd = [
                self.binary, "exec",
                "--skip-git-repo-check",
                "--color", "never",
                "-s", "read-only",
                "--output-last-message", str(out_file),
            ]
            model = self._model or os.environ.get("HARNESS_LENS_MODEL", "").strip()
            if model:
                cmd += ["-m", model]
            cmd.append(full)
            result = self._run(cmd)
            if result.returncode != 0:
                raise LLMUnavailable(
                    f"codex exec failed (exit {result.returncode}): {result.stderr.strip()[:300]}"
                )
            if out_file.exists():
                text = out_file.read_text(encoding="utf-8").strip()
                if text:
                    return text
            # Fall back to stdout if the final-message file was empty/absent.
            return result.stdout.strip()


_HOST_CLI_CLIENTS = {
    "claude-code": ClaudeCodeCLIClient,
    "codex": CodexCLIClient,
}


def default_client(model: Optional[str] = None) -> LLMClient:
    """Resolve an LLM backend.

    Order: an explicit ``HARNESS_LENS_LLM_BACKEND`` override, then a direct Anthropic API key
    (fastest path, unchanged), then the **detected host harness CLI** (reuses the host login, so
    no key is needed when attached to Claude Code / Codex). Falls back to the Anthropic client,
    whose ``ensure_ready`` raises a clear ``LLMUnavailable`` when nothing is configured.
    """
    backend = os.environ.get("HARNESS_LENS_LLM_BACKEND", "").strip().lower()
    if backend in ("api", "anthropic"):
        return AnthropicClient(model=model)
    if backend in ("claude", "claude-code"):
        return ClaudeCodeCLIClient(model=model)
    if backend == "codex":
        return CodexCLIClient(model=model)

    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return AnthropicClient(model=model)

    from .detector import detect

    platform = detect()
    if platform is not None:
        cli_client = _HOST_CLI_CLIENTS.get(platform.name)
        if cli_client is not None:
            return cli_client(model=model)
    return AnthropicClient(model=model)
