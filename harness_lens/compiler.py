"""Rule compiler — classify L1/L2 rules into deterministic detectors vs semantic (advisory).

Runs at *authoring* time (the harness GUI / a CLI), **not** at hook time, so it may call an LLM
(slow). The synchronous hook gate stays LLM-free. For each user-authored rule it reports:

* whether the rule already maps to a **built-in detector** (deterministic — gates today);
* otherwise an LLM **classification**: ``deterministic`` (a regex/structural check can be
  synthesised from a single tool call's text) vs ``semantic`` (needs the Judge / CI — it cannot
  gate at hook time because the control path may not call an LLM);
* for a synthesised regex, a **self-test** against LLM-provided examples so an unverified detector
  is never promoted to "blocking" — it stays advisory until its positive/negative examples pass.

The LLM is resolved via :func:`harness_lens.llm.default_client`, which delegates to the host
Claude Code / Codex CLI when no API key is set — so the compile reuses the host's own login and
needs no ``ANTHROPIC_API_KEY``. This module never mutates ``criteria.yaml``; it only classifies.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from .llm import LLMClient, LLMUnavailable

# Reuse the live detector libraries as the single source of truth for "does this rule already map to
# a built-in deterministic detector?". Guarded so a refactor of these private tuples degrades to
# "no builtin match" rather than crashing the compiler import.
try:  # Layer-1 invariant detectors (keyword pattern → detector fn)
    from .criteria.invariant import _RULE_DETECTORS as _L1_DETECTORS
except Exception:  # noqa: BLE001
    _L1_DETECTORS = ()
try:  # Layer-2 structural detectors (keyword pattern → detector fn)
    from .daemon.policy import _L2_DETECTORS
except Exception:  # noqa: BLE001
    _L2_DETECTORS = ()


_SYSTEM = (
    "You are a policy compiler for an AI coding-agent guardrail. Each rule is enforced at tool-call "
    "time by a synchronous hook that CANNOT call an LLM — it can only run deterministic regex / "
    "structural checks over a single tool call's text (tool name + input). "
    "Classify each rule as 'deterministic' (a regex over one tool call's text can flag a violation "
    "with acceptable precision) or 'semantic' (it needs judgement, runtime behaviour, or comparing "
    "values across steps — not statically decidable from one tool call). For a deterministic rule, "
    "propose a Python `re` regex (intended to run case-insensitively) that matches the VIOLATING "
    "text, plus positive examples (should match) and negative examples (should NOT match). "
    'Answer ONLY with a JSON array. Each element: {"index": <int>, "classification": '
    '"deterministic"|"semantic", "reason": "<short, in the rule\'s language>", "regex": '
    '"<python re, or null>", "examples": {"positive": ["..."], "negative": ["..."]}}. '
    "No prose outside the JSON."
)

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.S)


def builtin_invariant_detector(rule: str) -> Optional[str]:
    """Name of the built-in Layer-1 detector this invariant maps to (so it gates today), or None."""
    for pattern, detector in _L1_DETECTORS:
        if pattern.search(rule or ""):
            return getattr(detector, "__name__", "builtin")
    return None


def builtin_l2_detector(description: str) -> Optional[str]:
    """Name of the built-in Layer-2 structural detector this criterion maps to, or None."""
    for pattern, detector in _L2_DETECTORS:
        if pattern.search(description or ""):
            return getattr(detector, "__name__", "builtin")
    return None


def verify_regex(regex: str, examples: dict) -> dict:
    """Self-test a proposed regex against its examples.

    A proposed detector is only safe to *gate* if it matches every positive example and no negative
    example (and has at least one positive). Otherwise it must stay advisory. Returns
    ``{"verified": bool, "detail": str}`` — the deterministic check that keeps a hallucinated regex
    from being trusted as a blocking rule.
    """
    try:
        rx = re.compile(regex, re.I)
    except (re.error, TypeError):
        return {"verified": False, "detail": "정규식 컴파일 실패"}
    pos = [e for e in (examples or {}).get("positive", []) if isinstance(e, str)]
    neg = [e for e in (examples or {}).get("negative", []) if isinstance(e, str)]
    pos_ok = sum(1 for e in pos if rx.search(e))
    neg_hit = sum(1 for e in neg if rx.search(e))
    verified = bool(pos) and pos_ok == len(pos) and neg_hit == 0
    detail = f"양성 {pos_ok}/{len(pos)} 매칭 · 음성 오탐 {neg_hit}/{len(neg)}"
    return {"verified": verified, "detail": detail}


def classify_rules(
    invariants: list,
    domain_criteria: list,
    llm: Optional[LLMClient] = None,
) -> dict:
    """Classify each L1/L2 rule into a ``detector`` (deterministic) or ``advisory`` (semantic) verdict.

    Built-in detector matches are reported with no LLM call. Rules with no built-in detector are sent
    to the LLM in **one batched call** for a deterministic-vs-semantic verdict plus a self-verified
    proposed regex. Returns ``{"rules": [...], "llm_used": bool, "note": str}``. Never raises on a
    missing/failed backend — those rules fall back to ``advisory`` with an explanatory note.
    """
    rules: list[dict] = []
    pending: list[dict] = []  # entries awaiting the LLM (no built-in detector)

    def add(entry: dict) -> None:
        rules.append(entry)
        if entry["builtin_detector"] is None:
            pending.append(entry)

    for rule in invariants or []:
        text = str(rule or "").strip()
        if not text:
            continue
        name = builtin_invariant_detector(text)
        add({
            "layer": 1, "id": None, "text": text, "builtin_detector": name,
            "classification": "deterministic" if name else None,
            "enforcement": "detector" if name else None,
            "source": "builtin" if name else "pending",
            "reason": "내장 detector 매칭 — 지금도 차단(gate)됩니다" if name else "",
            "proposed": None,
        })

    for dc in domain_criteria or []:
        if not isinstance(dc, dict):
            continue
        text = str(dc.get("description") or "").strip()
        if not text:
            continue
        name = builtin_l2_detector(text)
        add({
            "layer": 2, "id": dc.get("id") or None, "text": text, "builtin_detector": name,
            "classification": "deterministic" if name else None,
            "enforcement": "detector" if name else None,
            "source": "builtin" if name else "pending",
            "reason": "내장 구조 detector 매칭 — enforce 에서 ESCALATE" if name else "",
            "proposed": None,
        })

    note = ""
    llm_used = False
    if pending:
        client = llm if llm is not None else _resolve_client()
        if client is None:
            note = ("LLM 백엔드 없음 — 내장 detector 매칭만 표시합니다 "
                    "(Claude Code / Codex 로그인 또는 ANTHROPIC_API_KEY 필요).")
            for entry in pending:
                _mark_unclassified(entry, "LLM 미연결 — 분류 보류 (기본 advisory)")
        else:
            try:
                verdicts = _classify_with_llm(client, [e["text"] for e in pending])
                llm_used = True
            except LLMUnavailable as exc:
                note = f"LLM 호출 실패: {exc}"
                verdicts = {}
            for idx, entry in enumerate(pending):
                _apply_verdict(entry, verdicts.get(idx))

    counts = {
        "detector": sum(1 for r in rules if r["enforcement"] == "detector"),
        "advisory": sum(1 for r in rules if r["enforcement"] == "advisory"),
        "total": len(rules),
    }
    return {"rules": rules, "llm_used": llm_used, "note": note, "counts": counts}


def compiled_policy(rules: list, name: str = "compiled-harness") -> dict:
    """Build an exportable / importable policy dict from a classification report.

    It carries the standard ``.harness-lens/policy.yaml`` fields (``invariants`` / ``domain_criteria``)
    so the daemon and CI already understand it as a repo policy, PLUS a ``compiled`` annotation that
    records, per rule, *how* it is enforced — built-in vs generated+verified detector vs semantic
    (advisory) — and any generated regex. The repo-policy loader ignores ``compiled``, so importing
    the file yields a working policy while preserving the compilation as documentation.
    """
    invariants = [r["text"] for r in rules if r.get("layer") == 1 and r.get("text")]
    domain: list[dict] = []
    seq = 0
    for r in rules:
        if r.get("layer") != 2 or not r.get("text"):
            continue
        seq += 1
        domain.append({"id": r.get("id") or f"DC-{seq:03d}", "description": r["text"], "weight": 1})
    compiled: list[dict] = []
    for r in rules:
        item: dict = {
            "text": r.get("text"), "layer": r.get("layer"),
            "enforcement": r.get("enforcement"), "classification": r.get("classification"),
        }
        if r.get("id"):
            item["id"] = r["id"]
        if r.get("builtin_detector"):
            item["detector"] = "builtin:" + r["builtin_detector"]
        proposed = r.get("proposed")
        if isinstance(proposed, dict) and proposed.get("regex"):
            item["detector"] = "generated"
            item["regex"] = proposed["regex"]
            item["verified"] = bool(proposed.get("verified"))
        compiled.append(item)
    # The verified-generated subset is also emitted as a `detectors:` list — the section the policy
    # engine loads to actually gate (escalate) at hook time. Unverified regexes are excluded here, so
    # importing the policy can never promote a detector that did not pass its own examples.
    detectors = [
        {"layer": r.get("layer"), "rule": r.get("text"),
         "regex": r["proposed"]["regex"], "verified": True}
        for r in rules
        if isinstance(r.get("proposed"), dict)
        and r["proposed"].get("regex") and r["proposed"].get("verified")
    ]
    policy = {"name": name, "invariants": invariants, "domain_criteria": domain, "compiled": compiled}
    if detectors:
        policy["detectors"] = detectors
    return policy


def _mark_unclassified(entry: dict, reason: str) -> None:
    entry["source"] = "none"
    entry["classification"] = "unknown"
    entry["enforcement"] = "advisory"
    entry["reason"] = reason


def _apply_verdict(entry: dict, verdict: Optional[dict]) -> None:
    if not isinstance(verdict, dict):
        _mark_unclassified(entry, "LLM 응답 없음 — 기본 advisory")
        return
    entry["source"] = "llm"
    classification = verdict.get("classification")
    entry["classification"] = classification
    entry["reason"] = str(verdict.get("reason") or "")
    if classification == "deterministic" and verdict.get("regex"):
        ver = verify_regex(str(verdict["regex"]), verdict.get("examples") or {})
        entry["proposed"] = {
            "regex": str(verdict["regex"]),
            "examples": verdict.get("examples") or {},
            "verified": ver["verified"], "verify_detail": ver["detail"],
        }
        # Only a self-verified regex is safe to promote to a blocking detector; otherwise advisory.
        entry["enforcement"] = "detector" if ver["verified"] else "advisory"
    else:
        entry["enforcement"] = "advisory"


def _resolve_client() -> Optional[LLMClient]:
    """A ready LLM client (host CLI or API), or None when no backend can be constructed."""
    from .llm import default_client

    try:
        client = default_client()
        ensure = getattr(client, "ensure_ready", None)
        if ensure is not None:
            ensure()
        return client
    except LLMUnavailable:
        return None


def _classify_with_llm(client: LLMClient, texts: list) -> dict:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    prompt = f"Classify these {len(texts)} rule(s):\n{numbered}"
    raw = client.complete(_SYSTEM, prompt, max_tokens=1500)
    out: dict = {}
    for item in _parse_llm(raw):
        if isinstance(item, dict) and isinstance(item.get("index"), int):
            out[item["index"]] = item
    return out


def _parse_llm(raw: str) -> list:
    match = _JSON_ARRAY_RE.search(raw or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
