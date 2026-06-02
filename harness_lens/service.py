"""LensService — the orchestration facade.

Both the MCP server (:mod:`harness_lens.server`) and the CLI
(:mod:`harness_lens.cli`) drive harness-lens through this one object so the
behaviour of ``record_step``, diagnosis, evolution, verification, etc. is defined
in a single place.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import yaml

from . import home_dir
from .agents import DebuggerAgent, EvolveAgent, ProposalError
from .components import EDITABLE_COMPONENTS, AppliedEdit, ComponentError, ComponentManager
from .criteria import CriteriaEngine, DEFAULT_CRITERIA_YAML, QAConfig, ThreeLayerCriteria, layer3_in_range
from .criteria.layer import CriteriaViolation
from .decision import DecisionVerifier, VerifyResult
from .detector import detect
from .experience import ExperienceCorpus
from .hooks.install import loads_jsonc
from .judge import JudgeMonitor, JudgeStatus
from .llm import LLMClient, LLMUnavailable, default_client
from .reconstructor import Reconstructor
from .store import EvolutionCandidate, JudgeSample, SQLiteStore, Step


class LensService:
    def __init__(self, root: Optional[Path] = None, llm: Optional[LLMClient] = None):
        self.root = root or home_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.criteria_path = self.root / "criteria.yaml"
        self.store = SQLiteStore(self.root / "ledger.db")
        self.criteria = ThreeLayerCriteria.load(self.criteria_path)
        self._llm = llm
        self.experience = ExperienceCorpus(self.store, self.criteria.qa)
        self.judge_monitor = JudgeMonitor(self.store)
        self.verifier = DecisionVerifier(self.store)
        self.components = ComponentManager(self.root)

    def close(self) -> None:
        self.store.close()

    def _reload_criteria(self) -> None:
        """Reload criteria.yaml and refresh every collaborator that captured it.

        ``self.experience`` holds a reference to the QA criteria, so it must be
        rebuilt too; otherwise a long-lived server keeps using stale thresholds.
        """
        self.criteria = ThreeLayerCriteria.load(self.criteria_path)
        self.experience = ExperienceCorpus(self.store, self.criteria.qa)

    # -- llm wiring ------------------------------------------------------ #
    def _require_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = default_client()
        return self._llm

    def _reconstructor(self, with_judge: bool = False) -> Reconstructor:
        llm = self._llm if with_judge else None
        return Reconstructor(self.store, CriteriaEngine(self.criteria, self.store, llm=llm))

    # -- recording ------------------------------------------------------- #
    def record_step(
        self,
        session_id: str,
        tool_name: str,
        input_summary: str = "",
        output_summary: str = "",
        success: bool = True,
        latency_ms: Optional[int] = None,
    ) -> Step:
        recon = self._reconstructor(with_judge=self._llm is not None)
        recon.on_pre_tool(session_id, tool_name, input_summary)
        return recon.on_post_tool(session_id, tool_name, output_summary, success, latency_ms)

    # -- read models ----------------------------------------------------- #
    def get_flow_summary(self, session_id: Optional[str] = None, limit: int = 20, only_failed: bool = False) -> list[dict]:
        sessions = (
            [self.store.get_session(session_id)] if session_id
            else self.store.recent_sessions(limit=limit, only_failed=only_failed)
        )
        result = []
        for session in sessions:
            if session is None:
                continue
            result.append(self._flow_tree(session.session_id))
        return result

    def _flow_tree(self, session_id: str) -> dict:
        session = self.store.get_session(session_id)
        steps = self.store.steps_for_session(session_id)
        tasks: dict[str, dict] = {}
        for step in steps:
            task = tasks.setdefault(step.task_id, {
                "task_id": step.task_id, "category": step.task_category, "steps": [],
            })
            task["steps"].append(asdict(step))
        scored = [s.layer2_score for s in steps if s.layer2_score is not None]
        return {
            "session_id": session_id,
            "platform": session.platform if session else "",
            "status": session.status if session else "",
            "total_tokens": session.total_tokens if session else 0,
            "layer2_avg": (sum(scored) / len(scored)) if scored else None,
            "tasks": list(tasks.values()),
        }

    # -- Pillar 2 -------------------------------------------------------- #
    def run_diagnosis(self) -> list[dict]:
        agent = DebuggerAgent(self._require_llm())
        return [asdict(d) for d in agent.diagnose(self.experience)]

    # -- Pillar 3 -------------------------------------------------------- #
    def propose_evolution(self, gap_threshold: float = 0.5) -> list[dict]:
        agent = DebuggerAgent(self._require_llm())
        evolver = EvolveAgent(self._require_llm())
        proposals: list[dict] = []
        for diagnosis in agent.diagnose(self.experience):
            try:
                candidate = evolver.propose(
                    diagnosis, current_content=self._current_component_content(diagnosis.affected_component)
                )
            except ProposalError as exc:
                proposals.append({"failure_pattern": diagnosis.failure_pattern, "held": str(exc)})
                continue
            self.store.add_candidate(candidate)
            proposals.append(asdict(candidate))
        return proposals

    def apply_evolution(self, candidate_id: str, confirmed: bool = False) -> dict:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            raise KeyError(f"unknown candidate {candidate_id}")
        if candidate.applied_at is not None or candidate.status == "applied":
            # Re-applying would back up the already-modified target and overwrite the
            # original backup, making the change unrecoverable on rollback.
            raise CriteriaViolation(f"candidate {candidate_id} is already applied")
        guard = self.criteria.guard
        guard.assert_evolvable_layer(candidate.target_layer)
        guard.assert_external_component(candidate.target_component, set(EDITABLE_COMPONENTS))
        if not confirmed:
            raise CriteriaViolation("apply_evolution requires confirmed=True")

        edit = self._apply_change(candidate)
        candidate.applied_at = time.time()
        candidate.status = "applied"
        candidate.proposed_change = {
            **candidate.proposed_change,
            "__backup__": str(edit.backup_path) if edit.backup_path else "",
            "__existed__": edit.existed,
            "__target__": str(edit.target_path),
        }
        self.store.update_candidate(candidate)
        self.verifier.record_prediction(candidate)
        return asdict(candidate)

    def _apply_change(self, candidate: EvolutionCandidate) -> AppliedEdit:
        change = candidate.proposed_change or {}
        # Layer-3 parameter change → rewrite criteria.yaml's layer3 block. Only the QA
        # component may do this; a non-QA target carrying LLM-supplied `params` must not
        # be allowed to rewrite criteria.yaml after passing the guard for another file.
        params = change.get("params")
        if candidate.target_component == "qa.py":
            return self._apply_layer3_params(params or {})
        if params:
            raise ComponentError(
                f"proposal targets {candidate.target_component!r} but carries Layer-3 'params'; "
                "only the qa.py component may change Layer-3 parameters"
            )
        # Explicit file content for a component with a real live destination
        # (hooks → settings.json, CLAUDE.md → the platform instruction file). The target
        # is derived from the detected platform, never the LLM-proposed path, so a
        # proposal cannot write outside the harness-controlled area.
        if "path" in change and "content" in change:
            target = self._live_target(candidate.target_component)
            if target is None:
                # Defensive: editable components all have a live destination, but refuse
                # rather than record an inert applied prediction if one ever does not.
                raise ComponentError(
                    f"{candidate.target_component!r} has no live application path; "
                    "refusing to record an inert applied change"
                )
            content = str(change["content"])
            if candidate.target_component == "hooks":
                # settings.json is structured and shared; merge the proposal into the
                # existing config instead of overwriting the whole file with a snippet.
                return self._apply_hooks_change(target, content)
            return self.components.apply(candidate.target_component, target, content)
        raise ComponentError(
            "proposed_change has no applicable payload (expected 'params' or 'path'+'content')"
        )

    def _apply_hooks_change(self, target: Path, content: str) -> AppliedEdit:
        try:
            proposed = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ComponentError(f"hooks proposal is not valid JSON: {exc}") from exc
        if not isinstance(proposed, dict):
            raise ComponentError("hooks proposal must be a JSON object")
        existing: dict = {}
        if target.exists() and target.read_text(encoding="utf-8").strip():
            try:
                # Tolerate JSONC and, on a genuinely unparseable file, refuse rather than
                # treat it as empty — which would drop the user's existing settings on merge.
                loaded = loads_jsonc(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ComponentError(f"existing settings at {target} are not valid JSON: {exc}") from exc
            existing = loaded if isinstance(loaded, dict) else {}
        merged = self._deep_merge(existing, proposed)
        return self.components.apply(
            "hooks", target, json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        )

    @staticmethod
    def _deep_merge(base: dict, overlay: dict, identity_lists: bool = False) -> dict:
        """Recursively merge ``overlay`` into ``base`` without dropping sibling keys.

        Nested dicts (e.g. ``hooks``/``mcpServers``) are merged key-by-key. Only lists
        *under the ``hooks`` subtree* (a hook event's entries) are merged by *identity* so a
        partial hooks proposal cannot wipe unrelated existing config: an overlay entry that
        matches an existing one by command/matcher replaces it in place — re-proposing the
        same hook with a new ``timeout`` updates the entry rather than appending a duplicate,
        which would make Claude Code fire the hook twice. Positional lists elsewhere (e.g.
        ``mcpServers.<name>.args``) are *replaced* wholesale: identity-merging them would
        concatenate old and new argv into an invalid command line.

        A proposal built from redacted settings may echo ``***REDACTED***`` placeholders for
        existing secrets; those overlay scalars are skipped so a hooks evolution never
        overwrites a live credential with the mask.
        """
        result = dict(base)
        for key, value in overlay.items():
            current = result.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                # Recurse first so the per-key mask check below still applies granularly
                # (a masked `env.KEY` is skipped while a sibling new key is merged).
                result[key] = LensService._deep_merge(
                    current, value, identity_lists or key == "hooks"
                )
            elif LensService._contains_redaction(value):
                # Overlay scalar/list carries a redaction mask (the proposal was built from
                # redacted settings) — keep the live base value rather than writing the mask
                # back over a secret or appending a redacted hook/arg.
                continue
            elif isinstance(current, list) and isinstance(value, list) and identity_lists:
                result[key] = LensService._merge_lists(current, value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _contains_redaction(value) -> bool:
        """True if the redaction mask appears anywhere in ``value`` (string, list, dict)."""
        if isinstance(value, str):
            return LensService._REDACTION_PLACEHOLDER in value
        if isinstance(value, dict):
            return any(LensService._contains_redaction(v) for v in value.values())
        if isinstance(value, list):
            return any(LensService._contains_redaction(v) for v in value)
        return False

    @staticmethod
    def _merge_lists(base: list, overlay: list) -> list:
        """Merge ``overlay`` into ``base`` by item identity, overlay winning on a match."""
        merged = list(base)
        index = {LensService._item_key(item): i for i, item in enumerate(merged)}
        for item in overlay:
            ident = LensService._item_key(item)
            if ident in index:
                merged[index[ident]] = item  # same hook/matcher → update (e.g. new timeout)
            else:
                index[ident] = len(merged)
                merged.append(item)
        return merged

    # Fields that identify the same hook entry differently per proposal but must not make
    # it count as a *new* entry (else a timeout tweak appends a duplicate).
    _VOLATILE_HOOK_KEYS = frozenset({"timeout"})

    @staticmethod
    def _item_key(item) -> str:
        """Identity of a list item for merge, ignoring volatile fields like ``timeout``."""
        try:
            return json.dumps(LensService._strip_volatile(item), sort_keys=True, ensure_ascii=False)
        except TypeError:
            return repr(item)

    @staticmethod
    def _strip_volatile(item):
        if isinstance(item, dict):
            return {
                k: LensService._strip_volatile(v)
                for k, v in item.items()
                if k not in LensService._VOLATILE_HOOK_KEYS
            }
        if isinstance(item, list):
            return [LensService._strip_volatile(v) for v in item]
        return item

    def _current_component_content(self, component: Optional[str]) -> Optional[str]:
        """Existing text of a file component, so the evolver edits rather than rewrites blind.

        Returns None for qa.py (params-based, no full-file overwrite) and when the live
        target does not exist or cannot be read.
        """
        if not component or component == "qa.py":
            return None
        target = self._live_target(component)
        if target is None or not target.exists():
            return None
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return None
        # The hooks component is the live settings file, which commonly holds mcpServers
        # `env` tokens and other secrets. Redact before it goes into the LLM prompt so
        # `evolve` never leaks local credentials for a hook-related diagnosis.
        if component == "hooks":
            return self._redact_settings(text)
        return text

    _SENSITIVE_KEY_HINTS = ("token", "secret", "password", "passwd", "apikey", "api_key",
                            "api-key", "auth", "credential", "key")
    _REDACTION_PLACEHOLDER = "***REDACTED***"

    # A CLI flag whose *value* is a credential (the next arg, or the inline `=value`).
    _SENSITIVE_FLAG_RE = re.compile(r"--?(?:[\w-]*(?:token|secret|password|apikey|api[-_]?key|auth|credential|key)[\w-]*)$", re.IGNORECASE)
    # Tokens recognizable by shape, redacted wherever they appear in a string leaf.
    _SECRET_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)")

    def _redact_settings(self, text: str) -> str:
        try:
            data = loads_jsonc(text)
        except (json.JSONDecodeError, ValueError):
            # Unparseable settings: don't risk sending raw secrets; the evolver can still
            # propose against an empty/placeholder file.
            return "<existing settings omitted: unparseable, redacted for privacy>"
        self._redact_in_place(data)
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def _redact_in_place(cls, node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if cls._is_sensitive_key(key):
                    # Mask the whole subtree: a sensitive key may hold a nested object/list
                    # (e.g. {"password": {"current": "hunter2"}}) whose leaves would
                    # otherwise leak through the recursive branch below.
                    node[key] = cls._REDACTION_PLACEHOLDER
                elif key == "env" and isinstance(value, dict):
                    for env_key in value:
                        value[env_key] = cls._REDACTION_PLACEHOLDER
                elif isinstance(value, str):
                    node[key] = cls._redact_token_string(value)
                else:
                    cls._redact_in_place(value)
        elif isinstance(node, list):
            # Argument vectors (e.g. mcpServers.*.args) carry secrets as positional items:
            # `["--api-key", "sk-..."]` or `["--api-key=sk-..."]`. Redact the value that
            # follows a credential-bearing flag, plus any token recognizable by shape.
            redact_next = False
            for i, item in enumerate(node):
                if isinstance(item, str):
                    if redact_next:
                        node[i] = cls._REDACTION_PLACEHOLDER
                        redact_next = False
                        continue
                    flag, sep, _ = item.partition("=")
                    if sep and cls._SENSITIVE_FLAG_RE.match(flag):
                        node[i] = f"{flag}={cls._REDACTION_PLACEHOLDER}"
                    elif cls._SENSITIVE_FLAG_RE.match(item):
                        redact_next = True
                    else:
                        node[i] = cls._redact_token_string(item)
                else:
                    redact_next = False
                    cls._redact_in_place(item)

    @classmethod
    def _redact_token_string(cls, value: str) -> str:
        return cls._SECRET_TOKEN_RE.sub(cls._REDACTION_PLACEHOLDER, value)

    @classmethod
    def _is_sensitive_key(cls, key: str) -> bool:
        low = key.lower()
        return any(hint in low for hint in cls._SENSITIVE_KEY_HINTS)

    def _live_target(self, component: str) -> Optional[Path]:
        """Canonical live file for a component on the detected platform.

        Returns ``None`` when no platform is detected or the component has no live
        destination, in which case the change cannot be applied.
        """
        platform = detect()
        if platform is None:
            return None
        if component == "hooks":
            return platform.settings_path
        if component == "CLAUDE.md":
            return platform.settings_path.parent / platform.instruction_file
        return None

    def _apply_layer3_params(self, params: dict) -> AppliedEdit:
        # Coerce to each field's numeric type and drop uncoercible values, so a value
        # like "two" cannot be written and then silently dropped on reload — which would
        # record an applied prediction for a no-op config change.
        defaults = QAConfig()
        valid: dict = {}
        for key, value in params.items():
            if key not in QAConfig.__dataclass_fields__:
                continue
            caster = type(getattr(defaults, key))
            try:
                coerced = caster(value)
            except (TypeError, ValueError):
                continue
            if not layer3_in_range(key, coerced):
                # Out-of-range thresholds (e.g. retry_threshold 0, quality_threshold 2)
                # would make find_failure_patterns flag every group, poisoning diagnosis.
                raise ComponentError(f"Layer-3 parameter {key}={coerced} is out of range")
            valid[key] = coerced
        if not valid:
            raise ComponentError("no recognised, coercible Layer-3 parameters in proposed change")
        # Seed from the same default the service loads when no file exists, otherwise the
        # first qa.py apply on a fresh install writes a layer3-only file and the next reload
        # silently drops the default invariants and domain criteria.
        source = self.criteria_path.read_text(encoding="utf-8") if self.criteria_path.exists() else DEFAULT_CRITERIA_YAML
        data = yaml.safe_load(source) or {}
        layer3 = data.setdefault("layer3", {})
        layer3.update(valid)
        new_yaml = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        edit = self.components.apply("qa.py", self.criteria_path, new_yaml)
        self._reload_criteria()  # reload with new params
        return edit

    def verify_predictions(self) -> list[VerifyResult]:
        results = []
        # Oldest-first: an older miss cascade-rolls-back newer same-target edits, so a newer
        # candidate must not be verified (and possibly confirmed) before the older one runs —
        # otherwise the decision log claims a hit for a change that is about to be undone.
        applied = sorted(self.store.list_candidates(status="applied"), key=lambda c: c.applied_at or 0)
        for candidate in applied:
            # An earlier miss can cascade-rollback a newer candidate on the same file, so
            # re-read status: a candidate already rolled back this round must not be
            # verified (and possibly confirmed) when its change is no longer on disk.
            fresh = self.store.get_candidate(candidate.candidate_id)
            if fresh is None or fresh.status != "applied":
                continue
            result = self.verifier.verify_after_round(candidate.candidate_id)
            results.append(result)
            if result.was_correct is False:
                # A missed prediction must actually undo the change, not just relabel it.
                self._restore_candidate(self.store.get_candidate(candidate.candidate_id))
        return results

    def rollback_last(self) -> Optional[dict]:
        # Candidates whose change is still live on disk — both "applied" and (after a
        # prediction hit) "confirmed". Ones already rolled back must not be re-selected.
        live = [
            c for c in self.store.list_candidates()
            if c.applied_at is not None and c.status in ("applied", "confirmed")
        ]
        if not live:
            return None
        candidate = max(live, key=lambda c: c.applied_at or 0)
        self._restore_candidate(candidate)
        return asdict(self.store.get_candidate(candidate.candidate_id))

    def _target_of(self, candidate: EvolutionCandidate) -> Optional[Path]:
        change = candidate.proposed_change or {}
        target_str = change.get("__target__")
        if target_str:
            return Path(target_str)
        return self.criteria_path if candidate.target_component == "qa.py" else None

    def _restore_file(self, candidate: EvolutionCandidate, target: Path) -> None:
        change = candidate.proposed_change or {}
        existed = change.get("__existed__", True)
        backup = change.get("__backup__")
        if not existed:
            if target.exists():
                target.unlink()
            return
        if backup and Path(backup).exists():
            shutil.copy2(backup, target)
            return
        # The target existed before this edit but its backup is gone (deleted/corrupt) or
        # was never recorded. We cannot restore the original, so refuse rather than letting
        # the caller mark the candidate `rolled_back` while the modified file stays live —
        # that would report a rollback that never happened.
        raise ComponentError(
            f"cannot roll back {candidate.candidate_id}: backup {backup!r} for {target} is missing"
        )

    def _restore_candidate(self, candidate: Optional[EvolutionCandidate]) -> None:
        if candidate is None:
            return
        target = self._target_of(candidate)
        if target is not None:
            # Edits to one file form a LIFO stack. This candidate's backup holds the file
            # state *before* its change — i.e. everything up to but not including it — so
            # restoring it also discards any newer live edits to the same target. Cascade:
            # mark those newer edits rolled_back too, keeping the ledger consistent with
            # the file (otherwise a later rollback of a newer edit would resurrect content).
            newer = [
                c for c in self.store.list_candidates()
                if c.candidate_id != candidate.candidate_id
                and c.applied_at is not None
                and c.status in ("applied", "confirmed")
                and (c.applied_at or 0) > (candidate.applied_at or 0)
                and self._target_of(c) == target
            ]
            self._restore_file(candidate, target)
            for other in newer:
                other.status = "rolled_back"
                self.store.update_candidate(other)
            if target == self.criteria_path:
                self._reload_criteria()
        candidate.status = "rolled_back"
        self.store.update_candidate(candidate)

    # -- guarded user override ------------------------------------------ #
    def set_layer3_override(self, key: str, value: float, reason: str) -> dict:
        self.criteria.guard.allow_user_override(key, value)
        return {"key": key, "value": value, "reason": reason, "effective": self.criteria.qa.effective(key)}

    # -- Judge ----------------------------------------------------------- #
    def get_judge_status(self) -> JudgeStatus:
        return self.judge_monitor.status()

    def pending_reviews(self) -> list[JudgeSample]:
        return self.judge_monitor.pending_samples()

    def label_sample(self, sample: JudgeSample, human_label: float) -> JudgeSample:
        return self.judge_monitor.record_label(sample, human_label)

    # -- aggregate status ------------------------------------------------ #
    def status(self) -> dict:
        return {
            "judge": self.get_judge_status(),
            "prediction_hit_rate": self.verifier.hit_rate(),
            "layer3": self.criteria.qa.config.as_dict(),
            "candidates": {
                "proposed": len(self.store.list_candidates(status="proposed")),
                "applied": len(self.store.list_candidates(status="applied")),
                "confirmed": len(self.store.list_candidates(status="confirmed")),
                "rolled_back": len(self.store.list_candidates(status="rolled_back")),
            },
        }

    def llm_available(self) -> bool:
        try:
            client = self._require_llm()
        except LLMUnavailable:
            return False
        probe = getattr(client, "ensure_ready", None)
        if probe is None:  # custom client injected for tests
            return True
        try:
            probe()
            return True
        except LLMUnavailable:
            return False
