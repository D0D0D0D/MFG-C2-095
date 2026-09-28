"""AgentCore Platform v1.0"""

# /feedback step 3 (docs/02_design.md §/feedback): deterministic hit/miss
# reconciliation against the most recent /invoke advisory_output for this
# farm. Feeds the "improves Stage 2 matching precision over time" moat
# narrative — this node is where the accumulation signal is recorded.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.incident_store import BaseIncidentStore


class ReconcilePredictionNode(FunctionNode):
    """Reconcile the most recent advisory against the confirmed outcome."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore) -> None:
        super().__init__()
        self._store = incident_store

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        updated_fields: dict[str, Any] = state.get("updated_incident_fields", {}) or {}
        incident_id = state.get("resolved_incident_id", "")

        recent_advisory = self._store.get_recent_advisory(farm_id)

        if recent_advisory is None:
            feedback_result = {
                "incident_id": incident_id,
                "reconciliation": "no_prior_advisory",
            }
        else:
            predicted_category = recent_advisory.get("predicted_category")
            actual_category = updated_fields.get("category") or updated_fields.get("agent")
            hit = bool(predicted_category) and predicted_category == actual_category
            feedback_result = {
                "incident_id": incident_id,
                "reconciliation": "hit" if hit else "miss",
                "predicted_category": predicted_category,
                "actual_category": actual_category,
                "cited_signal_ids": recent_advisory.get("cited_signal_ids", []),
                "cited_incident_ids": recent_advisory.get("cited_incident_ids", []),
            }

        emit_trace_event(
            "feedback_reconcile_prediction",
            {"farm_id": farm_id, "reconciliation": feedback_result["reconciliation"]},
            state,
        )

        return {"feedback_result": feedback_result, "status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
