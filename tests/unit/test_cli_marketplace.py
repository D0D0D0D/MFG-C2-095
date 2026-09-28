"""Marketplace adapter contract tests for MFG-C2-095."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, SecretProvider
from framework.secrets.context import bound_secrets

import cli


_ROOT = Path(__file__).resolve().parents[2]
_DEPLOYMENT_ENV = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    "MARKETPLACE_FARM_ID": "farm-from-environment",
}


class _IgnoredProvider(SecretProvider):
    def get(self, key: str, default: str | None = None) -> str | None:
        del key
        return default


class _RecordingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        self.calls += 1
        prompt = "\n".join(str(message.get("content", "")) for message in messages)
        match = re.search(r"'(sig-[^']+)'", prompt)
        citation = match.group(1) if match else "sig-test"
        return {
            "content": f"水温変化に注意が必要な可能性があります[{citation}]。",
            "tool_calls": [],
            "model": "recording-test-llm",
            "usage": {},
        }


def _file_config() -> dict[str, Any]:
    config = cli.load_agent_config(_ROOT)
    assert config
    return config


def _set_valid_deployment_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _DEPLOYMENT_ENV.items():
        monkeypatch.setenv(key, value)


def _ctx() -> InvocationContext:
    return InvocationContext(
        session_id="marketplace-test",
        caller_id="marketplace-caller",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
    )


def _invoke_runner_order(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    input_context: dict[str, Any] | None = None,
) -> tuple[cli.MarketplaceGraph, dict[str, Any]]:
    _set_valid_deployment_environment(monkeypatch)
    graph = cli.MarketplaceGraph(config=config if config is not None else _file_config())
    graph.compile()
    graph.provision_secrets(_IgnoredProvider())
    with bound_secrets(graph._secrets_provider):
        result = graph.invoke(json.dumps(payload), ctx=_ctx(), input_context=input_context)
    return graph, result


def _invoke_payload(*, farm_id: str = "farm-marketplace") -> dict[str, Any]:
    return {
        "mode": "invoke",
        "farm_id": farm_id,
        "species_code": "madai",
        "payload": {"current_observations": []},
    }


def test_runner_order_returns_nonempty_output_and_discards_conversation_history(monkeypatch):
    _, result = _invoke_runner_order(
        monkeypatch,
        _invoke_payload(),
        input_context={"conversation_history": [{"role": "user", "content": "CANARY_STRING_XYZ"}]},
    )

    assert result["status"] == "success"
    assert result["output"]
    assert "CANARY_STRING_XYZ" not in str(result)


def test_injected_llm_is_called_and_does_not_report_llm_unavailable(monkeypatch):
    recorder = _RecordingLLM()
    config = {**_file_config(), "llm": recorder}
    payload = _invoke_payload()
    payload["payload"]["current_observations"] = [
        {
            "metric": "water_temp",
            "value": 31.5,
            "observed_at": "2026-07-10",
            "pond_id": "p1",
        }
    ]

    _, result = _invoke_runner_order(monkeypatch, payload, config=config)

    assert recorder.calls >= 1
    assert "llm_unavailable" not in str(result.get("degradation_reason", ""))
    assert "llm_unavailable" not in str(result["output"].get("degradation_reason", ""))


def test_runner_config_injects_azure_llm_into_the_main_node():
    graph = cli.MarketplaceGraph(config=_file_config())
    graph.compile()

    assert isinstance(graph._nodes["main"]._config["llm"], cli._AzureMarketplaceLLM)


def test_caller_llm_injection_is_preserved():
    fake = _RecordingLLM()
    graph = cli.MarketplaceGraph(config={"llm": fake, **_file_config()})
    graph.compile()

    assert graph._nodes["main"]._config["llm"] is fake


def test_null_llm_config_is_replaced_with_azure_wrapper():
    graph = cli.MarketplaceGraph(config={**_file_config(), "llm": None})
    graph.compile()

    assert isinstance(graph._nodes["main"]._config["llm"], cli._AzureMarketplaceLLM)


def test_azure_wrapper_retains_no_connection_values_and_bind_tools_returns_a_copy():
    llm = cli._AzureMarketplaceLLM()
    bound = llm.bind_tools([object()])

    assert vars(llm) == {}
    assert bound is not llm
    assert set(vars(bound)) == {"_tools"}


def test_azure_wrapper_checks_secrets_before_optional_client_import():
    with pytest.raises(MissingSecret):
        cli._AzureMarketplaceLLM()._client()


def test_missing_weather_client_is_wired_before_compile():
    graph = cli.MarketplaceGraph(config=_file_config())
    graph.compile()

    assert isinstance(graph._nodes["pre_process"]._weather_client, cli.JmaWeatherClient)


@pytest.mark.parametrize("mode, write_field", [("ingest", "records"), ("feedback", "outcome")])
def test_marketplace_write_modes_are_rejected_without_forwarding_payload(monkeypatch, mode, write_field):
    _set_valid_deployment_environment(monkeypatch)
    payload = {
        "mode": mode,
        "farm_id": "farm-write-attempt",
        "species_code": "madai",
        "payload": {write_field: "must-not-reach-the-graph"},
    }
    rewritten = cli._marketplace_user_input(json.dumps(payload), _DEPLOYMENT_ENV["MARKETPLACE_FARM_ID"])

    assert json.loads(rewritten)["mode"] == cli._MARKETPLACE_REJECTION_MODE
    assert json.loads(rewritten)["farm_id"] == _DEPLOYMENT_ENV["MARKETPLACE_FARM_ID"]
    assert "payload" not in json.loads(rewritten)
    assert write_field not in rewritten

    _, result = _invoke_runner_order(monkeypatch, payload)
    assert result["status"] == "error"
    assert "write modes are not available" in str(result.get("error_log", ""))


def test_caller_farm_id_is_overwritten_and_never_reaches_the_result(monkeypatch):
    _set_valid_deployment_environment(monkeypatch)
    attacker_farm = "ATTACKER_FARM"
    deployment_farm = _DEPLOYMENT_ENV["MARKETPLACE_FARM_ID"]
    rewritten = json.loads(
        cli._marketplace_user_input(
            json.dumps(_invoke_payload(farm_id=attacker_farm)), deployment_farm
        )
    )
    _, result = _invoke_runner_order(monkeypatch, _invoke_payload(farm_id=attacker_farm))

    assert rewritten["farm_id"] == deployment_farm
    assert result["status"] == "success"
    assert attacker_farm not in str(result)
    assert result["output"]["severity"] == "low"
    assert result["output"]["signal_list_total_count"] == 0
    assert "signal_index" not in result["output"]


@pytest.mark.parametrize("missing_key", tuple(_DEPLOYMENT_ENV))
def test_provision_secrets_fails_closed_when_required_value_is_missing(monkeypatch, missing_key):
    _set_valid_deployment_environment(monkeypatch)
    monkeypatch.delenv(missing_key)
    graph = cli.MarketplaceGraph(config=_file_config())

    with pytest.raises(MissingSecret):
        graph.provision_secrets(_IgnoredProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_valid_deployment_environment(monkeypatch)
    graph = cli.MarketplaceGraph(config=_file_config())
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        graph.provision_secrets(_IgnoredProvider())


@pytest.mark.parametrize("key", tuple(_DEPLOYMENT_ENV))
def test_provision_secrets_fails_closed_when_required_value_is_empty(monkeypatch, key):
    _set_valid_deployment_environment(monkeypatch)
    monkeypatch.setenv(key, "")
    graph = cli.MarketplaceGraph(config=_file_config())

    with pytest.raises(MissingSecret):
        graph.provision_secrets(_IgnoredProvider())


@pytest.mark.parametrize("key", tuple(_DEPLOYMENT_ENV))
@pytest.mark.parametrize("invalid_value", ["contains space", "line\nbreak"])
def test_provision_secrets_rejects_whitespace(monkeypatch, key, invalid_value):
    _set_valid_deployment_environment(monkeypatch)
    monkeypatch.setenv(key, invalid_value)
    graph = cli.MarketplaceGraph(config=_file_config())

    with pytest.raises(ValueError, match=key):
        graph.provision_secrets(_IgnoredProvider())


def test_invoke_rebuilds_input_context_and_disables_hitl(monkeypatch):
    _set_valid_deployment_environment(monkeypatch)
    captured: dict[str, Any] = {}

    def _record_parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success", "output": {"recorded": True}}

    monkeypatch.setattr(cli.AquacultureEarlyWarningGraph, "invoke", _record_parent_invoke)
    graph = cli.MarketplaceGraph(config=_file_config())
    original_ctx = _ctx()
    result = graph.invoke(
        json.dumps(_invoke_payload()),
        session_id="explicit-session",
        ctx=original_ctx,
        input_context={"conversation_history": [{"content": "CANARY_STRING_XYZ"}]},
    )

    assert result["status"] == "success"
    assert captured["input_context"] == {}
    assert captured["ctx"].hitl_allowed is False
    assert original_ctx.hitl_allowed is True
    assert captured["session_id"] == "explicit-session"


def test_main_calls_marketplace_runner_with_loaded_config(monkeypatch):
    captured: dict[str, Any] = {}

    def _record_runner(agent_cls, **kwargs):
        captured["agent_cls"] = agent_cls
        captured.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", _record_runner)
    cli.main()

    assert captured["agent_cls"] is cli.MarketplaceGraph
    assert captured["agent_name"] == "MFG-C2-095"
    assert captured["namespace"] == "mfg"
    assert captured["config"]


def test_cli_file_is_importable_with_importlib_spec():
    module_name = "mfg_c2_095_marketplace_cli_import_test"
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "cli.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    assert module._AGENT_NAME == "MFG-C2-095"
