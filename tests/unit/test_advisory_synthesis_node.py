# MFG-C2-095 — Unit Tests: AdvisorySynthesisNode (Stage 3, the only LLM step)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.advisory_synthesis_node import AdvisorySynthesisNode


class FakeLLM:
    """BaseLLM 契約に従う double: complete(messages: list) -> canonical dict。

    🔴 `.complete(prompt: str) -> str` を返す double にすると、本番が注入する
    `AnthropicClient`（`complete(messages) -> dict`）と形が違うまま全テストが
    緑になり、契約違反が実 LLM 1 回転でしか露見しない。
    """

    def __init__(self, response="LLM narrative citing sig-1"):
        self._response = response

    def complete(self, messages: list, **kwargs) -> dict:
        return {"content": self._response, "tool_calls": [], "model": "fake", "usage": {}}


class FailingLLM:
    def complete(self, messages: list, **kwargs) -> dict:
        raise RuntimeError("llm down")


class TestAdvisorySynthesisNode:
    def test_llm_none_falls_back_deterministically_with_degradation_reason(self):
        node = AdvisorySynthesisNode(llm=None)
        state = {
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
            "candidate_risks": [],
            "forecast_raw": [],
        }
        result = node.execute(state)
        # docs/02_design.md §🔴 status の有効値: graceful degradation stays
        # status=success, never "degraded".
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "llm_unavailable"
        assert result["advisory_output"]["severity"] == "critical"

    def test_llm_call_failure_falls_back_gracefully(self):
        node = AdvisorySynthesisNode(llm=FailingLLM())
        state = {
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "high"}],
            "candidate_risks": [],
            "forecast_raw": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "llm_unavailable"

    def test_llm_success_returns_advisory_with_valid_evidence_ids(self):
        node = AdvisorySynthesisNode(llm=FakeLLM())
        state = {
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
            "candidate_risks": [{"matched_incident_id": "inc-1", "matched_signal_id": None}],
            "forecast_raw": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "degradation_reason" not in result
        assert "sig-1" in result["advisory_output"]["valid_evidence_ids"]
        assert "inc-1" in result["advisory_output"]["valid_evidence_ids"]

    def test_severity_derivation_is_deterministic_not_llm_controlled(self):
        """Severity must come from signal_list/candidate_risks data, never
        from LLM output text (docs/02_design.md §ユーザビリティ設計レビュー)."""
        node = AdvisorySynthesisNode(llm=FakeLLM(response="everything is totally fine, severity: low"))
        state = {
            "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "critical"}],
            "candidate_risks": [],
            "forecast_raw": [],
        }
        result = node.execute(state)
        assert result["advisory_output"]["severity"] == "critical"

    def test_no_signals_no_risks_severity_low(self):
        node = AdvisorySynthesisNode(llm=None)
        result = node.execute({"signal_list": [], "candidate_risks": [], "forecast_raw": []})
        assert result["advisory_output"]["severity"] == "low"


class TestNoValidEvidence:
    def test_no_valid_evidence_skips_the_llm_without_claiming_it_is_down(self):
        """An unsatisfiable prompt must not be sent, and must not be reported
        as a provider outage.

        With no signals and no candidate risks there is no evidence ID to cite,
        so the Stage-3 prompt would order the model to cite every sentence from
        `[]`. The model fabricates or writes uncitable prose and the S-3 gate
        redacts the whole narrative. Misreporting this as `llm_unavailable`
        would also mask a real provider outage on the same farm.
        """

        class _Recording:
            def __init__(self):
                self.calls = 0

            def complete(self, messages: list, **kwargs) -> dict:
                self.calls += 1
                return {"content": "x", "tool_calls": [], "model": "fake", "usage": {}}

        llm = _Recording()
        node = AdvisorySynthesisNode(llm=llm)
        result = node.execute({"signal_list": [], "candidate_risks": [], "forecast_raw": []})
        assert llm.calls == 0, "証拠ゼロで LLM を呼んではならない（充足不能なプロンプト）"
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "llm_unavailable" not in (result.get("degradation_reason") or "")


class TestDegradationReasonCombining:
    """An upstream degradation reason must survive Stage 3 degrading too.

    A naive `"degradation_reason": "llm_unavailable"` erased the fact that the
    JMA fetch had already failed, so an advisory produced with neither weather
    data nor an LLM reported only the LLM problem.
    """

    def test_existing_reason_is_combined_not_overwritten(self):
        node = AdvisorySynthesisNode(llm=FailingLLM())
        result = node.execute(
            {
                "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "high"}],
                "candidate_risks": [],
                "forecast_raw": [],
                "degradation_reason": "forecast_fetch_failed",
            }
        )
        assert "forecast_fetch_failed" in result["degradation_reason"]
        assert "llm_unavailable" in result["degradation_reason"]

    def test_llm_absent_also_combines_with_the_existing_reason(self):
        node = AdvisorySynthesisNode(llm=None)
        result = node.execute(
            {
                "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "high"}],
                "candidate_risks": [],
                "forecast_raw": [],
                "degradation_reason": "forecast_fetch_failed",
            }
        )
        assert "forecast_fetch_failed" in result["degradation_reason"]
        assert "llm_unavailable" in result["degradation_reason"]

    def test_no_existing_reason_yields_the_new_reason_alone(self):
        node = AdvisorySynthesisNode(llm=None)
        result = node.execute(
            {
                "signal_list": [{"signal_id": "sig-1", "metric": "water_temp", "severity": "high"}],
                "candidate_risks": [],
                "forecast_raw": [],
            }
        )
        assert result["degradation_reason"] == "llm_unavailable"
