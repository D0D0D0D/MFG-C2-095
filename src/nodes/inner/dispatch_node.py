"""AgentCore Platform v1.0"""

# Inner graph entry node — BaseGraph.invoke(user_input, ...) only seeds
# state["user_input"] (a JSON envelope string built by the outer
# AquacultureWorkflowGraphNode.extract_input()); it does NOT let the caller
# inject arbitrary custom state fields. This node parses that envelope and
# populates state["mode"] / farm_id / species_code / farm_context /
# species_profile / forecast_context / forecast_raw / payload so that the
# downstream mode-dispatch conditional edge (which reads state["mode"] per
# docs/02_design.md §🔴 inner graph の state 型注釈方針) has something to
# route on.

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class DispatchNode(FunctionNode):
    """Parse the inner graph's user_input envelope into typed state fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        try:
            envelope = json.loads(user_input) if user_input else {}
        except (json.JSONDecodeError, TypeError):
            emit_trace_event("dispatch_invalid_envelope", {}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DispatchNode: inner user_input is not valid JSON envelope"],
            }
        except RecursionError:
            # `json.loads` recurses one Python stack frame per nesting level.
            # A client-controlled request body containing a deeply nested
            # array/object (e.g. observed_at=[[[...]]] ~1000+ levels deep)
            # exhausts the interpreter's recursion limit INSIDE the stdlib
            # decoder itself — this is NOT a json.JSONDecodeError (a
            # RecursionError is a RuntimeError subclass), so it is not caught
            # by the branch above. Left uncaught, it propagates through
            # execute() -> BaseNode.__call__() and crashes the entire
            # invocation (a remote DoS: the request never reaches any
            # per-field validation deeper in the pipeline). Treat it exactly
            # like a malformed envelope — reject with status=error rather than
            # let the process crash.
            emit_trace_event("dispatch_invalid_envelope", {"reason": "excessive_nesting_depth"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DispatchNode: inner user_input is nested too deeply to parse"],
            }

        mode = envelope.get("mode")
        if mode not in ("invoke", "ingest", "feedback"):
            emit_trace_event("dispatch_unknown_mode", {"mode": mode}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"DispatchNode: unknown or missing mode {mode!r}"],
            }

        emit_trace_event("dispatch_resolved", {"mode": mode}, state)
        return {
            "mode": mode,
            "farm_id": envelope.get("farm_id", ""),
            "species_code": envelope.get("species_code", ""),
            "payload": envelope.get("payload", {}),
            "farm_context": envelope.get("farm_context", {}),
            "species_profile": envelope.get("species_profile", {}),
            "forecast_context": envelope.get("forecast_context", {}),
            "forecast_raw": envelope.get("forecast_raw", []),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
