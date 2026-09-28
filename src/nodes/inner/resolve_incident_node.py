"""AgentCore Platform v1.0"""

# /feedback step 1 (docs/02_design.md §/feedback): resolve target incident_id
# (mint new if none supplied).

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.incident_store import BaseIncidentStore


class ResolveIncidentNode(FunctionNode):
    """Resolve or mint the target incident_id for /feedback."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore) -> None:
        super().__init__()
        self._store = incident_store

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        payload = state.get("payload", {}) or {}
        requested_id = payload.get("incident_id")

        incident_id = self._store.resolve_incident_id(farm_id, requested_id)

        emit_trace_event(
            "feedback_resolve_incident",
            {"farm_id": farm_id, "incident_id": incident_id, "was_new": requested_id != incident_id},
            state,
        )

        return {"resolved_incident_id": incident_id, "status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
