# TC-05 / PB-1: every node's execute() calls emit_trace_event() at least
# once — including nodes with no external side effect (e.g. validation-only
# nodes), per docs/02_design.md §セキュリティ設計方針 S-4.

import importlib
import inspect
import pkgutil


def _domain_node_classes() -> list[type]:
    from framework.nodes.base_node import BaseNode
    from framework.nodes.graph_node import GraphNode

    pkg = importlib.import_module("src.nodes")
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


class TestAuditLoggingSourceScan:
    """PB-1: emit_trace_event is imported from shared.utils.audit_logger and
    referenced inside execute() for every domain node's source module —
    static check complementing the per-node behavioral unit tests."""

    def test_every_domain_node_module_calls_emit_trace_event(self):
        import ast

        node_classes = _domain_node_classes()
        assert node_classes, "expected at least one domain node under src/nodes/"

        checked_modules = set()
        violations = []
        for cls in node_classes:
            module = inspect.getmodule(cls)
            if module is None or module.__name__ in checked_modules:
                continue
            checked_modules.add(module.__name__)
            source = inspect.getsource(module)
            tree = ast.parse(source)
            has_import = any(
                isinstance(node, ast.ImportFrom)
                and node.module == "shared.utils.audit_logger"
                and any(alias.name == "emit_trace_event" for alias in node.names)
                for node in ast.walk(tree)
            )
            has_call = "emit_trace_event(" in source
            if not (has_import and has_call):
                violations.append(module.__name__)

        assert not violations, f"Modules missing emit_trace_event usage: {violations}"
