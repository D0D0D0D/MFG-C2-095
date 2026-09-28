# MFG-C2-095 — Unit Tests: AquacultureWorkflowGraphNode (main slot, GraphNode contract)

import json

from framework.schemas.agent_status import AgentStatus

from src.nodes.main_node import AquacultureWorkflowGraphNode


class TestAquacultureWorkflowGraphNode:
    def test_get_subgraph_is_cached_not_rebuilt(self):
        node = AquacultureWorkflowGraphNode(config={"llm": None})
        first = node.get_subgraph()
        second = node.get_subgraph()
        assert first is second

    def test_extract_input_prefers_validated_input(self):
        node = AquacultureWorkflowGraphNode(config={"llm": None})
        state = {"validated_input": json.dumps({"mode": "invoke"})}
        assert node.extract_input(state) == state["validated_input"]

    def test_extract_input_falls_back_to_composed_envelope(self):
        node = AquacultureWorkflowGraphNode(config={"llm": None})
        state = {"mode": "ingest", "farm_id": "f1", "species_code": "madai", "payload": {}}
        raw = node.extract_input(state)
        parsed = json.loads(raw)
        assert parsed["mode"] == "ingest"
        assert parsed["farm_id"] == "f1"

    def test_merge_output_invoke_mode_omits_ingest_and_feedback_keys(self):
        node = AquacultureWorkflowGraphNode(config={"llm": None})
        sub_result = {
            "status": AgentStatus.SUCCESS.value,
            "signal_list": [],
            "candidate_risks": [],
            "advisory_output": {},
        }
        delta = node.merge_output({}, sub_result)
        assert "signal_list" in delta
        assert "ingest_summary" not in delta
        assert "feedback_result" not in delta

    def test_merge_output_propagates_degradation_reason(self):
        node = AquacultureWorkflowGraphNode(config={"llm": None})
        sub_result = {"status": AgentStatus.SUCCESS.value, "ingest_summary": {}, "degradation_reason": "llm_unavailable"}
        delta = node.merge_output({}, sub_result)
        assert delta["degradation_reason"] == "llm_unavailable"

    def test_required_trust_level_is_valid_enum(self):
        from framework.schemas.trust_level import TrustLevel

        assert AquacultureWorkflowGraphNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )

    def test_error_strategy_is_propagate(self):
        assert AquacultureWorkflowGraphNode.error_strategy == "propagate"

    def test_propagate_hitl_is_false(self):
        assert AquacultureWorkflowGraphNode.propagate_hitl is False
