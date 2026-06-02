"""Small LLM client abstraction.

The Layer-2 Judge (:mod:`harness_lens.criteria.domain`) and the Pillar 2/3
agents (:mod:`harness_lens.agents`) all need an LLM. Centralising the Anthropic
call here keeps a single import boundary and a single place to swap the backend
or stub it in tests.
"""

from __future__ import annotations

import os
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


def default_client(model: Optional[str] = None) -> AnthropicClient:
    return AnthropicClient(model=model)
