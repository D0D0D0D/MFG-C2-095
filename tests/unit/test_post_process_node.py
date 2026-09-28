# MFG-C2-095 — Unit Tests: PostProcessNode (S-3 output gate)

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode


class TestPostProcessNode:
    def test_invoke_mode_formats_advisory_with_severity_heading(self):
        node = PostProcessNode()
        state = {
            "mode": "invoke",
            "advisory_output": {
                "severity": "high",
                "narrative": "水温が上昇しています。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            },
            "signal_list": [{"severity": "high"}],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["severity"] == "high"
        assert "警戒" in result["formatted_output"]["severity_label_ja"]

    def test_ingest_mode_formats_summary(self):
        node = PostProcessNode()
        state = {"mode": "ingest", "ingest_summary": {"env_observations_written": 3}}
        result = node.execute(state)
        assert result["formatted_output"]["ingest_summary"]["env_observations_written"] == 3

    def test_feedback_mode_formats_result(self):
        node = PostProcessNode()
        state = {"mode": "feedback", "feedback_result": {"reconciliation": "hit"}}
        result = node.execute(state)
        assert result["formatted_output"]["feedback_result"]["reconciliation"] == "hit"

    def test_s3_downgrades_assertive_language(self):
        node = PostProcessNode()
        # Cite a valid evidence ID so the per-sentence provenance check (step 3)
        # keeps the sentence — this test isolates the assertive-language
        # downgrade (step 1).
        result = {
            "formatted_output": {
                "severity": "medium",
                "narrative": "病気が発生します（根拠: sig-1）。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "発生します（" not in gated["formatted_output"]["narrative"]
        assert "可能性があります" in gated["formatted_output"]["narrative"]

    def test_s3_strips_fabricated_evidence_id(self):
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "medium",
                "narrative": "根拠: sig-real-1 および sig-fake-999 の可能性があります。",
                "cited_signal_ids": ["sig-real-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "sig-real-1" in narrative
        assert "sig-fake-999" not in narrative
        assert "要検証" in narrative

    def test_s3_adds_mandatory_disclaimer_for_critical_severity(self):
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "critical",
                "narrative": "重大な逸脱の可能性があります。",
                "cited_signal_ids": [],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "専門家" in gated["formatted_output"]["narrative"]
        assert "免責" in gated["formatted_output"]["narrative"]

    def test_s3_does_not_add_disclaimer_for_low_severity(self):
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "low",
                "narrative": "軽微な逸脱の可能性があります。",
                "cited_signal_ids": [],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "免責" not in gated["formatted_output"]["narrative"]

    def test_s3_normalization_is_nfkc_and_casefold_bypass_resistant(self):
        """Fullwidth / mixed-case evidence ID variants must still be treated
        as fabricated if not in the valid set (docs/02_design.md §S-3)."""
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "medium",
                "narrative": "根拠: SIG-FAKE-999 の可能性があります。",
                "cited_signal_ids": ["sig-real-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "SIG-FAKE-999" not in gated["formatted_output"]["narrative"]

    def test_non_advisory_output_passes_through_gate_unchanged(self):
        node = PostProcessNode()
        result = {"formatted_output": {"ingest_summary": {"written": 1}}}
        gated = node._extra_security_gate_output(result)
        assert gated == result

    # ── step 3: per-sentence evidence-ID provenance check (docs/02_design.md §(c)) ──

    def test_s3_strips_claim_without_evidence_id(self):
        """A substantive claim (and a control-action directive) that cites NO
        valid evidence ID must be removed/replaced — the orchestrator's
        reproduction case: '貧酸素水塊による大量斃死のおそれがあります。直ちに
        増酸素装置を稼働してください。' cites nothing and is non-assertive, so
        neither step 1 nor step 2 catches it; step 3 must."""
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "low",
                "narrative": "貧酸素水塊による大量斃死のおそれがあります。直ちに増酸素装置を稼働してください。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "大量斃死のおそれがあります" not in narrative
        assert "稼働してください" not in narrative
        assert "要検証" in narrative

    def test_s3_preserves_claim_with_valid_evidence_id(self):
        """A substantive claim that correctly cites a valid evidence ID must
        pass through step 3 unchanged."""
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "medium",
                "narrative": "貧酸素水塊による斃死のおそれがあります（根拠: sig-1）。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "斃死のおそれがあります" in narrative
        assert "sig-1" in narrative
        assert "要検証" not in narrative

    def test_s3_disclaimer_itself_is_not_stripped(self):
        """The standing disclaimer (which contains claim keywords like 診断/
        推奨 but carries no provenance requirement) must survive step 3, even
        when it is already present in the narrative before the gate runs
        (idempotency)."""
        from src.nodes.post_process_node import _DISCLAIMER_JA

        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "critical",
                "narrative": "水温逸脱の可能性があります（根拠: sig-1）。" + _DISCLAIMER_JA,
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert narrative.count("【免責】") == 1  # not duplicated, not stripped
        assert "専門家への相談を推奨します" in narrative
        assert "水産研究・教育機構" in narrative

    def test_s3_control_action_directive_suppressed_even_with_valid_id(self):
        """docs/02_design.md §(c): '自動対応（増餌停止等の制御アクション）は
        一切実装しない' — a control-action imperative is suppressed even when
        it cites a valid evidence ID."""
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "high",
                "narrative": "直ちに増酸素装置を稼働してください（根拠: sig-1）。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "稼働してください" not in narrative
        assert "要検証" in narrative

    def test_s3_fabricated_id_sentence_then_stripped_by_provenance_check(self):
        """A fabricated citation must not shield the claim it was invented to
        support. Step 2 rewrites sig-fake-999 to [要検証]; step 3 then sees a
        sentence with no VALID citation and must redact the whole claim.

        Exempting any sentence that merely *contains* a marker would invert the
        gate: citing a fake ID would be safer than citing none at all.
        """
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "medium",
                "narrative": "水温逸脱のおそれがあります（根拠: sig-fake-999）。",
                "cited_signal_ids": ["sig-real-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "sig-fake-999" not in narrative
        assert "水温逸脱のおそれがあります" not in narrative
        assert "根拠となる観測シグナル" in narrative

    def test_s3_accepts_evidence_id_adjacent_to_japanese_characters(self):
        """Regression: Python's \\w is Unicode-aware, so a \\b-anchored ID
        pattern does NOT match "水温がsig-1で" — the valid citation would be
        invisible to step 3 and the properly-sourced sentence redacted.

        Evidence IDs are ASCII, so boundaries must be ASCII-anchored.
        """
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "medium",
                # no delimiter/space around the ID — the real-world LLM phrasing
                "narrative": "水温がsig-water_temp-2026-07-10-farm-005で逸脱を示しています。",
                "cited_signal_ids": ["sig-water_temp-2026-07-10-farm-005"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "要検証" not in narrative
        assert "逸脱を示しています" in narrative

    def test_s3_redacts_unsourced_claim_outside_any_keyword_vocabulary(self):
        """Provenance is an allowlist, not a denylist. A claim phrased entirely
        outside any 'risky word' vocabulary still has no citation and must go.
        """
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "low",
                "narrative": "明日、生簀の魚がすべて死にます。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "死にます" not in gated["formatted_output"]["narrative"]

    def test_s3_protected_boilerplate_is_matched_exactly_not_as_substring(self):
        """Prefixing a protected agent line onto an unsourced claim must not
        buy an exemption — otherwise the LLM can ride boilerplate past the gate.
        """
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "low",
                "narrative": "一般的な養殖知識ベースに基づく注意喚起ですが、全滅は確実です。",
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        assert "全滅" not in gated["formatted_output"]["narrative"]

    def test_s3_degraded_mode_fallback_narrative_survives_the_gate(self):
        """The graceful-degradation advisory is agent-authored framing plus
        cited bullet lines. The gate must not empty it out — a degraded
        advisory that says only '[要検証]' would defeat the fallback's purpose.
        """
        node = PostProcessNode()
        result = {
            "formatted_output": {
                "severity": "high",
                "narrative": (
                    "一般的な養殖知識ベースに基づく注意喚起（詳細解釈は現在利用できません）:\n"
                    "- water_temp が high 水準の逸脱を示しています（根拠: sig-1）\n"
                    "現時点で顕著な逸脱シグナルは検出されていません。"
                ),
                "cited_signal_ids": ["sig-1"],
                "cited_incident_ids": [],
            }
        }
        gated = node._extra_security_gate_output(result)
        narrative = gated["formatted_output"]["narrative"]
        assert "要検証" not in narrative
        assert "一般的な養殖知識ベースに基づく注意喚起" in narrative
        assert "現時点で顕著な逸脱シグナルは検出されていません" in narrative

    # ── 2026-07-13 external review #6: recursion DoS / signal_list cap ──────

    def test_sanitize_client_echoes_does_not_raise_recursion_error_on_deep_nesting(self):
        """_sanitize_client_echoes is the LAST layer of defense against a
        RecursionError DoS: even if a non-scalar, deeply nested value somehow
        reaches formatted_output (bypassing SignalDetectionNode's own boundary
        check), the sanitizer must never raise RecursionError — it must
        truncate the walk at _SANITIZE_MAX_DEPTH and return a safe marker.
        """
        from src.nodes.post_process_node import _sanitize_client_echoes

        deep = "x"
        for _ in range(1500):
            deep = [deep]

        node = {
            "data_quality_warnings": {
                "unreadable_observations": [{"observed_at": deep, "reason": "malformed_field_type"}]
            }
        }
        result = _sanitize_client_echoes(node)  # must not raise RecursionError
        import json

        serialized = json.dumps(result, ensure_ascii=False, default=str)
        assert len(serialized) < 10_000, "depth cap failed to bound the sanitized output size"

    def test_extra_security_gate_output_survives_deeply_nested_formatted_output(self):
        """End-to-end: the full S-3 gate (which calls _sanitize_client_echoes
        unconditionally, step 5) must not crash when formatted_output contains
        arbitrarily deep client-controlled nesting anywhere in its tree."""
        node = PostProcessNode()
        deep = "x"
        for _ in range(1500):
            deep = [deep]

        result = {
            "formatted_output": {
                "severity": "low",
                "narrative": "",
                "cited_signal_ids": [],
                "cited_incident_ids": [],
                "data_quality_warnings": {"unreadable_observations": [{"observed_at": deep}]},
            }
        }
        gated = node._extra_security_gate_output(result)  # must not raise
        assert gated["formatted_output"]["severity"] == "low"

    def test_signal_index_selects_highest_severity_not_first_n(self):
        """SAFETY-CRITICAL: when signal_list exceeds the display cap, the
        selected signal_index must still contain the highest-severity signal
        even if it was appended LAST (worst case for a naive first-N slice).
        formatted_output['severity'] alone is not sufficient — an operator
        reading only the detail list must also see the critical entry.
        """
        from src.nodes.post_process_node import _MAX_SIGNAL_INDEX_RECORDED

        node = PostProcessNode()
        medium_signals = [
            {
                "signal_id": f"sig-water_temp-2026_01_{(i % 28) + 1:02d}-f1-p{i}-{'0' * 64}",
                "metric": "water_temp",
                "value": 31.0,
                "observed_at": f"2026-01-{(i % 28) + 1:02d}",
                "pond_id": f"p{i}",
                "deviation": 2.1,
                "severity": "medium",
            }
            for i in range(_MAX_SIGNAL_INDEX_RECORDED + 50)
        ]
        lethal_signal = {
            "signal_id": f"sig-water_temp-2026_12_25-f1-LETHAL-{'1' * 64}",
            "metric": "water_temp",
            "value": 38.0,
            "observed_at": "2026-12-25",
            "pond_id": "LETHAL",
            "deviation": None,
            "severity": "critical",
        }
        signal_list = medium_signals + [lethal_signal]  # appended LAST

        state = {
            "mode": "invoke",
            "advisory_output": {"severity": "critical", "narrative": "", "cited_signal_ids": [], "cited_incident_ids": []},
            "signal_list": signal_list,
        }
        result = node.execute(state)
        formatted = result["formatted_output"]
        assert formatted["severity"] == "critical"
        index = formatted["signal_index"]
        assert len(index) == _MAX_SIGNAL_INDEX_RECORDED
        assert any(s["pond_id"] == "LETHAL" for s in index), (
            "critical signal was dropped from signal_index by a naive first-N slice"
        )

    def test_signal_list_total_count_and_truncated_flag_are_surfaced(self):
        node = PostProcessNode()
        state = {
            "mode": "invoke",
            "advisory_output": {"severity": "low", "narrative": "", "cited_signal_ids": [], "cited_incident_ids": []},
            "signal_list": [],
            "signal_list_total_count": 12345,
            "signal_list_truncated": True,
        }
        result = node.execute(state)
        formatted = result["formatted_output"]
        assert formatted["signal_list_total_count"] == 12345
        assert formatted["signal_list_truncated"] is True
