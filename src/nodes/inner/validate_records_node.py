"""AgentCore Platform v1.0"""

# /ingest step 3 (docs/02_design.md §/ingest, §(b) 最小データ要件):
# required-field validation. Incident rows need onset_date + species +
# affected_count(approx OK) + a narrative; env_observations rows need
# metric+value+observed_at; inputs rows need type+occurred_at.

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_REQUIRED_FIELDS = {
    "env_observations": ["metric", "value", "observed_at"],
    "inputs": ["type", "occurred_at"],
    "incidents": ["onset_date", "species", "course_narrative"],
}


class ValidateRecordsNode(FunctionNode):
    """Minimum-data-requirement validation for /ingest records (docs/02 §(b))."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        records: list[dict[str, Any]] = state.get("normalized_records", []) or []

        valid: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for idx, rec in enumerate(records):
            series = rec.get("series")
            required = _REQUIRED_FIELDS.get(series, [])
            missing = [f for f in required if not rec.get(f)]
            # incidents.affected_count is "approx OK" — treat 0/None distinctly from missing key
            if series == "incidents" and "affected_count" not in rec:
                rec = {**rec, "affected_count": None}
            if missing:
                errors.append({"row_index": idx, "series": series, "missing_fields": missing})
            else:
                valid.append(rec)

        emit_trace_event(
            "ingest_validate_records",
            {"valid_count": len(valid), "error_count": len(errors)},
            state,
        )

        return {
            "valid_records": valid,
            "validation_errors": errors,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
