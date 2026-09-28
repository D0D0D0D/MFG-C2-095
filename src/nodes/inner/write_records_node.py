"""AgentCore Platform v1.0"""

# /ingest step 4 (docs/02_design.md §/ingest): INSERT into env_observations /
# inputs / incidents via the injected BaseIncidentStore, and summarize the
# write outcome for the caller.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.incident_store import BaseIncidentStore


class WriteRecordsNode(FunctionNode):
    """Bulk-write validated /ingest records into the 3-series schema."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore) -> None:
        super().__init__()
        self._store = incident_store

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        valid_records: list[dict[str, Any]] = state.get("valid_records", []) or []
        validation_errors: list[dict[str, Any]] = state.get("validation_errors", []) or []

        by_series: dict[str, list[dict[str, Any]]] = {"env_observations": [], "inputs": [], "incidents": []}
        for rec in valid_records:
            series = rec.get("series")
            if series in by_series:
                by_series[series].append(rec)

        env_count = self._store.write_env_observations(farm_id, by_series["env_observations"])
        input_count = self._store.write_inputs(farm_id, by_series["inputs"])
        incident_ids = self._store.write_incidents(farm_id, by_series["incidents"])

        ingest_summary = {
            "farm_id": farm_id,
            "env_observations_written": env_count,
            "inputs_written": input_count,
            "incidents_written": len(incident_ids),
            "incident_ids": incident_ids,
            "error_rows": validation_errors,
        }

        emit_trace_event("ingest_write_records", {"farm_id": farm_id, **{
            "env_observations_written": env_count,
            "inputs_written": input_count,
            "incidents_written": len(incident_ids),
        }}, state)

        return {"ingest_summary": ingest_summary, "status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
