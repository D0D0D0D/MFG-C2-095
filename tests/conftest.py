"""Shared fixtures for live LLM smoke coverage.

The weather fake and invoke payload mirror `tests/integration/test_graph.py`.
"""

from __future__ import annotations

import pytest

from src.services.incident_store import InMemoryIncidentStore
from src.services.weather_client import BaseWeatherClient, WeatherClientError


class FakeWeatherClient(BaseWeatherClient):
    def __init__(self, fail: bool = False):
        self._fail = fail

    def fetch_forecast(self, area_code: str) -> dict:
        if self._fail:
            raise WeatherClientError("boom")
        return [{"publishingOffice": "test", "reportDatetime": "2026-07-10T00:00:00+09:00", "timeSeries": []}]

    def fetch_bosai_feed(self) -> str:
        if self._fail:
            raise WeatherClientError("boom")
        return "<feed/>"


@pytest.fixture
def agent_config() -> dict:
    """Graph config for integration and live-LLM checks.

    🔴 This fixture used to monkeypatch the live-check harness's BaseLLM client
    so it would accept a bare string prompt and hand back a bare string. That
    made the harness conform to a defect in this template (nodes calling
    `complete(prompt_str)` and using the result as text) instead of exposing
    it, so the live LLM turn could not detect the contract violation. The nodes
    now speak the real BaseLLM contract via `src/services/llm_text.py`, and the
    harness client is used unmodified.
    """
    return {"incident_store": InMemoryIncidentStore(), "weather_client": FakeWeatherClient(), "max_retry": 1}


@pytest.fixture
def case_payload() -> dict:
    """Integration invoke payload that produces a critical signal."""
    return {
        "mode": "invoke",
        "farm_id": "farm-001",
        "species_code": "madai",
        "area_code": "130000",
        "payload": {
            "current_observations": [
                {"metric": "water_temp", "value": 31.5, "observed_at": "2026-07-10", "pond_id": "p1"}
            ]
        },
    }


@pytest.fixture
def live_input_context() -> dict:
    """The agent payload carries mode/farm/species fields; no extra context is needed."""
    return {"mode": "invoke"}
