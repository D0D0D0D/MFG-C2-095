"""AgentCore Platform v1.0"""

# pre_process — docs/02_design.md Node Configuration §pre_process:
#   - input validation, farm_id resolution (TENANT FAIL-CLOSED)
#   - species_code -> species_profile resolution
#   - /invoke only: JMA forecast + bosai feed fetch (graceful degradation on failure)
#   - S-2 input gate extension: schema-validate untrusted external responses, strip PII
#
# Node contract (sdk/concepts/node-contract.md): execute(state) -> dict,
# return only changed keys, status is an AgentStatus string value.

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.species_profile_service import SpeciesProfileNotFoundError, SpeciesProfileService
from src.services.weather_client import BaseWeatherClient, WeatherClientError

_VALID_MODES = {"ingest", "invoke", "feedback"}

# docs/02_design.md §セキュリティ設計方針 S-2 / §データモデル概要:
# "PII 該当は現場スタッフ氏名程度で S-2 で除去する" — a lightweight honorific-suffix
# pattern (氏名 + 様/さん/君/氏) covers the realistic PII surface in free-text
# farm-log fields without building a full Japanese NER pipeline (out of scope
# for a Cat2 template's input gate).
_STAFF_NAME_PII_PATTERN = re.compile(r"[぀-ヿ一-鿿]{2,4}(?:様|さん|君|氏)")
_FREE_TEXT_PAYLOAD_FIELDS = ("note_free_text", "course_narrative")


