"""AgentCore Platform v1.0"""

# Stage 2 — matching (docs/02_design.md §予兆判定の3段パイプライン).
# SEARCH, NOT LLM: matches Stage 1 signals against (a) this farm's own
# incidents KB (the moat) and (b) the species profile's disease_triggers
# dictionary. Every candidate risk carries a provenance ID
# (matched_incident_id or matched_signal_id) that Stage 3 / S-3 depend on.

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.incident_store import BaseIncidentStore
from src.services.severity_cap import apply_severity_cap

# Upper bound on the number of candidate_risks carried past Stage 2 into
# Stage 3 and ultimately into State/the response. Without this, a farm's
# incidents_kb (self._store.query_similar_incidents) re-amplifies Stage 1's
# already-capped signal_list: every one of N matched incidents becomes its
# own candidate_risks entry, so a farm with thousands of historically similar
# incidents produces a candidate_risks list (and therefore
# advisory_output.cited_incident_ids / narrative citations) that bypasses
# _MAX_SIGNALS entirely — a 3000-incident match was measured to produce a
# 214KB response from a single /invoke call; state and response size must
# remain bounded to limit denial-of-service risk.
#
# 200 mirrors SignalDetectionNode._MAX_SIGNALS's headroom reasoning one stage
# down: this template's reference deployment (docs/02_design.md) expects
# O(10-100) signals per farm per day, and even a farm with a long incident
# history matching many of those signals is not expected to have hundreds of
# DISTINCT similar incidents open at once. 200 keeps candidate_risks well
# under the size that would push narrative/response size into the multi-MB
# range while remaining generous headroom over any plausible single-farm
# volume.
#
# SAFETY-CRITICAL CONSTRAINT (same invariant as _MAX_SIGNALS): capping must
# never cause the most dangerous candidate risk to be dropped from view.
# `apply_severity_cap` (src/services/severity_cap.py — the SAME helper
# SignalDetectionNode uses for signal_list) enforces this by keeping the
# highest-`severity_baseline` candidates first. Only
# `source == "species_profile_knowledge"` entries carry their own severity
# signal (`severity_baseline`, from the species profile's disease_triggers
# dictionary); `source == "incidents_kb"` entries (matched historical
# incidents) carry no severity field of their own. That asymmetry is
# intentional, not a gap: AdvisorySynthesisNode._derive_severity does not read
# severity from incidents_kb candidates either (it only reads
# signal_list[].severity and candidate_risks[].severity_baseline) — so ranking
# incidents_kb entries at the lowest tier here (apply_severity_cap's `-1` rank
# for an absent key) exactly mirrors which candidates actually influence the
# top-level advisory severity. A species_profile_knowledge candidate is always
# preferred to survive the cap over an incidents_kb one; within either group,
# ties are broken by the STABLE SORT preserving original arrival order (no
# `magnitude_fn`, so all candidates in a tier compare equal on the tie-break
# key and Python's sort never reorders equal elements) — this keeps the cap
# fully deterministic.
_MAX_CANDIDATE_RISKS = 200


class SignalMatchingNode(FunctionNode):
    """Stage 2: match Stage 1 signals against incidents KB + species disease triggers."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore) -> None:
        super().__init__()
        self._store = incident_store

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        species_code = state.get("species_code", "")
        species_profile = state.get("species_profile", {}) or {}
        signal_list: list[dict[str, Any]] = state.get("signal_list", []) or []

        candidate_risks: list[dict[str, Any]] = []

        # (a) self-farm incidents KB search — the moat. `limit` is a store-side
        # defense-in-depth pre-filter (src/services/incident_store.py) layered
        # in front of the node-side severity-priority cap applied below — the
        # store's pre-filter has no severity ordering of its own (incidents_kb
        # rows carry no severity field), so the node-side cap remains the
        # authoritative safety guarantee.
        matched_incidents = self._store.query_similar_incidents(
            farm_id=farm_id,
            species_code=species_code,
            signal_list=signal_list,
            limit=_MAX_CANDIDATE_RISKS,
        )
        signal_metrics = {s.get("metric") for s in signal_list}
        for incident in matched_incidents:
            candidate_risks.append(
                {
                    "matched_incident_id": incident.get("incident_id"),
                    "matched_signal_id": None,
                    "category": incident.get("category"),
                    "agent": incident.get("agent"),
                    "source": "incidents_kb",
                    "matched_metrics": sorted(
                        signal_metrics & set((incident.get("environmental_context") or {}).keys())
                    ),
                }
            )

        # (b) species disease_trigger dictionary lookup.
        # Only signals already flagged medium+ by Stage 1 are matched against
        # triggers — a metric name substring match alone is too permissive
        # (every trigger mentioning e.g. "water_temp" would match any
        # water_temp signal regardless of how small the deviation is).
        triggers = species_profile.get("disease_triggers", []) or []
        for signal in signal_list:
            if signal.get("severity") not in ("medium", "high", "critical"):
                continue
            metric = signal.get("metric")
            for trigger in triggers:
                trigger_signals = trigger.get("environmental_signals", [])
                if self._signal_matches_trigger(metric, trigger_signals):
                    candidate_risks.append(
                        {
                            "matched_incident_id": None,
                            "matched_signal_id": signal.get("signal_id"),
                            "category": trigger.get("category"),
                            "agent": trigger.get("agent"),
                            "source": "species_profile_knowledge",
                            "severity_baseline": trigger.get("severity_baseline"),
                        }
                    )

        # Bound candidate_risks to _MAX_CANDIDATE_RISKS, SAFETY-CRITICAL
        # ordering — see _MAX_CANDIDATE_RISKS docstring above.
        # `apply_severity_cap` guarantees, for ANY cap >= 1: the highest
        # severity_baseline present survives, so AdvisorySynthesisNode.
        # _derive_severity() (which runs on this capped list) can never be
        # silently downgraded by truncation here, mirroring the same
        # invariant SignalDetectionNode._apply_signal_cap already provides one
        # stage up for signal_list.
        total_candidate_count = len(candidate_risks)
        capped_candidate_risks = apply_severity_cap(
            candidate_risks,
            _MAX_CANDIDATE_RISKS,
            severity_key="severity_baseline",
        )

        emit_trace_event(
            "stage2_signal_matching",
            {
                "farm_id": farm_id,
                "candidate_count": len(capped_candidate_risks),
                "candidate_count_total": total_candidate_count,
            },
            state,
        )

        result: dict[str, Any] = {
            "candidate_risks": capped_candidate_risks,
            # The TRUE candidate count, even when candidate_risks itself is
            # capped to _MAX_CANDIDATE_RISKS (state/response-size bound). Same
            # disclosure contract as signal_list_total_count
            # (signal_detection_node.py) — capping detail is fine, hiding the
            # fact that capping happened is not.
            "candidate_risks_total_count": total_candidate_count,
            "status": AgentStatus.SUCCESS.value,
        }
        if total_candidate_count > len(capped_candidate_risks):
            result["candidate_risks_truncated"] = True
        return result

    @staticmethod
    def _signal_matches_trigger(metric: str | None, trigger_signals: list[str]) -> bool:
        if not metric:
            return False
        # Match "metric" as a whole token (word boundary) so e.g. "water_temp"
        # does not also match a hypothetical "water_temperature_index" trigger.
        pattern = re.compile(rf"\b{re.escape(str(metric))}\b")
        return any(pattern.search(sig_str) for sig_str in trigger_signals)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
