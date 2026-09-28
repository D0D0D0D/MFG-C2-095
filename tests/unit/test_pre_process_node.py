# MFG-C2-095 — Unit Tests: PreProcessNode

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode
from src.services.weather_client import BaseWeatherClient, WeatherClientError


# Shape mirrors the real JMA forecast API — a list of dicts containing
# publishingOffice/reportDatetime/timeSeries (docs/02_design.md §(d)).
_VALID_JMA_SHAPE = [{"publishingOffice": "test", "reportDatetime": "2026-07-10T00:00:00+09:00", "timeSeries": []}]


class FakeWeatherClient(BaseWeatherClient):
    def __init__(self, forecast=None, bosai="<feed/>", fail=False):
        self._forecast = forecast if forecast is not None else _VALID_JMA_SHAPE
        self._bosai = bosai
        self._fail = fail

    def fetch_forecast(self, area_code: str) -> dict:
        if self._fail:
            raise WeatherClientError("boom")
        return self._forecast

    def fetch_bosai_feed(self) -> str:
        if self._fail:
            raise WeatherClientError("boom")
        return self._bosai


def _base_state(user_input: str) -> dict:
    return {
        "user_input": user_input,
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "test-corr",
        "session_id": "test-session",
    }


class TestPreProcessNode:
    def test_fail_closed_on_missing_farm_id(self):
        node = PreProcessNode()
        payload = json.dumps({"mode": "invoke", "farm_id": "", "species_code": "madai", "payload": {}})
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.ERROR.value
        assert "fail-closed" in result["error_log"][0]

    def test_fail_closed_on_absent_farm_id_key(self):
        node = PreProcessNode()
        payload = json.dumps({"mode": "invoke", "species_code": "madai", "payload": {}})
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.ERROR.value

    def test_rejects_invalid_json(self):
        node = PreProcessNode()
        result = node.execute(_base_state("not json"))
        assert result["status"] == AgentStatus.ERROR.value

    def test_rejects_unknown_mode(self):
        node = PreProcessNode()
        payload = json.dumps({"mode": "bogus", "farm_id": "farm-1", "species_code": "madai", "payload": {}})
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.ERROR.value

    def test_rejects_unknown_species_code(self):
        node = PreProcessNode()
        payload = json.dumps({"mode": "ingest", "farm_id": "farm-1", "species_code": "does_not_exist", "payload": {}})
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.ERROR.value

    def test_success_ingest_mode_no_forecast_fetch(self):
        node = PreProcessNode(weather_client=FakeWeatherClient())
        payload = json.dumps({"mode": "ingest", "farm_id": "farm-1", "species_code": "madai", "payload": {"records": []}})
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["mode"] == "ingest"
        assert result["farm_id"] == "farm-1"
        assert "forecast_context" not in result  # only /invoke fetches forecast

    def test_success_invoke_mode_fetches_forecast(self):
        node = PreProcessNode(weather_client=FakeWeatherClient())
        payload = json.dumps(
            {
                "mode": "invoke",
                "farm_id": "farm-1",
                "species_code": "madai",
                "area_code": "130000",
                "payload": {"current_observations": []},
            }
        )
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["forecast_context"]["forecast_available"] is True
        assert len(result["forecast_raw"]) == 2  # forecast json + bosai xml
        assert "degradation_reason" not in result

    def test_invoke_mode_gracefully_degrades_on_forecast_failure(self):
        node = PreProcessNode(weather_client=FakeWeatherClient(fail=True))
        payload = json.dumps(
            {
                "mode": "invoke",
                "farm_id": "farm-1",
                "species_code": "madai",
                "area_code": "130000",
                "payload": {"current_observations": []},
            }
        )
        result = node.execute(_base_state(payload))
        # Graceful degradation: status stays "success", never "degraded"
        # (docs/02_design.md §🔴 status の有効値 — "degraded" is not a
        # valid AgentStatus value).
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["forecast_context"] == {}
        assert result["degradation_reason"] == "forecast_fetch_failed"

    def test_invoke_mode_without_area_code_skips_forecast_without_error(self):
        node = PreProcessNode(weather_client=FakeWeatherClient())
        payload = json.dumps(
            {"mode": "invoke", "farm_id": "farm-1", "species_code": "madai", "payload": {"current_observations": []}}
        )
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["forecast_context"] == {}

    def test_required_trust_level_is_valid_enum(self):
        assert PreProcessNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )

    def test_malformed_jma_forecast_shape_degrades_gracefully(self):
        """S-2: the JMA response is untrusted — a shape that doesn't match
        the documented contract (list of dicts with publishingOffice/
        timeSeries) must not flow into Stage 1/3 (docs/02_design.md §(d))."""
        node = PreProcessNode(weather_client=FakeWeatherClient(forecast={"unexpected": "shape"}))
        payload = json.dumps(
            {
                "mode": "invoke",
                "farm_id": "farm-1",
                "species_code": "madai",
                "area_code": "130000",
                "payload": {"current_observations": []},
            }
        )
        result = node.execute(_base_state(payload))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["forecast_context"] == {}
        assert result["degradation_reason"] == "forecast_fetch_failed"

    def test_s2_gate_redacts_staff_name_in_free_text_payload(self):
        """S-2 _extra_security_gate_input: staff-name PII in free-text
        payload fields is masked before execute() runs (docs/02_design.md
        §セキュリティ設計方針 S-2 — "現場スタッフ氏名程度の PII を除去")."""
        node = PreProcessNode()
        payload = json.dumps(
            {
                "mode": "feedback",
                "farm_id": "farm-1",
                "species_code": "madai",
                "payload": {"note_free_text": "田中太郎様が対応しました"},
            }
        )
        state = _base_state(payload)
        gated_state = node._extra_security_gate_input(state)
        gated_envelope = json.loads(gated_state["user_input"])
        assert "田中太郎様" not in gated_envelope["payload"]["note_free_text"]
        assert "[MASKED]" in gated_envelope["payload"]["note_free_text"]

    def test_s2_gate_redacts_staff_name_in_ingest_records(self):
        node = PreProcessNode()
        payload = json.dumps(
            {
                "mode": "ingest",
                "farm_id": "farm-1",
                "species_code": "madai",
                "payload": {
                    "records": [
                        {"series": "incidents", "course_narrative": "鈴木一郎さんが確認、水温上昇を記録"}
                    ]
                },
            }
        )
        state = _base_state(payload)
        gated_state = node._extra_security_gate_input(state)
        gated_envelope = json.loads(gated_state["user_input"])
        narrative = gated_envelope["payload"]["records"][0]["course_narrative"]
        assert "鈴木一郎さん" not in narrative
        assert "[MASKED]" in narrative

    def test_s2_gate_leaves_clean_input_unchanged(self):
        node = PreProcessNode()
        payload = json.dumps(
            {"mode": "invoke", "farm_id": "farm-1", "species_code": "madai", "payload": {"current_observations": []}}
        )
        state = _base_state(payload)
        gated_state = node._extra_security_gate_input(state)
        assert gated_state["user_input"] == payload

    def test_s2_gate_does_not_raise_on_malformed_json(self):
        """Hook contract: must not raise — malformed input is left for
        execute()'s own JSON validation to reject via status=error."""
        node = PreProcessNode()
        state = _base_state("not json")
        gated_state = node._extra_security_gate_input(state)
        assert gated_state["user_input"] == "not json"