class PreProcessNode(FunctionNode):
    """Validate input, resolve tenant (farm_id, fail-closed), select species profile,
    and (for /invoke) fetch JMA forecast/bosai data.
    """

    # /ingest and /feedback have write side effects; /invoke is read-only but
    # this template treats all three modes uniformly at VERIFIED_EXTERNAL
    # (docs/02_design.md §セキュリティ設計方針 S-1).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        species_profile_service: SpeciesProfileService | None = None,
        weather_client: BaseWeatherClient | None = None,
    ) -> None:
        super().__init__()
        # Constructor injection seam — tests pass fakes, server.py wires the
        # real JmaWeatherClient via wire_services().
        self._species_service = species_profile_service or SpeciesProfileService()
        self._weather_client = weather_client

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        payload = self._parse_user_input(user_input)
        if payload is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is not valid JSON envelope"],
            }

        mode = payload.get("mode")
        farm_id = payload.get("farm_id")
        species_code = payload.get("species_code")

        # ── TENANT FAIL-CLOSED (docs/02_design.md §セキュリティ設計方針) ──
        # An empty/missing farm_id must never fall through to downstream
        # nodes — it is the sole tenant isolation boundary for this agent.
        if not farm_id or not isinstance(farm_id, str) or not farm_id.strip():
            emit_trace_event(
                "tenant_fail_closed",
                {"reason": "farm_id missing or empty"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: farm_id resolution failed (fail-closed)"],
            }

        if mode not in _VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unknown mode {mode!r}, expected one of {sorted(_VALID_MODES)}"],
            }

        if not species_code or not isinstance(species_code, str):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: species_code is required"],
            }

        try:
            species_profile = self._species_service.get_profile(species_code)
        except SpeciesProfileNotFoundError as exc:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: {exc}"],
            }

        farm_context = {
            "farm_id": farm_id,
            "ponds": payload.get("ponds", []),
            "area_code": payload.get("area_code", ""),
        }

        result: dict[str, Any] = {
            "validated_input": json.dumps(
                {
                    "mode": mode,
                    "farm_id": farm_id,
                    "species_code": species_code,
                    "payload": payload.get("payload", {}),
                    "farm_context": farm_context,
                    "species_profile": species_profile,
                },
                default=str,
            ),
            # Also set on the OUTER state directly (not just embedded in the
            # validated_input envelope) — post_process reads state["mode"]
            # to select formatting, and the outer GraphNode.merge_output()
            # does not echo mode back from the inner graph (docs/02_design.md
            # §main 契約 — merge_output returns only mode-produced fields).
            "mode": mode,
            "farm_id": farm_id,
            "species_code": species_code,
            "farm_context": farm_context,
            "species_profile": species_profile,
            "status": AgentStatus.SUCCESS.value,
        }

        if mode == "invoke":
            forecast_context, forecast_raw, degradation_reason = self._fetch_forecast_data(
                farm_context.get("area_code", ""), state
            )
            result["forecast_context"] = forecast_context
            result["forecast_raw"] = forecast_raw
            # Re-embed forecast data into the envelope passed to the inner graph.
            envelope = json.loads(result["validated_input"])
            envelope["forecast_context"] = forecast_context
            envelope["forecast_raw"] = forecast_raw
            result["validated_input"] = json.dumps(envelope, default=str)
            if degradation_reason:
                result["degradation_reason"] = degradation_reason

        emit_trace_event(
            "pre_process_validated",
            {"mode": mode, "farm_id": farm_id, "species_code": species_code},
            state,
        )
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_user_input(user_input: str) -> dict[str, Any] | None:
        if not user_input or not user_input.strip():
            return None
        try:
            data = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            return None
        except RecursionError:
            # A client-controlled request body containing an extremely deeply
            # nested array/object (e.g. current_observations[0].observed_at
            # nested ~1000+ levels deep) exhausts the interpreter's recursion
            # limit INSIDE the stdlib json decoder itself. RecursionError is a
            # RuntimeError subclass, NOT a json.JSONDecodeError, so it is not
            # caught by the branch above — left unhandled it propagates
            # through execute() and crashes the whole invocation (remote DoS,
            # and this is the FIRST place raw user_input is parsed, ahead of
            # DispatchNode). Treat it as "not parseable", same as malformed
            # JSON, rather than let the process crash.
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _fetch_forecast_data(
        self, area_code: str, state: dict
    ) -> tuple[dict[str, Any], list[str], str | None]:
        """Fetch JMA forecast + bosai feed. Never raises — on failure, returns
        empty forecast_context and a degradation_reason so /invoke continues
        with self-farm-only signal detection (docs/02_design.md グレースフルデグレード).
        """
        if self._weather_client is None or not area_code:
            return {}, [], "forecast_source_unavailable" if area_code else None

        forecast_raw: list[str] = []
        forecast_summary: dict[str, Any] = {}
        try:
            forecast_json = self._weather_client.fetch_forecast(area_code)
            # S-2 (docs/02_design.md §セキュリティ設計方針): the JMA response is
            # an untrusted external payload — validate its shape before it
            # flows further into Stage 1/3, rather than trusting it blindly.
            if not self._is_valid_jma_forecast_shape(forecast_json):
                emit_trace_event(
                    "forecast_schema_validation_failed", {"area_code": area_code}, state
                )
                return {}, [], "forecast_fetch_failed"
            forecast_raw.append(json.dumps(forecast_json, ensure_ascii=False, default=str))
            forecast_summary["forecast_available"] = True
        except WeatherClientError as exc:
            emit_trace_event("forecast_fetch_degraded", {"error": str(exc)}, state)
            return {}, [], "forecast_fetch_failed"

        try:
            bosai_xml = self._weather_client.fetch_bosai_feed()
            forecast_raw.append(bosai_xml)
            forecast_summary["bosai_feed_available"] = True
        except WeatherClientError as exc:
            emit_trace_event("bosai_fetch_degraded", {"error": str(exc)}, state)
            forecast_summary["bosai_feed_available"] = False

        return forecast_summary, forecast_raw, None

    @staticmethod
    def _is_valid_jma_forecast_shape(data: Any) -> bool:
        """S-2 schema validation for the untrusted JMA forecast response
        (docs/02_design.md §(d): "レスポンスは配列で publishingOffice/
        reportDatetime/timeSeries...を含む"). Deliberately permissive on
        exact field set (JMA is an unofficial/undocumented API — §(d) notes
        "仕様変更リスクあり") but rejects a response shape that would let a
        malformed/malicious payload flow further into Stage 1/3 unchecked.
        """
        if not isinstance(data, list) or not data:
            return False
        first = data[0]
        if not isinstance(first, dict):
            return False
        return "publishingOffice" in first and "timeSeries" in first

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 extension (docs/02_design.md §セキュリティ設計方針, §Framework
        Utilization): strip obvious staff-name PII from free-text fields
        inside the JSON envelope before execute() ever sees it. Must not
        raise — a malformed envelope is left as-is here and rejected later
        by execute()'s own JSON/schema validation (status=error), consistent
        with the hook contract ("must not raise, signal via state only").

        Untrusted external response (JMA forecast/bosai) schema validation
        happens in execute() -> _fetch_forecast_data(), since that payload
        does not exist yet at S-2 gate time (S-2 runs before execute()).
        """
        user_input = state.get("user_input", "")
        if not isinstance(user_input, str) or not user_input:
            return state

        try:
            envelope = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            return state  # malformed JSON — execute() will reject it with status=error
        except RecursionError:
            # This S-2 hook runs BEFORE execute() (framework __call__() order:
            # S-2 gate -> execute()), so it is the FIRST place the raw,
            # untrusted user_input string is parsed — an extremely deeply
            # nested payload trips Python's recursion limit here, inside
            # json.loads itself, before _parse_user_input's own guard (above)
            # even runs. Must not raise (hook contract: "must not raise,
            # signal via state only" — see this method's docstring); leave
            # state untouched and let execute() -> _parse_user_input reject it
            # with status=error via its own (now RecursionError-safe) parse.
            return state

        if not isinstance(envelope, dict):
            return state

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return state

        redacted = False
        for field in _FREE_TEXT_PAYLOAD_FIELDS:
            value = payload.get(field)
            if isinstance(value, str) and _STAFF_NAME_PII_PATTERN.search(value):
                payload[field] = _STAFF_NAME_PII_PATTERN.sub("[MASKED]", value)
                redacted = True

        # Also scan records[] for /ingest (each record may carry the same
        # free-text fields — docs/02_design.md §/ingest).
        records = payload.get("records")
        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                for field in _FREE_TEXT_PAYLOAD_FIELDS:
                    value = rec.get(field)
                    if isinstance(value, str) and _STAFF_NAME_PII_PATTERN.search(value):
                        rec[field] = _STAFF_NAME_PII_PATTERN.sub("[MASKED]", value)
                        redacted = True

        if redacted:
            state = dict(state)
            envelope["payload"] = payload
            state["user_input"] = json.dumps(envelope, default=str, ensure_ascii=False)

        return state
