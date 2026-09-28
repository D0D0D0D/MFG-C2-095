# PB-6: Invoke Execution Order Verification
# Verifies BaseNode.__call__() enforces: S-1 trust gate -> S-4 node_start ->
# S-2 _security_gate_input() -> execute() -> S-3 _security_gate_output() ->
# S-4 node_complete, for every concrete node under src/nodes/ (including
# src/nodes/inner/, the domain_workflow_graph node set).
#
# Several MFG-C2-095 nodes take required constructor arguments (incident
# store / llm / weather client injection seams — docs/02_design.md §main
# 契約, §services). _NODE_FACTORY_OVERRIDES supplies a no-op-safe
# instance for those so this generic order-of-execution scan still covers
# every node without needing per-agent framework changes.

import importlib
import inspect
import pkgutil
from typing import ClassVar

import pytest
from framework.nodes.base_node import BaseNode
from framework.schemas.trust_level import TrustLevel


class _PrivilegedTrustGateFixture(BaseNode):
    """Always-present privileged node used to prove the S-1 negative boundary."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _security_gate_input(self, state):
        return state

    def execute(self, state):
        return {"status": "success"}

    def _security_gate_output(self, result):
        return result


def _trust_predecessor(required: TrustLevel) -> TrustLevel:
    """Return a lower valid trust level; fail loudly if the framework adds one."""
    predecessors = {
        TrustLevel.VERIFIED_EXTERNAL: TrustLevel.ANONYMOUS,
        TrustLevel.INTERNAL: TrustLevel.VERIFIED_EXTERNAL,
    }
    try:
        return predecessors[required]
    except KeyError as exc:
        raise AssertionError(f"no lower trust level defined for {required!r}") from exc


def _build_incident_store():
    from src.services.incident_store import InMemoryIncidentStore

    return InMemoryIncidentStore()


def _node_factory_overrides() -> dict[str, callable]:
    """Map node class name -> zero-arg factory for classes whose __init__
    requires constructor-injected collaborators (fakes are sufficient here;
    this test only verifies __call__() ordering, not business logic)."""
    from src.nodes.inner.advisory_synthesis_node import AdvisorySynthesisNode
    from src.nodes.inner.reconcile_prediction_node import ReconcilePredictionNode
    from src.nodes.inner.resolve_incident_node import ResolveIncidentNode
    from src.nodes.inner.signal_detection_node import SignalDetectionNode
    from src.nodes.inner.signal_matching_node import SignalMatchingNode
    from src.nodes.inner.structure_with_llm_node import StructureWithLlmNode
    from src.nodes.inner.update_incident_node import UpdateIncidentNode
    from src.nodes.inner.write_records_node import WriteRecordsNode

    return {
        SignalDetectionNode.__name__: lambda: SignalDetectionNode(incident_store=_build_incident_store()),
        SignalMatchingNode.__name__: lambda: SignalMatchingNode(incident_store=_build_incident_store()),
        AdvisorySynthesisNode.__name__: lambda: AdvisorySynthesisNode(llm=None),
        StructureWithLlmNode.__name__: lambda: StructureWithLlmNode(llm=None),
        WriteRecordsNode.__name__: lambda: WriteRecordsNode(incident_store=_build_incident_store()),
        ResolveIncidentNode.__name__: lambda: ResolveIncidentNode(incident_store=_build_incident_store()),
        UpdateIncidentNode.__name__: lambda: UpdateIncidentNode(incident_store=_build_incident_store(), llm=None),
        ReconcilePredictionNode.__name__: lambda: ReconcilePredictionNode(incident_store=_build_incident_store()),
    }


def _discover_node_classes() -> list[type]:
    """Import every module under src/nodes/ (recursively, including
    src/nodes/inner/) and collect concrete BaseNode subclasses.

    GraphNode subclasses (here: AquacultureWorkflowGraphNode) are excluded:
    GraphNode.execute() invokes a full inner BaseGraph.invoke() which
    requires a complete InvocationContext.from_state(state), not the
    minimal state this generic scan constructs. GraphNode's S-2/S-3
    delegation-to-hook contract is still exercised — see
    tests/unit/test_main_node.py for its dedicated behavioral coverage —
    and PB-6's S-1/S-4 ordering guarantee comes from the shared
    BaseNode.__call__() implementation exercised by every FunctionNode
    subclass in this same scan.
    """
    from framework.nodes.graph_node import GraphNode

    try:
        pkg = importlib.import_module("src.nodes")
    except ImportError as exc:
        pytest.fail(f"PB-6 cannot import src.nodes; framework/template setup is broken: {exc}")

    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes.", onerror=lambda _n: None):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and not issubclass(attr, GraphNode)
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestInvokeOrder:
    """PB-6: __call__ must run S-1 -> node_start -> S-2 -> execute() -> S-3 -> node_complete."""

    def test_s1_denial_refuses_execution_before_execute(self, monkeypatch):
        """TC-08: an always-present privileged node proves the negative S-1 path."""
        import framework.nodes.base_node as base_node_module

        events: list[str] = []
        execute_calls: list[object] = []
        monkeypatch.setattr(
            base_node_module,
            "emit_trace_event",
            lambda event_type, _payload, _state: events.append(event_type),
        )
        original_execute = _PrivilegedTrustGateFixture.execute

        def spy_execute(self, state):
            execute_calls.append(state)
            return original_execute(self, state)

        monkeypatch.setattr(_PrivilegedTrustGateFixture, "execute", spy_execute)
        result = _PrivilegedTrustGateFixture()(
            {
                "caller_trust_level": _trust_predecessor(
                    _PrivilegedTrustGateFixture.required_trust_level
                ).value,
                "correlation_id": "tc08-s1-denial",
            }
        )

        assert result["status"] == "error"
        assert "S-1 trust gate denied" in result["error_log"][0]
        assert events == ["s1_denied"]
        assert not execute_calls

    def test_call_order_for_every_node(self, monkeypatch):
        node_classes = _discover_node_classes()
        if not node_classes:
            pytest.skip("no concrete BaseNode subclasses found under src/nodes/")

        overrides = _node_factory_overrides()

        import framework.nodes.base_node as base_node_module

        failures: list[str] = []
        for node_cls in node_classes:
            order: list[str] = []
            monkeypatch.setattr(
                base_node_module,
                "emit_trace_event",
                lambda event_type, _payload, _state, _o=order: _o.append(f"event:{event_type}"),
            )

            for method_name, label in (
                ("_security_gate_input", "security_gate_input"),
                ("execute", "execute"),
                ("_security_gate_output", "security_gate_output"),
            ):
                original = getattr(node_cls, method_name)

                def spy(self, arg, _o=order, _label=label, _orig=original):
                    _o.append(_label)
                    return _orig(self, arg)

                monkeypatch.setattr(node_cls, method_name, spy)

            factory = overrides.get(node_cls.__name__, node_cls)
            instance = factory()
            state = {
                "caller_trust_level": node_cls.required_trust_level.value,
                "correlation_id": "pb6-invoke-order-test",
                # Minimal fields so nodes that read mode/farm_id/etc. inside
                # execute() don't raise before reaching the order-of-calls
                # assertions below (this test only cares about call order).
                "mode": "invoke",
                "farm_id": "pb6-farm",
                "species_code": "madai",
                "payload": {},
                "user_input": "{}",
            }
            instance(state)

            expected = [
                "event:node_start",
                "security_gate_input",
                "execute",
                "security_gate_output",
                "event:node_complete",
            ]
            if order != expected:
                failures.append(
                    f"{node_cls.__name__}: invoke order violation.\n" f"expected: {expected}\nactual:   {order}"
                )

        assert not failures, "\n\n".join(failures)
