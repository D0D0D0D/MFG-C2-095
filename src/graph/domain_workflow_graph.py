"""AgentCore Platform v1.0"""

# Inner graph for MFG-C2-095 (docs/02_design.md §/invoke §/ingest §/feedback).
# Fully custom topology (BaseGraph, not AgentBaseGraph) — 3 modes share this
# graph but execute disjoint node sets selected by state["mode"].
#
# 🔴 docs/02_design.md §inner graph の state 型注釈方針 (binding contract):
# the mode-routing function and get_output() are annotated with
# AquacultureWorkflowState (this module's own schema), NOT AgentState.
# Annotating with the base AgentState causes state projection to drop the
# custom `mode` field and can therefore route to the wrong branch.
#
# Directory rule: src/graph/domain_workflow_graph.py is the only accepted path.

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.advisory_synthesis_node import AdvisorySynthesisNode
from src.nodes.inner.dispatch_node import DispatchNode
from src.nodes.inner.normalize_input_node import NormalizeInputNode
from src.nodes.inner.reconcile_prediction_node import ReconcilePredictionNode
from src.nodes.inner.resolve_incident_node import ResolveIncidentNode
from src.nodes.inner.signal_detection_node import SignalDetectionNode
from src.nodes.inner.signal_matching_node import SignalMatchingNode
from src.nodes.inner.structure_with_llm_node import StructureWithLlmNode
from src.nodes.inner.update_incident_node import UpdateIncidentNode
from src.nodes.inner.validate_records_node import ValidateRecordsNode
from src.nodes.inner.write_records_node import WriteRecordsNode
from src.schemas.state import State


class AquacultureWorkflowState(State):
    """Inner graph state schema — see docs/02_design.md §🔴 inner graph の
    state 型注釈方針. Identical field contract to the outer State (this
    template's inner/outer schemas share the same custom fields), declared
    as its own type so route()/get_output() type annotations resolve
    against the real schema rather than the framework base AgentState.
    """


