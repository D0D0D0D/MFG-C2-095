"""AGENTIC STAR Marketplace entry adapter for MFG-C2-095."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 compatibility for this repository's test image.
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # pragma: no cover - old local wheel has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("The installed AgentCore wheel does not include Marketplace support")


from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import AquacultureEarlyWarningGraph
from src.services.weather_client import JmaWeatherClient

_NAMESPACE = "mfg"
_AGENT_NAME = "MFG-C2-095"
_PROVIDER_AGENT_NAME = "mfg-c2-095"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_FARM_ID_KEY = "MARKETPLACE_FARM_ID"
_REQUIRED_SECRET_KEYS = _AZURE_OPENAI_SECRET_KEYS + (_FARM_ID_KEY,)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)
_MARKETPLACE_REJECTION_MODE = "marketplace_mode_not_available"


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        self._namespace = _NAMESPACE
        self._agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = os.environ.get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve one required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        provider = current_secrets()
        client_config = {
            "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
            "azure_endpoint": _require_azure_endpoint(provider),
            "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
        }

        # Keep the optional Azure import lazy, but only after secret validation
        # so a missing deployment value is never misreported as an import issue.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        client: BaseLLM = AzureOpenAIClient(client_config)
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _marketplace_user_input(user_input: str, farm_id: str) -> str:
    """Force JSON packets to the deployment farm and read-only invoke mode."""

    try:
        packet = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        return user_input
    if not isinstance(packet, dict):
        return user_input

    requested_mode = packet.get("mode", "invoke")
    if requested_mode != "invoke":
        # PreProcessNode validates farm/species before mode. Preserve only
        # those routing fields so the sentinel reaches the mode check while
        # dropping every write-bearing field from the rejected request.
        return json.dumps(
            {
                "mode": _MARKETPLACE_REJECTION_MODE,
                "farm_id": farm_id,
                "species_code": packet.get("species_code"),
            },
            ensure_ascii=False,
        )

    packet["mode"] = "invoke"
    packet["farm_id"] = farm_id
    return json.dumps(packet, ensure_ascii=False)


class MarketplaceGraph(AquacultureEarlyWarningGraph):
    """Marketplace-only read boundary and Azure/JMA dependency wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        if merged.get("llm") is None:
            merged["llm"] = _AzureMarketplaceLLM()
        if merged.get("weather_client") is None:
            merged["weather_client"] = JmaWeatherClient()
        super().__init__(config=merged)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        farm_id = _require_clean(_AllowlistedEnvironmentProvider(), _FARM_ID_KEY)
        marketplace_input = _marketplace_user_input(user_input, farm_id)
        result = cast(
            dict[str, Any],
            super().invoke(
                marketplace_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )
        # AgentCore 1.0.0 omits state.error_log from its final error envelope.
        # Keep the rejection observable to Marketplace callers after the
        # sentinel has travelled through the graph's normal validation path.
        try:
            rejected_mode = json.loads(marketplace_input).get("mode")
        except (AttributeError, json.JSONDecodeError, TypeError):
            rejected_mode = None
        if rejected_mode == _MARKETPLACE_REJECTION_MODE and result.get("status") == "error":
            result.setdefault(
                "error_log",
                ["Marketplace: write modes are not available; this deployment is read-only (invoke)."],
            )
        return result

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
