# MFG-C2-095 — Unit Tests: SignalMatchingNode (Stage 2, search-only)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.signal_matching_node import SignalMatchingNode
from src.services.incident_store import InMemoryIncidentStore


class TestSignalMatchingNode:
    def test_no_signals_no_candidates(self):
        node = SignalMatchingNode(incident_store=InMemoryIncidentStore())
        result = node.execute({"farm_id": "f1", "species_code": "madai", "species_profile": {}, "signal_list": []})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["candidate_risks"] == []

    def test_low_severity_signal_does_not_match_disease_trigger(self):
        species_profile = {
            "disease_triggers": [
                {"category": "disease", "agent": "x", "environmental_signals": ["water_temp>25"], "severity_baseline": "high"}
            ]
        }
        node = SignalMatchingNode(incident_store=InMemoryIncidentStore())
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": species_profile,
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "low"}],
        }
        result = node.execute(state)
        assert result["candidate_risks"] == []

    def test_medium_plus_severity_signal_matches_disease_trigger_with_evidence_id(self):
        species_profile = {
            "disease_triggers": [
                {"category": "disease", "agent": "x", "environmental_signals": ["water_temp>25"], "severity_baseline": "high"}
            ]
        }
        node = SignalMatchingNode(incident_store=InMemoryIncidentStore())
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": species_profile,
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) == 1
        risk = result["candidate_risks"][0]
        assert risk["matched_signal_id"] == "sig-1"
        assert risk["matched_incident_id"] is None

    def test_metric_substring_does_not_false_positive(self):
        """'water_temp' must not match a trigger referencing 'water_temperature_index'."""
        species_profile = {
            "disease_triggers": [
                {"category": "x", "agent": "y", "environmental_signals": ["water_temperature_index_anomaly"], "severity_baseline": "high"}
            ]
        }
        node = SignalMatchingNode(incident_store=InMemoryIncidentStore())
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": species_profile,
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert result["candidate_risks"] == []

    def test_incidents_kb_match_carries_matched_incident_id(self):
        store = InMemoryIncidentStore()
        store.write_incidents(
            "f1",
            [
                {
                    "incident_id": "inc-1",
                    "species": "madai",
                    "category": "disease",
                    "agent": "x",
                    "environmental_context": {"water_temp": 31.0},
                }
            ],
        )
        node = SignalMatchingNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": {},
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) == 1
        assert result["candidate_risks"][0]["matched_incident_id"] == "inc-1"
        assert result["candidate_risks"][0]["matched_signal_id"] is None


class TestSignalMatchingNodeCandidateRiskCap:
    """Regression for the Stage-2 re-amplification incident: an unbounded
    incidents_kb match set (e.g. 3000 similar incidents) re-amplifies Stage
    1's already-capped signal_list into an unbounded candidate_risks list,
    bypassing _MAX_SIGNALS entirely (a 214KB response was measured from a
    single /invoke call). SignalMatchingNode._MAX_CANDIDATE_RISKS + the
    shared apply_severity_cap helper must bound candidate_risks the same
    severity-priority way SignalDetectionNode bounds signal_list.
    """

    @staticmethod
    def _store_with_incidents(count: int, *, farm_id: str = "f1", species_code: str = "madai") -> InMemoryIncidentStore:
        store = InMemoryIncidentStore()
        store.write_incidents(
            farm_id,
            [
                {
                    "species": species_code,
                    "category": "disease",
                    "agent": "x",
                    "environmental_context": {"water_temp": 31.0},
                }
                for _ in range(count)
            ],
        )
        return store

    class _UnboundedStore(InMemoryIncidentStore):
        """A store backend that ignores `limit` (e.g. a real DB adapter that
        forgot to honor it) — exercises SignalMatchingNode's OWN cap as an
        independent layer, not the store-side pre-filter added alongside it.
        """

        def query_similar_incidents(self, farm_id, species_code, signal_list, limit=None):  # noqa: ARG002
            return super().query_similar_incidents(farm_id, species_code, signal_list, limit=10**9)

    def test_candidate_risks_bounded_when_incidents_kb_match_is_large(self):
        """Node-side cap alone (independent of the store-side pre-filter)
        must bound an unbounded store response — the exact 3000-incident/
        214KB-response scenario from the reported vulnerability."""
        store = self._UnboundedStore()
        store.write_incidents(
            "f1",
            [
                {"species": "madai", "category": "disease", "agent": "x", "environmental_context": {"water_temp": 31.0}}
                for _ in range(3000)
            ],
        )
        node = SignalMatchingNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": {},
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) <= 200
        assert result["candidate_risks_total_count"] == 3000
        assert result["candidate_risks_truncated"] is True

    def test_store_side_limit_pre_filters_before_node_cap(self):
        """The store-side `limit` param (defense-in-depth) is honored by
        InMemoryIncidentStore — a compliant store never returns more than
        the node asked for in the first place."""
        store = self._store_with_incidents(3000)
        node = SignalMatchingNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": {},
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) <= 200
        # candidate_risks_total_count reflects what SignalMatchingNode itself
        # observed post-store-cap (200), demonstrating the two caps compose
        # rather than conflict.
        assert result["candidate_risks_total_count"] == 200
        assert "candidate_risks_truncated" not in result

    def test_no_truncation_flag_when_under_cap(self):
        store = self._store_with_incidents(5)
        node = SignalMatchingNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": {},
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) == 5
        assert result["candidate_risks_total_count"] == 5
        assert "candidate_risks_truncated" not in result

    def test_critical_species_profile_candidate_survives_cap_behind_large_incidents_kb_match(self):
        """A large low-value incidents_kb match (no severity_baseline of its
        own) must never push out the one candidate_risks entry that DOES
        carry severity_baseline=critical (i.e. the one AdvisorySynthesisNode.
        _derive_severity actually reads) — mirrors the signal_list cap's
        "a lethal signal must never be silently dropped" invariant.
        """
        store = self._store_with_incidents(500)
        species_profile = {
            "disease_triggers": [
                {
                    "category": "hypoxia",
                    "agent": "dissolved_oxygen_depletion",
                    "environmental_signals": ["dissolved_oxygen"],
                    "severity_baseline": "critical",
                }
            ]
        }
        node = SignalMatchingNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": species_profile,
            # Trigger the disease_triggers match (b) on top of the 500
            # incidents_kb matches (a) — the critical one is appended LAST,
            # after every incidents_kb entry, worst case for a naive
            # first-N cap.
            "signal_list": [{"signal_id": "sig-1", "metric": "dissolved_oxygen", "severity": "critical"}],
        }
        result = node.execute(state)
        assert len(result["candidate_risks"]) <= 200
        critical_present = any(
            r.get("severity_baseline") == "critical" for r in result["candidate_risks"]
        )
        assert critical_present, "the critical species-profile candidate must survive the cap"

    def test_cap_is_deterministic_across_repeated_calls(self):
        store_a = self._store_with_incidents(3000)
        store_b = self._store_with_incidents(3000)
        state = {
            "farm_id": "f1",
            "species_code": "madai",
            "species_profile": {},
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
        }
        result_a = SignalMatchingNode(incident_store=store_a).execute(dict(state))
        result_b = SignalMatchingNode(incident_store=store_b).execute(dict(state))
        assert result_a["candidate_risks"] == result_b["candidate_risks"]
        assert result_a["candidate_risks_total_count"] == result_b["candidate_risks_total_count"]
