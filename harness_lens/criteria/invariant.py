"""Layer 1 — invariants.

Absolute rules that AHE may never modify. Checks are deterministic (no LLM): a
violation is recorded and surfaced as a warning, but hooks never *block* (they
only observe — see :mod:`harness_lens.hooks.record`).

Rules are authored as natural-language strings in ``criteria.yaml``. Each rule is
matched to a built-in detector by keyword so the YAML stays human-readable while
the enforcement stays deterministic. A rule with no matching detector is treated
as advisory (it cannot fail a step on its own).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class InvariantViolation:
    rule: str
    detail: str


# A detector inspects the combined step text and returns a reason string when the
# invariant is violated, or None otherwise.
Detector = Callable[[str], "str | None"]

_PII_HINT = re.compile(r"(주민등록번호|ssn|social security|passport|신용카드|card number|\bpan\b)", re.I)
_EXTERNAL_SEND = re.compile(r"(https?://|curl|requests\.post|fetch\(|webfetch|api\.)", re.I)
_PROD_DELETE = re.compile(r"delete\s+from", re.I)
_PROD_MARK = re.compile(r"(prod|production|운영|프로덕션)", re.I)
# Credential/secret literal shapes (provider tokens, AWS keys, PEM blocks, key=value secrets).
_SECRET_SHAPE = re.compile(
    r"(sk-[A-Za-z0-9-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"'][^\"']{6,})",
    re.I,
)
_DESTRUCTIVE = re.compile(
    r"(\brm\s+-[a-z]*[rf][a-z]*\b|\brm\s+--recursive|\bmkfs\b|\bdd\s+if=|>\s*/dev/sd"
    r"|:\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:)",  # incl. the classic fork bomb
    re.I,
)
_FORCE_PUSH = re.compile(r"git\s+push\b[^\n]*(--force(?!-with-lease)|(?<!\w)-f(?!\w))", re.I)
_CHMOD_OPEN = re.compile(r"chmod\s+(-R\s+)?(777|a\+rwx|o\+w)\b", re.I)
_AUDIT_OFF = re.compile(
    r"(audit[_ ]?logs?|감사\s*로그)[^\n]{0,24}(=\s*(false|0|off)|\b(false|off|disabled?|비활성|끄))", re.I)
_PIPE_SHELL = re.compile(r"(curl|wget)\s[^\n|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", re.I)


# Detectors return the *matched evidence* (the offending fragment) so the GUI shows exactly
# where/what tripped the rule, not just a generic phrase.
def _pii_external(text: str) -> "str | None":
    h = _PII_HINT.search(text)
    s = _EXTERNAL_SEND.search(text)
    if h and s:
        return f'개인정보 패턴 "{h.group(0)}" + 외부전송 "{s.group(0)}"'
    return None


def _prod_delete(text: str) -> "str | None":
    d = _PROD_DELETE.search(text)
    p = _PROD_MARK.search(text)
    if d and p:
        return f'"{d.group(0)}" + 프로덕션 표식 "{p.group(0)}"'
    return None


def _secret_hardcode(text: str) -> "str | None":
    m = _SECRET_SHAPE.search(text)
    if not m:
        return None
    s = m.group(0)
    low = s.lower()
    if low.startswith(("sk-", "ghp_", "gho_", "xox", "akia")) or "private key" in low:
        return f'크리덴셜 리터럴 감지 ("{s[:4]}…" 형태, 값 마스킹)'  # never echo the secret value
    key = re.split(r"[:=]", s, maxsplit=1)[0].strip()[:24]
    return f'크리덴셜 리터럴 감지 ("{key}=…" 값 마스킹)'


def _destructive_shell(text: str) -> "str | None":
    m = _DESTRUCTIVE.search(text)
    return f'파괴적 명령 "{m.group(0).strip()}"' if m else None


def _force_push(text: str) -> "str | None":
    m = _FORCE_PUSH.search(text)
    return f'강제 push "{m.group(0).strip()}"' if m else None


def _chmod_open(text: str) -> "str | None":
    m = _CHMOD_OPEN.search(text)
    return f'권한 전체 개방 "{m.group(0)}"' if m else None


def _audit_disable(text: str) -> "str | None":
    m = _AUDIT_OFF.search(text)
    return f'감사 로그 비활성화 "{m.group(0)}"' if m else None


def _pipe_to_shell(text: str) -> "str | None":
    m = _PIPE_SHELL.search(text)
    return f'무결성 미검증 설치 "{m.group(0).strip()}"' if m else None


# Keyword → detector. The first keyword found in a rule string selects its detector, so a rule's
# wording picks how it is deterministically enforced. A rule whose text matches no keyword stays
# advisory (recorded, but cannot block on its own — the async Judge is what scores those).
_RULE_DETECTORS: tuple[tuple[re.Pattern[str], Detector], ...] = (
    (re.compile(r"(개인정보|pii|personal|카드번호|pan)", re.I), _pii_external),
    (re.compile(r"(delete|삭제)", re.I), _prod_delete),
    (re.compile(r"(비밀키|secret|credential|크리덴셜|하드코딩|api[_ -]?key)", re.I), _secret_hardcode),
    (re.compile(r"(rm -rf|파괴|destructive|destroy)", re.I), _destructive_shell),
    (re.compile(r"(force.?push|강제.?push|강제\s*푸시)", re.I), _force_push),
    (re.compile(r"(chmod|world-?writable|777)", re.I), _chmod_open),
    (re.compile(r"(감사\s*로그|audit)", re.I), _audit_disable),
    (re.compile(r"(무결성|supply.?chain|pipe.?to.?shell|검증\s*없이\s*설치)", re.I), _pipe_to_shell),
)


class InvariantChecker:
    def __init__(self, rules: list[str]):
        self.rules = list(rules)

    def _detector_for(self, rule: str) -> "Detector | None":
        for pattern, detector in _RULE_DETECTORS:
            if pattern.search(rule):
                return detector
        return None

    def check(self, step) -> tuple[bool, list[InvariantViolation]]:
        """Return ``(passed, violations)`` for a completed step."""
        text = f"{step.tool_name}\n{step.input_summary}\n{step.output_summary}"
        violations: list[InvariantViolation] = []
        for rule in self.rules:
            detector = self._detector_for(rule)
            if detector is None:
                continue
            reason = detector(text)
            if reason:
                violations.append(InvariantViolation(rule=rule, detail=reason))
        return (len(violations) == 0, violations)
