# MFG-C2-095 — Unit Tests: /feedback inner nodes (resolve/update/reconcile)

from src.nodes.inner.reconcile_prediction_node import ReconcilePredictionNode
from src.nodes.inner.resolve_incident_node import ResolveIncidentNode
from src.nodes.inner.update_incident_node import UpdateIncidentNode
from src.services.incident_store import InMemoryIncidentStore


class TestResolveIncidentNode:
    def test_mints_new_incident_id_when_none_supplied(self):
        node = ResolveIncidentNode(incident_store=InMemoryIncidentStore())
        result = node.execute({"farm_id": "f1", "payload": {}})
        assert result["resolved_incident_id"].startswith("inc-f1-")

    def test_reuses_existing_incident_id(self):
        store = InMemoryIncidentStore()
        store.write_incidents("f1", [{"incident_id": "inc-existing"}])
        node = ResolveIncidentNode(incident_store=store)
        result = node.execute({"farm_id": "f1", "payload": {"incident_id": "inc-existing"}})
        assert result["resolved_incident_id"] == "inc-existing"


class TestUpdateIncidentNode:
    def test_updates_known_fields_only(self):
        store = InMemoryIncidentStore()
        node = UpdateIncidentNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "resolved_incident_id": "inc-1",
            "payload": {"outcome": "resolved", "unrelated_field": "ignored"},
        }
        result = node.execute(state)
        assert result["updated_incident_fields"] == {"outcome": "resolved"}
        assert "unrelated_field" not in result["updated_incident_fields"]

    def test_free_text_note_without_llm_degrades_gracefully(self):
        store = InMemoryIncidentStore()
        node = UpdateIncidentNode(incident_store=store, llm=None)
        state = {"farm_id": "f1", "resolved_incident_id": "inc-1", "payload": {"note_free_text": "回復した"}}
        result = node.execute(state)
        assert result["status"] == "success"
        assert result["degradation_reason"] == "llm_unavailable"
        assert result["updated_incident_fields"]["course_narrative"] == "回復した"


class TestReconcilePredictionNode:
    def test_no_prior_advisory(self):
        node = ReconcilePredictionNode(incident_store=InMemoryIncidentStore())
        result = node.execute({"farm_id": "f1", "updated_incident_fields": {}, "resolved_incident_id": "inc-1"})
        assert result["feedback_result"]["reconciliation"] == "no_prior_advisory"

    def test_hit_when_predicted_category_matches_actual(self):
        store = InMemoryIncidentStore()
        store.record_advisory("f1", {"predicted_category": "disease", "cited_signal_ids": ["sig-1"]})
        node = ReconcilePredictionNode(incident_store=store)
        state = {"farm_id": "f1", "updated_incident_fields": {"category": "disease"}, "resolved_incident_id": "inc-1"}
        result = node.execute(state)
        assert result["feedback_result"]["reconciliation"] == "hit"

    def test_miss_when_predicted_category_differs(self):
        store = InMemoryIncidentStore()
        store.record_advisory("f1", {"predicted_category": "disease", "cited_signal_ids": []})
        node = ReconcilePredictionNode(incident_store=store)
        state = {"farm_id": "f1", "updated_incident_fields": {"category": "red_tide"}, "resolved_incident_id": "inc-1"}
        result = node.execute(state)
        assert result["feedback_result"]["reconciliation"] == "miss"


class TestUpdateIncidentNodeLlmContract:
    """The feedback path's LLM call must speak the BaseLLM wire contract.

    🔴 Without a test that injects an LLM here, reverting just this one call
    site to `self._llm.complete(prompt_str)` survives the whole suite: the
    other two call sites are covered but this one was not.
    """

    def test_structures_the_free_text_note_through_the_basellm_contract(self):
        seen: list = []

        class _Canonical:
            def complete(self, messages: list, **kwargs) -> dict:
                # A double taking a bare str would let the contract violation
                # back in; assert the wire shape production actually sends.
                assert isinstance(messages, list), "BaseLLM.complete() は messages: list を受け取る"
                assert all(isinstance(m, dict) and "content" in m for m in messages)
                seen.append(messages)
                return {
                    "content": "構造化済みの経過記録",
                    "tool_calls": [],
                    "model": "fake",
                    "usage": {},
                }

        store = InMemoryIncidentStore()
        store.write_incidents("f1", [{"incident_id": "inc-1"}])
        node = UpdateIncidentNode(incident_store=store, llm=_Canonical())
        result = node.execute(
            {
                "farm_id": "f1",
                "resolved_incident_id": "inc-1",
                "payload": {"note_free_text": "赤潮の疑いで斃死が増加"},
            }
        )
        assert seen, "LLM が呼ばれていない"
        assert result["updated_incident_fields"]["course_narrative"] == "構造化済みの経過記録"
        assert result.get("degradation_reason") is None

    def test_malformed_llm_response_keeps_the_raw_note_and_reports_degradation(self):
        class _Malformed:
            def complete(self, messages: list, **kwargs) -> dict:
                # Canonical-looking but contract-violating: content is not a str.
                return {"content": {"unexpected": "object"}, "tool_calls": [], "model": "f", "usage": {}}

        store = InMemoryIncidentStore()
        store.write_incidents("f1", [{"incident_id": "inc-1"}])
        node = UpdateIncidentNode(incident_store=store, llm=_Malformed())
        result = node.execute(
            {
                "farm_id": "f1",
                "resolved_incident_id": "inc-1",
                "payload": {"note_free_text": "赤潮の疑いで斃死が増加"},
            }
        )
        # The operator's own words survive; the stringified object must not.
        assert result["updated_incident_fields"]["course_narrative"] == "赤潮の疑いで斃死が増加"
        assert result["degradation_reason"] == "llm_unavailable"
