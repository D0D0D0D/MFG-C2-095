# MFG-C2-095 — Unit Tests: /ingest inner nodes (normalize/structure/validate/write)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.normalize_input_node import NormalizeInputNode
from src.nodes.inner.structure_with_llm_node import StructureWithLlmNode
from src.nodes.inner.validate_records_node import ValidateRecordsNode
from src.nodes.inner.write_records_node import WriteRecordsNode
from src.services.incident_store import InMemoryIncidentStore


class FakeLLM:
    """BaseLLM 契約に従う double: complete(messages: list) -> canonical dict。

    🔴 `.complete(prompt: str) -> str` を返す double にすると、本番が注入する
    `AnthropicClient`（`complete(messages) -> dict`）と形が違うまま全テストが
    緑になり、契約違反が実 LLM 1 回転でしか露見しない。
    """

    def complete(self, messages: list, **kwargs) -> dict:
        assert all(isinstance(m, dict) and "content" in m for m in messages), (
            "BaseLLM.complete() は messages: list[dict] を受け取る契約"
        )
        return {
            "content": '{"onset_date": "2026-05-01", "species": "madai"}',
            "tool_calls": [],
            "model": "fake",
            "usage": {},
        }


class TestNormalizeInputNode:
    def test_unknown_series_dropped(self):
        node = NormalizeInputNode()
        result = node.execute({"payload": {"records": [{"series": "unknown"}]}})
        assert result["normalized_records"] == []

    def test_incident_free_text_routed_to_llm_structuring(self):
        node = NormalizeInputNode()
        result = node.execute(
            {"payload": {"records": [{"series": "incidents", "course_narrative": "free text"}]}}
        )
        assert len(result["records_needing_llm_structuring"]) == 1
        assert result["normalized_records"] == []

    def test_env_observation_passes_through_directly(self):
        node = NormalizeInputNode()
        result = node.execute(
            {"payload": {"records": [{"series": "env_observations", "metric": "water_temp", "value": 20.0}]}}
        )
        assert len(result["normalized_records"]) == 1


class TestStructureWithLlmNode:
    def test_no_pending_records_passthrough(self):
        node = StructureWithLlmNode(llm=None)
        result = node.execute({"records_needing_llm_structuring": [], "normalized_records": [{"a": 1}]})
        assert result["normalized_records"] == [{"a": 1}]
        assert "degradation_reason" not in result

    def test_llm_none_marks_unstructured_with_degradation(self):
        node = StructureWithLlmNode(llm=None)
        result = node.execute(
            {
                "records_needing_llm_structuring": [{"series": "incidents", "course_narrative": "x"}],
                "normalized_records": [],
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "llm_unavailable"
        assert result["normalized_records"][0]["course_narrative_structured"] is False

    def test_llm_structures_pending_records(self):
        node = StructureWithLlmNode(llm=FakeLLM())
        result = node.execute(
            {
                "records_needing_llm_structuring": [{"series": "incidents", "course_narrative": "free text"}],
                "normalized_records": [],
            }
        )
        assert "degradation_reason" not in result
        assert result["normalized_records"][0]["course_narrative_structured"] is True
        assert result["normalized_records"][0]["onset_date"] == "2026-05-01"


class TestValidateRecordsNode:
    def test_missing_required_field_becomes_error_row(self):
        node = ValidateRecordsNode()
        result = node.execute({"normalized_records": [{"series": "inputs"}]})
        assert result["valid_records"] == []
        assert len(result["validation_errors"]) == 1
        assert "type" in result["validation_errors"][0]["missing_fields"]

    def test_complete_record_is_valid(self):
        node = ValidateRecordsNode()
        result = node.execute(
            {
                "normalized_records": [
                    {"series": "incidents", "onset_date": "2026-05-01", "species": "madai", "course_narrative": "x"}
                ]
            }
        )
        assert len(result["valid_records"]) == 1
        assert result["validation_errors"] == []


class TestWriteRecordsNode:
    def test_writes_split_by_series_and_returns_summary(self):
        store = InMemoryIncidentStore()
        node = WriteRecordsNode(incident_store=store)
        state = {
            "farm_id": "f1",
            "valid_records": [
                {"series": "env_observations", "metric": "water_temp", "value": 20.0},
                {"series": "incidents", "onset_date": "2026-05-01", "species": "madai", "course_narrative": "x"},
            ],
            "validation_errors": [],
        }
        result = node.execute(state)
        summary = result["ingest_summary"]
        assert summary["env_observations_written"] == 1
        assert summary["incidents_written"] == 1
        assert len(summary["incident_ids"]) == 1


class TestIngestStructuringContract:
    """The ingest prompt must state the JSON contract the parser enforces.

    🔴 `_parse_structured_fields()` accepts only a strict JSON object, but the
    prompt merely asked to "extract structured fields". A real model answers
    that in prose, extraction silently yields {}, and the ingest reports a
    clean success having structured nothing.
    """

    def test_prose_answering_model_is_reported_as_degraded_not_clean_success(self):
        class _Prose:
            def complete(self, messages: list, **kwargs) -> dict:
                return {
                    "content": "onset_date: 2026-05-01, species: madai",
                    "tool_calls": [],
                    "model": "fake",
                    "usage": {},
                }

        node = StructureWithLlmNode(llm=_Prose())
        result = node.execute(
            {
                "normalized_records": [],
                "records_needing_llm_structuring": [
                    {"incident_id": "inc-1", "note_free_text": "赤潮の疑い"}
                ],
            }
        )
        assert result["normalized_records"][0]["course_narrative_structured"] is False
        assert result["degradation_reason"] == "llm_output_invalid"

    def test_contract_obeying_model_is_a_clean_success(self):
        class _Json:
            _CONTRACT = "Respond with a single JSON object"

            def complete(self, messages: list, **kwargs) -> dict:
                prompt = "\n".join(str(m.get("content", "")) for m in messages)
                # Prompt-obedient: only answers in JSON if the prompt asks for it.
                body = (
                    '{"onset_date": "2026-05-01", "species": "madai"}'
                    if self._CONTRACT in prompt
                    else "onset_date: 2026-05-01, species: madai"
                )
                return {"content": body, "tool_calls": [], "model": "fake", "usage": {}}

        node = StructureWithLlmNode(llm=_Json())
        result = node.execute(
            {
                "normalized_records": [],
                "records_needing_llm_structuring": [
                    {"incident_id": "inc-1", "note_free_text": "赤潮の疑い"}
                ],
            }
        )
        assert result["normalized_records"][0]["course_narrative_structured"] is True
        assert result["normalized_records"][0]["onset_date"] == "2026-05-01"
        assert result.get("degradation_reason") is None
