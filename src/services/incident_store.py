"""AgentCore Platform v1.0"""

# Domain data store abstraction for env_observations / inputs / incidents
# (docs/02_design.md §データモデル概要). This template owns the schema; the
# concrete backend (Postgres, SQLite, in-memory) is an injection seam so
# tests never touch a real DB; the target backend is deployment-specific.
#
# IMPORTANT (docs/02_design.md §State Constraints): incident detail records
# returned by this store must never be placed directly into graph State —
# only reference IDs flow through State/checkpoints. Resolution of full
# incident detail happens in the API adapter layer (src/api/server.py) only.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Store-side defense-in-depth cap on query_similar_incidents (Stage 2). This
# is a SECOND, independent bound layered in front of SignalMatchingNode's own
# `_MAX_CANDIDATE_RISKS` cap (src/nodes/inner/signal_matching_node.py) — a
# store that returns an unbounded match set (e.g. a real DB backend with a
# broad similarity query) should not have to rely on the node remembering to
# cap afterward. 200 mirrors the node-side cap: see that module for the
# rationale. A store MAY return fewer than `limit` rows (never required to
# pad); callers must not assume exactly `limit` rows come back.
_DEFAULT_SIMILAR_INCIDENTS_LIMIT = 200


class BaseIncidentStore(ABC):
    """Injection seam for the 3-series schema (env_observations/inputs/incidents)."""

    @abstractmethod
    def query_env_observations(
        self, farm_id: str, pond_id: str | None, metric: str, since_years: int
    ) -> list[dict[str, Any]]:
        """Return historical env_observations rows for multi-year comparison (Stage 1)."""

    @abstractmethod
    def query_similar_incidents(
        self,
        farm_id: str,
        species_code: str,
        signal_list: list[dict[str, Any]],
        limit: int = _DEFAULT_SIMILAR_INCIDENTS_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return incidents whose environmental_context is similar to signal_list (Stage 2).

        `limit` bounds the number of rows returned — a defense-in-depth cap
        the store applies BEFORE SignalMatchingNode's own severity-priority
        cap runs on the result (see that node's `_MAX_CANDIDATE_RISKS`).
        Implementations MAY return fewer rows than `limit`; callers MUST NOT
        assume an unbounded result set.
        """

    @abstractmethod
    def write_env_observations(self, farm_id: str, records: list[dict[str, Any]]) -> int:
        """Bulk-insert env_observations rows. Returns count written."""

    @abstractmethod
    def write_inputs(self, farm_id: str, records: list[dict[str, Any]]) -> int:
        """Bulk-insert inputs (投入/処置) rows. Returns count written."""

    @abstractmethod
    def write_incidents(self, farm_id: str, records: list[dict[str, Any]]) -> list[str]:
        """Bulk-insert incidents rows. Returns list of assigned incident_id."""

    @abstractmethod
    def resolve_incident_id(self, farm_id: str, incident_id: str | None) -> str:
        """Return an existing incident_id, or mint a new deterministic one."""

    @abstractmethod
    def update_incident(self, farm_id: str, incident_id: str, fields: dict[str, Any]) -> None:
        """Update (or insert if absent) an incidents row identified by incident_id."""

    @abstractmethod
    def get_recent_advisory(self, farm_id: str) -> dict[str, Any] | None:
        """Return the most recent /invoke advisory_output for this farm, if any
        (used by reconcile_prediction in /feedback)."""


class InMemoryIncidentStore(BaseIncidentStore):
    """In-memory fake store — used by tests and as a safe local-dev default.

    Not for production use (no persistence across process restarts).
    """

    def __init__(self) -> None:
        self._env_observations: dict[str, list[dict[str, Any]]] = {}
        self._inputs: dict[str, list[dict[str, Any]]] = {}
        self._incidents: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_advisory: dict[str, dict[str, Any]] = {}
        self._incident_seq = 0

    def query_env_observations(
        self, farm_id: str, pond_id: str | None, metric: str, since_years: int
    ) -> list[dict[str, Any]]:
        rows = self._env_observations.get(farm_id, [])
        return [
            r
            for r in rows
            if r.get("metric") == metric and (pond_id is None or r.get("pond_id") == pond_id)
        ]

    def query_similar_incidents(
        self,
        farm_id: str,
        species_code: str,
        signal_list: list[dict[str, Any]],
        limit: int = _DEFAULT_SIMILAR_INCIDENTS_LIMIT,
    ) -> list[dict[str, Any]]:
        incidents = self._incidents.get(farm_id, {})
        signal_metrics = {s.get("metric") for s in signal_list}
        matched = []
        for incident in incidents.values():
            if incident.get("species") != species_code:
                continue
            env_ctx = incident.get("environmental_context", {})
            if isinstance(env_ctx, dict) and signal_metrics & set(env_ctx.keys()):
                matched.append(incident)
                if len(matched) >= limit:
                    break
        return matched

    def write_env_observations(self, farm_id: str, records: list[dict[str, Any]]) -> int:
        self._env_observations.setdefault(farm_id, []).extend(records)
        return len(records)

    def write_inputs(self, farm_id: str, records: list[dict[str, Any]]) -> int:
        self._inputs.setdefault(farm_id, []).extend(records)
        return len(records)

    def write_incidents(self, farm_id: str, records: list[dict[str, Any]]) -> list[str]:
        ids = []
        for rec in records:
            incident_id = rec.get("incident_id") or self.resolve_incident_id(farm_id, None)
            rec = {**rec, "incident_id": incident_id}
            self._incidents.setdefault(farm_id, {})[incident_id] = rec
            ids.append(incident_id)
        return ids

    def resolve_incident_id(self, farm_id: str, incident_id: str | None) -> str:
        if incident_id and incident_id in self._incidents.get(farm_id, {}):
            return incident_id
        self._incident_seq += 1
        return f"inc-{farm_id}-{self._incident_seq:06d}"

    def update_incident(self, farm_id: str, incident_id: str, fields: dict[str, Any]) -> None:
        farm_incidents = self._incidents.setdefault(farm_id, {})
        existing = farm_incidents.get(incident_id, {"incident_id": incident_id})
        existing.update(fields)
        farm_incidents[incident_id] = existing

    def get_recent_advisory(self, farm_id: str) -> dict[str, Any] | None:
        return self._last_advisory.get(farm_id)

    def record_advisory(self, farm_id: str, advisory: dict[str, Any]) -> None:
        """Test/dev helper — not part of the ABC (real store persists on write_records)."""
        self._last_advisory[farm_id] = advisory
