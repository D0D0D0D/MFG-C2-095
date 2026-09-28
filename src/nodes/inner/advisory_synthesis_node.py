"""AgentCore Platform v1.0"""

# Stage 3 — synthesis (docs/02_design.md §予兆判定の3段パイプライン).
# THE ONLY LLM STEP in the /invoke pipeline. Synthesizes Stage 1 signals +
# Stage 2 candidate risks + forecast_raw into a non-assertive advisory.
#
# The LLM is injected via the constructor (config["llm"]); no model ID is
# hardcoded here. When llm is
# None, falls back to a deterministic advisory built from Stage1/2 data only
# — status stays "success", degradation_reason="llm_unavailable"
# (docs/02_design.md §🔴 status の有効値 — "degraded" must never be assigned
# to status).
#
# severity is derived deterministically from signal_list/candidate_risks —
# never left to LLM judgment (docs/02_design.md §ユーザビリティ設計レビュー).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text
from src.services.degradation import combine_degradation_reasons

_SEVERITY_ORDER = ["low", "medium", "high", "critical"]


class AdvisorySynthesisNode(FunctionNode):
    """Stage 3: LLM-based synthesis of the non-assertive advisory.

    `llm` follows the BaseLLM interface (shared/services/llm/) — duck-typed
    here (constructor injection only, no import) so this node stays
    decoupled from the concrete LLM client wiring. See src/api/server.py
    wire_services() for how the real client is constructed and injected;
    `llm` MUST honour the BaseLLM wire contract
    (`complete(messages: list) -> dict`); all calls go through
    `src/services/llm_text.complete_text()`, which enforces it.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        signal_list: list[dict[str, Any]] = state.get("signal_list", []) or []
        candidate_risks: list[dict[str, Any]] = state.get("candidate_risks", []) or []
        forecast_raw: list[str] = state.get("forecast_raw", []) or []

        severity = self._derive_severity(signal_list, candidate_risks)
        valid_ids = self._collect_valid_ids(signal_list, candidate_risks)
        existing_degradation = state.get("degradation_reason")

        if self._llm is None:
            advisory = self._deterministic_fallback(signal_list, candidate_risks, severity)
            emit_trace_event(
                "stage3_llm_unavailable_fallback",
                {"signal_count": len(signal_list), "candidate_count": len(candidate_risks)},
                state,
            )
            return {
                "advisory_output": advisory,
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": combine_degradation_reasons(existing_degradation, "llm_unavailable"),
            }

        if not valid_ids:
            # 🔴 An empty evidence set makes the Stage-3 prompt unsatisfiable:
            # it would order the model to cite every sentence from `[]`. The
            # model then either fabricates an ID or writes uncitable prose, and
            # the S-3 provenance gate redacts the whole narrative either way.
            # Take the deterministic branch instead.
            #
            # This is NOT an llm_unavailable degradation: the LLM is healthy,
            # there is simply nothing for it to ground a claim on. Reporting it
            # as llm_unavailable would misattribute the cause to the provider
            # and mask a genuine provider outage on the same farm.
            advisory = self._deterministic_fallback(signal_list, candidate_risks, severity)
            emit_trace_event(
                "stage3_no_valid_evidence_deterministic",
                {"signal_count": len(signal_list), "candidate_count": len(candidate_risks)},
                state,
            )
            return {"advisory_output": advisory, "status": AgentStatus.SUCCESS.value}

        prompt = self._build_prompt(signal_list, candidate_risks, forecast_raw, severity, valid_ids)
        try:
            raw_text = complete_text(self._llm, prompt)
        except Exception as exc:  # noqa: BLE001 — LLM failure degrades, does not crash the pipeline
            emit_trace_event("stage3_llm_call_failed", {"error": str(exc)}, state)
            advisory = self._deterministic_fallback(signal_list, candidate_risks, severity)
            return {
                "advisory_output": advisory,
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": combine_degradation_reasons(existing_degradation, "llm_unavailable"),
            }

        advisory = {
            "severity": severity,
            "narrative": raw_text,
            "cited_signal_ids": [s.get("signal_id") for s in signal_list],
            "cited_incident_ids": [
                r.get("matched_incident_id") for r in candidate_risks if r.get("matched_incident_id")
            ],
            "valid_evidence_ids": sorted(valid_ids),
        }

        emit_trace_event(
            "stage3_advisory_synthesized",
            {"severity": severity, "signal_count": len(signal_list)},
            state,
        )

        return {"advisory_output": advisory, "status": AgentStatus.SUCCESS.value}

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _derive_severity(signal_list: list[dict[str, Any]], candidate_risks: list[dict[str, Any]]) -> str:
        """Deterministic severity derivation — never delegated to LLM judgment."""
        levels = []
        for s in signal_list:
            sev = s.get("severity")
            if sev in _SEVERITY_ORDER:
                levels.append(sev)
        for r in candidate_risks:
            sev = r.get("severity_baseline")
            if sev in _SEVERITY_ORDER:
                levels.append(sev)
        if not levels:
            return "low"
        return max(levels, key=lambda s: _SEVERITY_ORDER.index(s))

    @staticmethod
    def _collect_valid_ids(
        signal_list: list[dict[str, Any]], candidate_risks: list[dict[str, Any]]
    ) -> set[str]:
        ids = {s.get("signal_id") for s in signal_list if s.get("signal_id")}
        ids |= {r.get("matched_incident_id") for r in candidate_risks if r.get("matched_incident_id")}
        ids |= {r.get("matched_signal_id") for r in candidate_risks if r.get("matched_signal_id")}
        return ids

    @staticmethod
    def _build_prompt(
        signal_list: list[dict[str, Any]],
        candidate_risks: list[dict[str, Any]],
        forecast_raw: list[str],
        severity: str,
        valid_ids: set[str],
    ) -> str:
        return (
            "You are an aquaculture environmental early-warning advisory writer.\n"
            "Rules:\n"
            "1. You may ONLY reference risks present in the provided signal/candidate data below.\n"
            # 🔴 The S-3 provenance gate in post_process_node.py enforces this
            # PER SENTENCE (_strip_unsupported_claims splits on 。！？\n and
            # redacts any sentence carrying no valid evidence ID), and an ID
            # written AFTER the closing "。" belongs to the next fragment. Saying
            # only "every claim" made a real model cite once per paragraph, after
            # the "。" — every one of the 4 sentences it produced was redacted to
            # "[要検証：...]" while the envelope still looked populated
            # (live LLM check, 2026-08-20). The prompt must state the exact
            # granularity and placement the gate applies.
            "2. Every SENTENCE MUST carry a cited evidence ID in square brackets, "
            "taken verbatim from this exact set: "
            f"{sorted(valid_ids)}\n"
            "   Place the bracket INSIDE the sentence, immediately BEFORE the closing "
            "'。' — write '～の可能性があります[ID]。', NOT '～の可能性があります。[ID]'. "
            "An ID placed after the '。' belongs to the next sentence, leaving the "
            "sentence it was meant to support uncited.\n"
            "   A sentence with no ID inside it is removed before the reader sees it, "
            "so do not write headings, lead-ins or summarising sentences without one. "
            "Repeat the relevant ID in each sentence rather than citing once per "
            "paragraph. Never invent an ID outside the set above.\n"
            "3. Use non-assertive language (e.g. '~の可能性があります'), never assert an outcome as certain.\n"
            "4. Do not propose automated control actions or a confirmed diagnosis.\n\n"
            f"Derived severity (do not override): {severity}\n"
            f"Stage 1 signals: {signal_list}\n"
            f"Stage 2 candidate risks: {candidate_risks}\n"
            f"Raw forecast payloads (for citation only): {forecast_raw}\n"
        )

    @staticmethod
    def _deterministic_fallback(
        signal_list: list[dict[str, Any]], candidate_risks: list[dict[str, Any]], severity: str
    ) -> dict[str, Any]:
        lines = ["一般的な養殖知識ベースに基づく注意喚起（詳細解釈は現在利用できません）:"]
        for s in signal_list:
            lines.append(f"- {s.get('metric')} が {s.get('severity')} 水準の逸脱を示しています（根拠: {s.get('signal_id')}）")
        for r in candidate_risks:
            evidence = r.get("matched_incident_id") or r.get("matched_signal_id")
            lines.append(f"- {r.get('category')}/{r.get('agent')} の可能性があります（根拠: {evidence}）")
        if len(lines) == 1:
            lines.append("現時点で顕著な逸脱シグナルは検出されていません。")
        return {
            "severity": severity,
            "narrative": "\n".join(lines),
            "cited_signal_ids": [s.get("signal_id") for s in signal_list],
            "cited_incident_ids": [
                r.get("matched_incident_id") for r in candidate_risks if r.get("matched_incident_id")
            ],
            "valid_evidence_ids": sorted(AdvisorySynthesisNode._collect_valid_ids(signal_list, candidate_risks)),
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
