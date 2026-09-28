# MFG-C2-095 — Unit Tests: src/api/server.py wire_services()
#
# 🔴 Why this file exists: wire_services() is module-level deployment wiring
# that NO other test and no CI job executes, so a mistake here surfaces only at
# STG deploy. It previously imported `AnthropicLLMClient` — a name that does
# not exist in shared.services.llm.anthropic_client (the class is
# `AnthropicClient`) — and constructed it with no arguments although its
# __init__ requires config["api_key"]. A blanket `except Exception` swallowed
# both, so production ran with llm=None on every request while
# config/agent.yaml declared generation_mode: "llm".
#
# The distinction these tests pin: exactly TWO conditions may degrade to
# llm=None (the optional `[anthropic]` extra being absent, and the API key not
# being provisioned — in both cases the service keeps serving the
# deterministic path). Every other failure is a wiring bug and must
# propagate rather than silently disable the LLM.

import importlib.util
import sys
import types

import pytest
from framework.secrets.base import MissingSecret

import src.api.server as server


class _StubProvider:
    def __init__(self, key: str | None = "sk-test-key"):
        self._key = key

    def require(self, name: str) -> str:
        assert name == "ANTHROPIC_API_KEY"
        if self._key is None:
            raise MissingSecret(f"required secret '{name}' not found")
        return self._key


def _stub_anthropic_module(record: dict) -> types.ModuleType:
    """Stand-in for shared.services.llm.anthropic_client.

    Exposes ONLY the real symbol name and the real constructor arity, so
    reverting either half of the fix fails loudly here.
    """
    mod = types.ModuleType("shared.services.llm.anthropic_client")

    class AnthropicClient:
        def __init__(self, config: dict):
            record["config"] = config

    mod.AnthropicClient = AnthropicClient
    return mod


class TestWireServices:
    def test_constructs_the_real_client_with_the_provisioned_api_key(self, monkeypatch):
        record: dict = {}
        monkeypatch.setitem(sys.modules, "shared.services.llm.anthropic_client", _stub_anthropic_module(record))
        seen_kwargs: dict = {}

        def _factory(**kw):
            seen_kwargs.update(kw)
            return _StubProvider()

        monkeypatch.setattr(server, "secrets_factory", _factory)

        config = server.wire_services()

        # The secret namespace scopes which agent's secrets are readable; a
        # wrong namespace silently reads another agent's key store.
        assert seen_kwargs == {"namespace": "mfg", "agent_name": "mfg-c2-095"}
        assert config["llm"] is not None, (
            "wire_services() failed to construct the LLM client even though the "
            "module imported cleanly — production would silently degrade to the "
            "deterministic path on every request."
        )
        assert record["config"]["api_key"] == "sk-test-key"

    @pytest.mark.skipif(
        importlib.util.find_spec("langchain_anthropic") is not None,
        reason="`[anthropic]` extra IS installed here — this case needs it absent",
    )
    def test_missing_anthropic_extra_degrades_to_none_without_crashing(self, monkeypatch):
        """(a) The optional dependency itself is absent — documented degradation.

        Uses the REAL import failure rather than a simulated one: with the
        `[anthropic]` extra uninstalled, importing the client module raises
        ModuleNotFoundError naming `langchain_anthropic` from inside
        shared/services/llm/anthropic_client.py. Dropping the cached module
        forces wire_services() to take that genuine path.
        """
        monkeypatch.delitem(sys.modules, "shared.services.llm.anthropic_client", raising=False)
        monkeypatch.setattr(server, "secrets_factory", lambda **kw: _StubProvider())

        config = server.wire_services()
        assert config["llm"] is None
        assert config["incident_store"] is not None

    def test_missing_api_key_degrades_to_none_without_crashing(self, monkeypatch):
        """(b) Key not provisioned — documented degradation.

        🔴 Regression guard: resolving the secret at module-import time means a
        raised MissingSecret aborts the import of src/api/server.py entirely,
        taking /health down with it, instead of the documented behaviour
        (未設定時は fallback し、サービスは停止しない).
        """
        record: dict = {}
        monkeypatch.setitem(sys.modules, "shared.services.llm.anthropic_client", _stub_anthropic_module(record))
        monkeypatch.setattr(server, "secrets_factory", lambda **kw: _StubProvider(key=None))

        config = server.wire_services()
        assert config["llm"] is None
        assert config["incident_store"] is not None

    def test_unrelated_missing_module_is_a_wiring_bug_and_propagates(self, monkeypatch):
        """Only `langchain_anthropic` counts as "the optional extra is absent".

        A ModuleNotFoundError naming anything else means our own import graph
        is broken (a typo'd module path, a removed transitive dependency).
        Degrading to llm=None there would disable the LLM for every template and report
        it as a supported configuration, which is the failure mode this whole
        file exists to prevent.
        """
        broken = types.ModuleType("shared.services.llm.anthropic_client")

        def _raise_other(name):
            raise ModuleNotFoundError("No module named 'some_internal_dep'", name="some_internal_dep")

        broken.__getattr__ = _raise_other  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "shared.services.llm.anthropic_client", broken)
        monkeypatch.setattr(server, "secrets_factory", lambda **kw: _StubProvider())

        with pytest.raises(ModuleNotFoundError):
            server.wire_services()

    def test_absent_public_symbol_is_a_wiring_bug_and_propagates(self, monkeypatch):
        """A renamed/absent public symbol must NOT be read as "extra missing".

        `from X import Name` raises plain ImportError when the module exists but
        the name does not — exactly the original `AnthropicLLMClient` typo.
        Swallowing it is what let production run llm=None for the whole life of
        the template, so it must now surface.
        """
        empty = types.ModuleType("shared.services.llm.anthropic_client")
        monkeypatch.setitem(sys.modules, "shared.services.llm.anthropic_client", empty)
        monkeypatch.setattr(server, "secrets_factory", lambda **kw: _StubProvider())

        with pytest.raises(ImportError):
            server.wire_services()
