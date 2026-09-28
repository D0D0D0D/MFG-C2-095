"""AgentCore Platform v1.0"""

# /feedback step 2 (docs/02_design.md §/feedback): confirm UPDATE/INSERT of
# course_narrative / outcome / actions_taken / status. Reuses the
# structure_with_llm-equivalent step only if a free-text note is supplied
# and llm is available (deterministic pass-through otherwise).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text

from src.services.incident_store import BaseIncidentStore

# docs/02_design.md §データモデル概要 incidents schema: category (病気/災害種別)
# and agent (病原体/災害種) are required for reconcile_prediction_node's
# hit/miss comparison against the prior advisory's predicted_category —
# omitting them here would silently make every /feedback outcome "miss".
_UPDATABLE_FIELDS = [
    "course_narrative",
    "outcome",
    "actions_taken",
    "status",
    "species",
    "onset_date",
    "category",
    "agent",
    "severity",
    "affected_count",
    "mortality",
]


class UpdateIncidentNode(FunctionNode):
    """Confirm UPDATE/INSERT of the resolved incident record."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore, llm: Any | None = None) -> None:
        super().__init__()
        self._store = incident_store
        self._llm = llm

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        incident_id = state.get("resolved_incident_id", "")
        payload = state.get("payload", {}) or {}

        fields = {k: v for k, v in payload.items() if k in _UPDATABLE_FIELDS}

        free_text_note = payload.get("note_free_text")
        degradation_reason = None
        if free_text_note and not fields.get("course_narrative"):
            if self._llm is not None:
                try:
                    fields["course_narrative"] = complete_text(
                        self._llm,
                        f"Structure this aquaculture incident update note:\n{free_text_note}",
                    )
                except Exception:  # noqa: BLE001
                    fields["course_narrative"] = free_text_note
                    degradation_reason = "llm_unavailable"
            else:
                fields["course_narrative"] = free_text_note
                degradation_reason = "llm_unavailable"

        self._store.update_incident(farm_id, incident_id, fields)

        emit_trace_event(
            "feedback_update_incident",
            {"farm_id": farm_id, "incident_id": incident_id, "fields_updated": sorted(fields.keys())},
            state,
        )

        result: dict[str, Any] = {
            "updated_incident_fields": fields,
            "status": AgentStatus.SUCCESS.value,
        }
        if degradation_reason:
            result["degradation_reason"] = degradation_reason
        return result

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
