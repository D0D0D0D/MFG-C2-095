# TC-06 / TC-07: FunctionNode._security_gate_input()/_security_gate_output()
# are @final — attempting to override either in a domain subclass must
# raise TypeError at class definition time (docs/02_design.md §S-2/S-3).

import pytest
from framework.nodes.function_node import FunctionNode


class TestSecurityGateNonBypassable:
    def test_overriding_security_gate_input_raises_type_error(self):
        with pytest.raises(TypeError):

            class BadNodeInput(FunctionNode):
                def _security_gate_input(self, state):  # noqa: D401 — intentional violation
                    return state

                def execute(self, state: dict) -> dict:
                    return {"status": "success"}

    def test_overriding_security_gate_output_raises_type_error(self):
        with pytest.raises(TypeError):

            class BadNodeOutput(FunctionNode):
                def _security_gate_output(self, result):  # noqa: D401 — intentional violation
                    return result

                def execute(self, state: dict) -> dict:
                    return {"status": "success"}

    def test_all_domain_function_nodes_use_extra_hooks_only(self):
        """All FunctionNode subclasses in this template must extend via the
        _extra_* hooks — a simple existence check that they define __init__
        or execute() without redefining the @final gate methods themselves
        (the __init_subclass__ check above already makes redefinition
        impossible; this test documents the intended extension pattern)."""
        import importlib
        import pkgutil

        pkg = importlib.import_module("src.nodes")
        node_classes = []
        for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes.", onerror=lambda _n: None):
            module = importlib.import_module(modname)
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, FunctionNode)
                    and attr is not FunctionNode
                    and attr.__module__ == modname
                ):
                    node_classes.append(attr)

        assert node_classes, "expected at least one FunctionNode subclass under src/nodes/"
        for cls in node_classes:
            assert "_security_gate_input" not in cls.__dict__
            assert "_security_gate_output" not in cls.__dict__
