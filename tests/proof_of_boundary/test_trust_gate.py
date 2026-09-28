# TC-08 / PB-6 (S-1 slice): required_trust_level is declared and enforced —
# a caller below the required trust level must be refused execution
# (execute() never runs; __call__ short-circuits to status=error).

from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode


class TestTrustGateEnforcement:
    def test_anonymous_caller_is_denied_by_pre_process(self):
        node = PreProcessNode()
        state = {
            "caller_trust_level": TrustLevel.ANONYMOUS.value,
            "correlation_id": "x",
            "user_input": "{}",
        }
        result = node(state)  # __call__, not execute() — S-1 gate lives in __call__
        assert result["status"] == "error"
        assert any("trust" in msg.lower() for msg in result.get("error_log", []))

    def test_verified_external_caller_passes_trust_gate(self):
        node = PostProcessNode()
        state = {
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "x",
            "mode": "invoke",
            "advisory_output": {"severity": "low", "narrative": "軽微な逸脱の可能性があります。"},
            "signal_list": [],
        }
        result = node(state)
        assert result["status"] == "success"

    def test_all_domain_nodes_declare_a_valid_trust_level(self):
        """required_trust_level must be one of the three valid TrustLevel
        enum values, never an ad-hoc string."""
        import importlib
        import inspect
        import pkgutil

        from framework.nodes.base_node import BaseNode

        pkg = importlib.import_module("src.nodes")
        node_classes = []
        for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes.", onerror=lambda _n: None):
            module = importlib.import_module(modname)
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseNode)
                    and attr is not BaseNode
                    and attr.__module__ == modname
                    and not inspect.isabstract(attr)
                ):
                    node_classes.append(attr)

        assert node_classes
        for cls in node_classes:
            assert cls.required_trust_level in (
                TrustLevel.ANONYMOUS,
                TrustLevel.VERIFIED_EXTERNAL,
                TrustLevel.INTERNAL,
            ), f"{cls.__name__}.required_trust_level is not a valid TrustLevel value"
