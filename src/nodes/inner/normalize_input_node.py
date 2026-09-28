"""AgentCore Platform v1.0"""

# /ingest step 1 (docs/02_design.md §/ingest): deterministic column mapping
# for heterogeneous input formats (CSV-derived JSON, free-text). Marks rows
# whose narrative needs LLM structuring downstream (structure_with_llm).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_KNOWN_SERIES = {"env_observations", "inputs", "incidents"}


class NormalizeInputNode(FunctionNode):
    """Deterministic column-mapping normalization for /ingest records."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        payload: dict[str, Any] = state.get("payload", {}) or {}
        records: list[dict[str, Any]] = payload.get("records", [])

        normalized: list[dict[str, Any]] = []
        needs_llm_structuring: list[dict[str, Any]] = []

        for rec in records:
            series = rec.get("series")
            if series not in _KNOWN_SERIES:
                continue
            row = dict(rec)
            free_text = row.get("course_narrative") or row.get("note_free_text")
            if series == "incidents" and free_text and not row.get("course_narrative_structured"):
                needs_llm_structuring.append(row)
            else:
                normalized.append(row)

        emit_trace_event(
            "ingest_normalize",
            {"normalized_count": len(normalized), "needs_llm_count": len(needs_llm_structuring)},
            state,
        )

        return {
            "normalized_records": normalized,
            "records_needing_llm_structuring": needs_llm_structuring,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
