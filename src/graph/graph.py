"""AgentCore Platform v1.0"""

# MFG-C2-095 AquacultureEarlyWarningGraph — Cat 2 outer graph.
# Fixed backbone: initialize -> pre_process -> main -> {route} -> post_process -> finalize.
# Domain complexity (3 modes, 3-stage /invoke pipeline) lives in the inner
# AquacultureWorkflowGraph, wrapped by AquacultureWorkflowGraphNode in the
# `main` slot (docs/02_design.md §main（GraphNode）契約).
#
# add_edges()/route() are NOT overridden — backbone wiring belongs to the framework.

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import AquacultureWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class AquacultureEarlyWarningGraph(AgentBaseGraph):
    """MFG-C2-095 — AquacultureEnvironmentalEarlyWarningAgent.

    config keys consumed:
      llm             — BaseLLM instance (project standard injection seam,
                         config["llm"]) or None -> deterministic fallback
                         (docs/02_design.md その他の設計方針).
      incident_store  — BaseIncidentStore instance; defaults to
                         InMemoryIncidentStore when absent (real backend
                         wired by src/api/server.py wire_services()).
      weather_client  — BaseWeatherClient instance for pre_process; None
                         means /invoke runs with an empty forecast_context
                         (graceful degradation, docs/02_design.md §(d)).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)

    @property
    def name(self) -> str:
        return "aquaculture_early_warning"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        incident_store = self.config.get("incident_store")
        weather_client = self.config.get("weather_client")

        self._nodes["pre_process"] = PreProcessNode(weather_client=weather_client)
        self._nodes["main"] = AquacultureWorkflowGraphNode(
            config={"llm": self.config.get("llm")}, incident_store=incident_store
        )
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.
