"""AgentCore Platform v1.0"""

# main slot — AquacultureWorkflowGraphNode (docs/02_design.md §main（GraphNode）契約).
# Wraps the inner AquacultureWorkflowGraph (src/graph/domain_workflow_graph.py).
#
# get_subgraph()  — cached in __init__ (avoid rebuilding the inner StateGraph
#                    on every execute() — same rationale as the Cat2 HITL
#                    incident_store caching lesson).
# extract_input() — JSON-serialized envelope (default=str) of the fields the
#                    inner graph's DispatchNode needs.
# merge_output()  — mode-dependent: only returns the keys the mode actually
#                    produced (no None-padding — docs/02_design.md §main 契約).

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.services.degradation import combine_degradation_reasons
from framework.schemas.trust_level import TrustLevel


class AquacultureWorkflowGraphNode(GraphNode):
    """Outer `main` slot — delegates to the inner AquacultureWorkflowGraph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any] | None = None, incident_store=None) -> None:
        super().__init__()
        self._config = config or {}
        self._incident_store = incident_store
        self._subgraph = None  # built lazily on first get_subgraph() call

    def get_subgraph(self):
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import AquacultureWorkflowGraph

            subgraph = AquacultureWorkflowGraph(config=self._config, incident_store=self._incident_store)
            subgraph.compile()
            self._subgraph = subgraph
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        return state.get(
            "validated_input",
            json.dumps(
                {
                    "mode": state.get("mode"),
                    "farm_id": state.get("farm_id"),
                    "species_code": state.get("species_code"),
                    "payload": state.get("payload", {}),
                    "farm_context": state.get("farm_context", {}),
                    "species_profile": state.get("species_profile", {}),
                    "forecast_context": state.get("forecast_context", {}),
                    "forecast_raw": state.get("forecast_raw", []),
                },
                default=str,
            ),
        )

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Only return the fields the mode that actually ran produced
        (docs/02_design.md §main 契約 — no None-padding for absent modes).
        """
        delta: dict[str, Any] = {"status": sub_result.get("status", AgentStatus.SUCCESS.value)}
        # 🔴 Accumulate, do not assign. pre_process may already have recorded
        # `forecast_fetch_failed` in the OUTER state, and the inner graph never
        # receives that value — so assigning the inner reason here erased it and
        # the caller saw only `llm_unavailable` for a run that also had no
        # weather data. merge_output is the only place with both halves.
        combined = combine_degradation_reasons(
            state.get("degradation_reason"), sub_result.get("degradation_reason")
        )
        if combined:
            delta["degradation_reason"] = combined

        if "signal_list" in sub_result:
            delta["signal_list"] = sub_result["signal_list"]
            delta["signal_list_total_count"] = sub_result.get(
                "signal_list_total_count", len(sub_result["signal_list"])
            )
            if sub_result.get("signal_list_truncated"):
                delta["signal_list_truncated"] = True
        if sub_result.get("unparseable_observations"):
            delta["unparseable_observations"] = sub_result["unparseable_observations"]
            delta["unparseable_observation_count"] = sub_result.get(
                "unparseable_observation_count", len(sub_result["unparseable_observations"])
            )
        if "candidate_risks" in sub_result:
            delta["candidate_risks"] = sub_result["candidate_risks"]
            delta["candidate_risks_total_count"] = sub_result.get(
                "candidate_risks_total_count", len(sub_result["candidate_risks"])
            )
            if sub_result.get("candidate_risks_truncated"):
                delta["candidate_risks_truncated"] = True
        if "advisory_output" in sub_result:
            delta["advisory_output"] = sub_result["advisory_output"]
        if "ingest_summary" in sub_result:
            delta["ingest_summary"] = sub_result["ingest_summary"]
        if "feedback_result" in sub_result:
            delta["feedback_result"] = sub_result["feedback_result"]

        return delta

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        return result
