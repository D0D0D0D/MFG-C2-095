"""AgentCore Platform v1.0"""

# /ingest step 2 (docs/02_design.md §/ingest): LLM-based structuring of
# free-text incident narratives ONLY — extraction, not advisory interpretation.
# Also reused by /feedback update_incident when a free-text note is supplied
# (docs/02_design.md §/feedback update_incident).
#
# Constructor-injected llm (project standard — no hardcoded model ID). When
# llm is None, records pass through unstructured with a degradation marker
# rather than being silently dropped.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text

_STRUCTURE_FIELDS = ["onset_date", "species", "affected_count", "severity_hint", "actions_taken"]


class StructureWithLlmNode(FunctionNode):
    """Extracts structured fields from free-text incident narratives."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        pending: list[dict[str, Any]] = state.get("records_needing_llm_structuring", []) or []
        normalized: list[dict[str, Any]] = list(state.get("normalized_records", []) or [])

        if not pending:
            return {"normalized_records": normalized, "status": AgentStatus.SUCCESS.value}

        if self._llm is None:
            for rec in pending:
                rec = dict(rec)
                rec["course_narrative_structured"] = False
                normalized.append(rec)
            emit_trace_event("ingest_structure_llm_unavailable", {"pending_count": len(pending)}, state)
            return {
                "normalized_records": normalized,
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": "llm_unavailable",
            }

        structured_count = 0
        for rec in pending:
            narrative = rec.get("course_narrative") or rec.get("note_free_text") or ""
            prompt = self._build_prompt(narrative)
            try:
                raw = complete_text(self._llm, prompt)
                extracted = self._parse_structured_fields(raw)
            except Exception:  # noqa: BLE001 — keep the raw narrative on any extraction failure
                extracted = {}
            rec = dict(rec)
            rec.update(extracted)
            rec["course_narrative"] = narrative
            rec["course_narrative_structured"] = bool(extracted)
            if extracted:
                structured_count += 1
            normalized.append(rec)

        # 🔴 Count records actually structured, not records attempted. Reporting
        # len(pending) made an ingest where EVERY extraction failed look
        # identical in the audit trail to one where every extraction succeeded.
        emit_trace_event(
            "ingest_structure_with_llm",
            {"structured_count": structured_count, "pending_count": len(pending)},
            state,
        )

        result = {"normalized_records": normalized, "status": AgentStatus.SUCCESS.value}
        if structured_count < len(pending):
            # Surface partial/total extraction failure instead of reporting a
            # clean success (post_process folds this into formatted_output).
            result["degradation_reason"] = "llm_output_invalid"
        return result

    @staticmethod
    def _build_prompt(narrative: str) -> str:
        # 🔴 _parse_structured_fields() accepts ONLY a strict JSON object, but
        # this prompt used to ask merely to "extract structured fields". A real
        # model answers that in prose ("onset_date: 2026-05-01, species: madai"),
        # json.loads() raises, extraction silently yields {} and the record is
        # marked course_narrative_structured=False with no degradation reason —
        # an ingest that looks successful but structured nothing. State the
        # output contract the parser actually enforces.
        return (
            "Extract structured fields from this aquaculture incident narrative. "
            f"Fields to extract: {_STRUCTURE_FIELDS}. Return only fields you can "
            "confidently identify; do not infer a diagnosis.\n"
            "Respond with a single JSON object and nothing else — no prose, no "
            "explanation. Use exactly these keys, omitting any key you cannot "
            f"identify: {sorted(_STRUCTURE_FIELDS)}. All values must be strings.\n"
            'Example: {"onset_date": "2026-05-01", "species": "madai"}\n'
            f"\nNarrative:\n{narrative}"
        )

    @staticmethod
    def _parse_structured_fields(raw: str) -> dict[str, Any]:
        """Best-effort structured-field parse. Real implementation would use a
        constrained JSON-mode LLM call; this keeps the node's I/O contract
        stable regardless of LLM client capability.
        """
        import json

        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k in _STRUCTURE_FIELDS}
        except (json.JSONDecodeError, TypeError):
            pass
        return {}

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