class AquacultureWorkflowGraph(BaseGraph):
    """Inner domain workflow — mode-dispatch across /ingest /invoke /feedback.

    Topology (mode-dependent, wired via START edges to each mode's entry node
    and END edges from each mode's terminal node — LangGraph does not require
    a single shared entry/exit node, only that every registered node is
    reachable):

        /invoke:   START -> dispatch -> signal_detection -> signal_matching -> advisory_synthesis -> END
        /ingest:   START -> dispatch -> normalize_input -> structure_with_llm -> validate_records -> write_records -> END
        /feedback: START -> dispatch -> resolve_incident -> update_incident -> reconcile_prediction -> END

    `dispatch` is required because BaseGraph.invoke(user_input, ...) only
    seeds state["user_input"] — it cannot inject arbitrary custom state
    fields at invocation time. DispatchNode parses the JSON envelope
    (built by the outer AquacultureWorkflowGraphNode.extract_input()) into
    state["mode"] / farm_id / species_code / payload / ... so the
    conditional edge immediately after it has state["mode"] to route on.
    """

    def __init__(self, config: dict[str, Any] | None = None, incident_store=None) -> None:
        # incident_store is a constructor-injection seam (fake in tests,
        # real store wired by server.py's wire_services()). Falls back to
        # an in-memory store so the graph never crashes for lack of wiring
        # in non-write-critical paths (e.g. ad-hoc local smoke tests).
        from src.services.incident_store import InMemoryIncidentStore

        self._incident_store = incident_store or InMemoryIncidentStore()
        super().__init__(config=config or {})

    @property
    def name(self) -> str:
        return "aquaculture_workflow"

    @property
    def state_schema(self) -> type:
        return AquacultureWorkflowState

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        llm = self.config.get("llm")

        self._nodes["dispatch"] = DispatchNode()

        # /invoke
        self._nodes["signal_detection"] = SignalDetectionNode(incident_store=self._incident_store)
        self._nodes["signal_matching"] = SignalMatchingNode(incident_store=self._incident_store)
        self._nodes["advisory_synthesis"] = AdvisorySynthesisNode(llm=llm)

        # /ingest
        self._nodes["normalize_input"] = NormalizeInputNode()
        self._nodes["structure_with_llm"] = StructureWithLlmNode(llm=llm)
        self._nodes["validate_records"] = ValidateRecordsNode()
        self._nodes["write_records"] = WriteRecordsNode(incident_store=self._incident_store)

        # /feedback
        self._nodes["resolve_incident"] = ResolveIncidentNode(incident_store=self._incident_store)
        self._nodes["update_incident"] = UpdateIncidentNode(incident_store=self._incident_store, llm=llm)
        self._nodes["reconcile_prediction"] = ReconcilePredictionNode(incident_store=self._incident_store)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "dispatch")
        self._sg.add_conditional_edges(
            "dispatch",
            self._route_entry,
            {
                "invoke": "signal_detection",
                "ingest": "normalize_input",
                "feedback": "resolve_incident",
                "error": END,
            },
        )

        # /invoke pipeline
        self._sg.add_edge("signal_detection", "signal_matching")
        self._sg.add_edge("signal_matching", "advisory_synthesis")
        self._sg.add_edge("advisory_synthesis", END)

        # /ingest pipeline
        self._sg.add_edge("normalize_input", "structure_with_llm")
        self._sg.add_edge("structure_with_llm", "validate_records")
        self._sg.add_edge("validate_records", "write_records")
        self._sg.add_edge("write_records", END)

        # /feedback pipeline
        self._sg.add_edge("resolve_incident", "update_incident")
        self._sg.add_edge("update_incident", "reconcile_prediction")
        self._sg.add_edge("reconcile_prediction", END)

    def _route_entry(self, state: AquacultureWorkflowState) -> str:
        """Mode dispatch — MUST be annotated with AquacultureWorkflowState,
        not AgentState (docs/02_design.md §🔴 inner graph の state 型注釈方針:
        annotating with the base AgentState causes state projection to drop
        the custom `mode` field and route to the wrong branch).

        Runs after `dispatch`, so state["mode"] is guaranteed to be set (or
        DispatchNode already routed status=error, handled below).
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return "error"
        mode = state.get("mode")
        if mode not in ("invoke", "ingest", "feedback"):
            return "error"
        return mode

    def route(self, state: AquacultureWorkflowState) -> str:
        """Required by BaseGraph ABC. Not used for conditional routing beyond
        the entry dispatch (add_conditional_edges references _route_entry,
        not this method) — each mode's internal pipeline is linear.
        """
        return END

    def get_output(self, state: AquacultureWorkflowState) -> dict:
        """Shape sub_result for the outer AquacultureWorkflowGraphNode.merge_output().

        MUST be annotated with AquacultureWorkflowState — see module docstring.
        Returns every mode-specific field; merge_output() on the outer side
        picks only the ones relevant to the mode that actually ran.
        """
        output: dict[str, Any] = {
            "status": state.get("status", AgentStatus.SUCCESS.value),
        }
        if state.get("degradation_reason"):
            output["degradation_reason"] = state["degradation_reason"]

        mode = state.get("mode")
        if mode == "invoke":
            output["signal_list"] = state.get("signal_list", [])
            # Truncation disclosure must cross the inner/outer boundary too —
            # same projection pitfall as unparseable_observations below: an
            # undeclared/omitted key is silently dropped by LangGraph's
            # state_schema projection, which would hide from the operator that
            # signal_list was capped at all.
            output["signal_list_total_count"] = state.get(
                "signal_list_total_count", len(output["signal_list"])
            )
            if state.get("signal_list_truncated"):
                output["signal_list_truncated"] = True
            output["candidate_risks"] = state.get("candidate_risks", [])
            # Truncation disclosure must cross the inner/outer boundary too —
            # same projection pitfall as signal_list_total_count above: an
            # omitted key is silently dropped by LangGraph's state_schema
            # projection, hiding from the operator that candidate_risks was
            # capped at all.
            output["candidate_risks_total_count"] = state.get(
                "candidate_risks_total_count", len(output["candidate_risks"])
            )
            if state.get("candidate_risks_truncated"):
                output["candidate_risks_truncated"] = True
            output["advisory_output"] = state.get("advisory_output", {})
            # Data-quality defects must cross the inner/outer boundary. Omitting
            # this key here silently discards them (LangGraph projects node I/O
            # through state_schema) — the operator would never learn a reading
            # was unreadable.
            if state.get("unparseable_observations"):
                output["unparseable_observations"] = state["unparseable_observations"]
                output["unparseable_observation_count"] = state.get(
                    "unparseable_observation_count", len(state["unparseable_observations"])
                )
        elif mode == "ingest":
            output["ingest_summary"] = state.get("ingest_summary", {})
        elif mode == "feedback":
            output["feedback_result"] = state.get("feedback_result", {})

        return output
