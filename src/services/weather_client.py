"""AgentCore Platform v1.0"""

# JMA (気象庁) public data client — the ONLY two external sources in v1 scope
# (docs/02_design.md §(d)): forecast JSON + bosai (disaster info) Atom feed.
# Both are unauthenticated GET — no secrets required.
#
# Injection seam: pre_process constructs this via config (or receives a fake
# in tests) so no real network call happens during unit/integration tests.
# Real connectivity must be validated in the target deployment environment.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

JMA_FORECAST_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json"
JMA_AREA_TABLE_URL = "https://www.jma.go.jp/bosai/common/const/area.json"
JMA_BOSAI_EXTRA_FEED_URL = "https://www.data.jma.go.jp/developer/xml/feed/extra.xml"

DEFAULT_TIMEOUT_SECONDS = 10.0


class WeatherClientError(Exception):
    """Raised when a JMA fetch fails. Callers MUST treat this as
    non-fatal (docs/02_design.md グレースフルデグレード): /invoke continues
    with an empty forecast_context rather than aborting the pipeline.
    """


class BaseWeatherClient(ABC):
    """Injection seam interface — real HTTP client and test fakes both implement this."""

    @abstractmethod
    def fetch_forecast(self, area_code: str) -> Any:
        """Return the raw parsed JMA forecast JSON for area_code.

        Per docs/02_design.md §(d), the real JMA response top-level shape is
        a LIST of dicts (each containing publishingOffice/reportDatetime/
        timeSeries) — not a single dict. Callers (PreProcessNode) validate
        this shape via _is_valid_jma_forecast_shape() before trusting it
        (S-2, untrusted external response).
        """

    @abstractmethod
    def fetch_bosai_feed(self) -> str:
        """Return the raw bosai (disaster warning) Atom feed XML text."""


class JmaWeatherClient(BaseWeatherClient):
    """Real HTTP client for the two v1-scoped JMA public endpoints.

    No authentication — JMA forecast/bosai endpoints are open government data.
    """

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._timeout = timeout_seconds

    def fetch_forecast(self, area_code: str) -> Any:
        url = JMA_FORECAST_URL.format(area_code=area_code)
        try:
            resp = httpx.get(url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — any network/parse failure degrades gracefully
            raise WeatherClientError(f"JMA forecast fetch failed for area_code={area_code}: {exc}") from exc
        return data

    def fetch_bosai_feed(self) -> str:
        try:
            resp = httpx.get(JMA_BOSAI_EXTRA_FEED_URL, timeout=self._timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:  # noqa: BLE001
            raise WeatherClientError(f"JMA bosai feed fetch failed: {exc}") from exc
