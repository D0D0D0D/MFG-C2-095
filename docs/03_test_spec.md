# Test Specification — MFG-C2-095 AquacultureEnvironmentalEarlyWarningAgent

## Test Strategy
- Coverage target: 100% of framework-compliance (TC-01〜TC-11) + boundary (PB-1〜PB-6) items, plus domain-specific coverage for the 3 modes (`/ingest` `/invoke` `/feedback`) and the S-3 output gate contract defined in `docs/02_design.md`.
- Test types: Unit (`tests/unit/`) / Integration (`tests/integration/`) / Proof-of-Boundary (`tests/proof_of_boundary/`)
- LLM / external API (JMA weather) / DB (incident store) are all constructor-injection seams — every test uses a fake; no real network or DB call happens in unit/integration tests. Real connectivity must be validated in the target deployment environment (docs/02_design.md §(d)).
- `hitl.enabled: false` for this template (docs/02_design.md §Design Decision Record) — PB-7 (HITL interrupt propagation) is **not applicable** and is intentionally absent.

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | PASS — `tests/proof_of_boundary/test_state_safety.py` (AST scan of `src/schemas/state.py`) |
| TC-02 | Invalid/rejected input surfaces as an error, not a silent pass-through | Error raised via `status=error` | PASS — `tests/unit/test_pre_process_node.py::test_fail_closed_on_missing_farm_id`, `test_rejects_invalid_json`, `test_rejects_unknown_mode`, `test_rejects_unknown_species_code` |
| TC-03 | No JWT/Credential in `src/` | CI `gate-credential-scan`: 0 violations | Verified by the credential-scan CI job (S-5 is CI-only) |
| TC-04 | InvocationContext via `config["configurable"]` only, never stored in State | State never holds an `InvocationContext` instance | PASS — `tests/proof_of_boundary/test_state_safety.py` (`PROHIBITED_TYPE_ANNOTATIONS` includes `InvocationContext`); confirmed by design: all nodes read `state.get(...)` primitives only, no node constructs `InvocationContext` directly (GraphNode uses `InvocationContext.from_state()` internally per SDK contract) |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` body; domain event present | PASS — `tests/proof_of_boundary/test_audit_logging.py` (AST + source scan: every domain `FunctionNode` module imports and calls `emit_trace_event()`; none call the lifecycle-event names directly) |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode` subclass) | `TypeError` at class definition if overridden | PASS — `tests/proof_of_boundary/test_security_gate_non_bypassable.py::test_overriding_security_gate_input_raises_type_error` |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode` subclass) | `TypeError` at class definition if overridden | PASS — `tests/proof_of_boundary/test_security_gate_non_bypassable.py::test_overriding_security_gate_output_raises_type_error` |
| TC-08 | `required_trust_level` declared and enforced — dedicated S-1 boundary coverage MUST call `node(state)` (via `__call__`) for every privileged node, or use equivalent parameterized coverage | Insufficient trust level → execution refused *before* `execute()`; test must prove `execute()` was not called. Direct `node.execute(state)` is permitted only for isolated business-logic unit tests: it does not satisfy TC-08, but is not a finding when TC-08 and PB-6 coverage pass. | PASS — `tests/proof_of_boundary/test_trust_gate.py::test_anonymous_caller_is_denied_by_pre_process`, `test_verified_external_caller_passes_trust_gate`, `test_all_domain_nodes_declare_a_valid_trust_level` + `tests/proof_of_boundary/test_pb_invoke_order.py::test_s1_denial_refuses_execution_before_execute` |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial when domain checks needed | Domain-specific input checks execute correctly | PASS — `PreProcessNode._extra_security_gate_input` masks staff-name PII (docs/02_design.md §セキュリティ設計方針 S-2 — "現場スタッフ氏名程度の PII を除去") in `payload.note_free_text`/`course_narrative` (top-level and `/ingest` `records[]`) before `execute()` runs. JMA forecast response schema validation (untrusted external payload) runs in `execute()` → `_fetch_forecast_data()` since that data does not exist yet at S-2 gate time. `tests/unit/test_pre_process_node.py::test_s2_gate_*`, `test_malformed_jma_forecast_shape_degrades_gracefully` |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial when domain checks needed | Domain-specific output checks execute correctly | PASS — `tests/unit/test_post_process_node.py::test_s3_downgrades_assertive_language`, `test_s3_strips_fabricated_evidence_id`, `test_s3_adds_mandatory_disclaimer_for_critical_severity`, `test_s3_normalization_is_nfkc_and_casefold_bypass_resistant` (docs/02_design.md §(c)) |
| TC-11 | S-4: at least one domain `emit_trace_event()` inside each `execute()` | Domain event emitted on every invocation path | PASS — `tests/proof_of_boundary/test_audit_logging.py::test_every_domain_node_module_calls_emit_trace_event` |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → EventEmitter | `emit_trace_event()` fires on every invocation path | No silent failures | PASS — `tests/proof_of_boundary/test_audit_logging.py` |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | PASS — `tests/proof_of_boundary/test_state_safety.py` |
| PB-3 | L1 → External service (composition boundary) | GraphNode `main` slot connects outer graph to the inner `AquacultureWorkflowGraph`; inner graph connects to the injected `BaseIncidentStore`/`BaseWeatherClient` seams | Data flows end-to-end through the real (in-memory/fake) implementations | PASS — `tests/integration/test_graph.py::test_ingest_then_invoke_then_feedback_full_lifecycle` (ingest writes are read back by invoke/feedback via the shared `InMemoryIncidentStore`); real JMA/DB connectivity is outside this suite and must be validated in the target environment |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | PASS — `tests/proof_of_boundary/test_import_isolation.py` |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpoint | Inspection pass | PASS — `tests/proof_of_boundary/test_state_safety.py` (same State schema is checkpointed) |
| PB-6 | Invoke execution order | `__call__()`: S-1 trust gate → S-4 `node_start` → S-2 `_security_gate_input` → `execute()` → S-3 `_security_gate_output` → S-4 `node_complete` | Order verified | PASS — `tests/proof_of_boundary/test_pb_invoke_order.py` (covers every `FunctionNode` under `src/nodes/` including `src/nodes/inner/`; `GraphNode` — `AquacultureWorkflowGraphNode` — is excluded from this generic scan per the documented rationale in the test file, and is separately covered by `tests/unit/test_main_node.py`) |
| PB-7 | HITL interrupt propagation | N/A | N/A | **Not applicable** — `hitl.enabled: false` (docs/02_design.md §Design Decision Record) |

## Business Logic Tests

| TC-ID | Test | Input | Expected Result | Result |
|-------|------|-------|----------------|--------|
| BL-01 | Tenant fail-closed | `farm_id` empty/missing | `status=error`, no downstream node executes | PASS — `tests/unit/test_pre_process_node.py`, `tests/integration/test_graph.py::test_tenant_fail_closed_on_missing_farm_id` |
| BL-02 | Stage 1 signal detection is deterministic (non-LLM) | Same observation twice | Identical `signal_id`/severity both times | PASS — `tests/unit/test_signal_detection_node.py::test_signal_ids_are_deterministic` |
| BL-03 | Stage 1 range + historical deviation scoring | Value inside/outside species warning/critical ranges; historical series with a sudden deviation | Correct severity tiering (`medium`/`high`/`critical`) | PASS — `tests/unit/test_signal_detection_node.py` |
| BL-04 | Stage 2 matching requires medium+ severity and whole-token metric match | Low-severity signal; substring-only metric match (`water_temp` vs `water_temperature_index`) | No false-positive candidate risk | PASS — `tests/unit/test_signal_matching_node.py::test_low_severity_signal_does_not_match_disease_trigger`, `test_metric_substring_does_not_false_positive` |
| BL-05 | Stage 2 self-farm incidents KB match carries `matched_incident_id` | Seeded incident with matching `environmental_context` | Candidate risk with `matched_incident_id` set, `matched_signal_id` null | PASS — `tests/unit/test_signal_matching_node.py::test_incidents_kb_match_carries_matched_incident_id` |
| BL-06 | Stage 3 LLM unavailable → deterministic fallback, `status` stays `success` | `llm=None` | `status=success`, `degradation_reason=llm_unavailable`, advisory still produced from Stage1/2 data | PASS — `tests/unit/test_advisory_synthesis_node.py::test_llm_none_falls_back_deterministically_with_degradation_reason` |
| BL-07 | Severity is derived deterministically, not from LLM text | LLM returns text claiming "severity: low" while signals are critical | `advisory_output.severity == "critical"` (LLM cannot override) | PASS — `tests/unit/test_advisory_synthesis_node.py::test_severity_derivation_is_deterministic_not_llm_controlled` |
| BL-08 | S-3 assertive-language downgrade | Narrative containing "発生します" | Rewritten to non-assertive ("〜可能性があります") | PASS — `tests/unit/test_post_process_node.py::test_s3_downgrades_assertive_language` |
| BL-09 | S-3 fabricated evidence-ID detection (provenance check) | Narrative citing an ID not in Stage1/2 output | ID stripped/replaced with "[要検証]" | PASS — `tests/unit/test_post_process_node.py::test_s3_strips_fabricated_evidence_id`, `test_s3_normalization_is_nfkc_and_casefold_bypass_resistant` |
| BL-10 | S-3 mandatory disclaimer for critical severity only | `severity=critical` vs `severity=low` | Disclaimer + expert-consultation text present only for critical | PASS — `tests/unit/test_post_process_node.py::test_s3_adds_mandatory_disclaimer_for_critical_severity`, `test_s3_does_not_add_disclaimer_for_low_severity` |
| BL-11 | `/ingest` minimum-data validation (docs/02_design.md §(b)) | Record missing required fields | Row routed to `validation_errors`, not written | PASS — `tests/unit/test_ingest_nodes.py::TestValidateRecordsNode` |
| BL-12 | `/ingest` free-text narrative structuring is LLM-only for extraction (no advisory interpretation) | Free-text incident note | `structure_with_llm` extracts fields only; degrades gracefully (`llm_unavailable`) when `llm=None` | PASS — `tests/unit/test_ingest_nodes.py::TestStructureWithLlmNode` |
| BL-13 | `/feedback` prediction reconciliation | Prior advisory present with matching/non-matching predicted category | `reconciliation` = `hit`/`miss`/`no_prior_advisory` | PASS — `tests/unit/test_feedback_nodes.py::TestReconcilePredictionNode` |
| BL-14 | Species-agnostic scope (docs/02_design.md §(a)) | `/invoke` across fish/crustacean/bivalve/seaweed species codes | All 4 taxon groups process successfully with no code branching on taxon | PASS — `tests/integration/test_graph.py::test_species_agnostic_scope_all_taxon_groups_invoke_successfully` (parametrized: madai/kuruma_ebi/magaki/nori) |
| BL-15 | Graceful degradation on JMA fetch failure | `weather_client` raises `WeatherClientError` | `/invoke` continues with empty `forecast_context`, `status=success`, `degradation_reason=forecast_fetch_failed` | PASS — `tests/unit/test_pre_process_node.py::test_invoke_mode_gracefully_degrades_on_forecast_failure`, `tests/integration/test_graph.py::test_weather_fetch_failure_degrades_gracefully_not_error` |
| BL-16 | `main`/`merge_output()` returns only mode-produced fields (no `None`-padding) | `/invoke` sub_result vs `/ingest` sub_result | `ingest_summary`/`feedback_result` keys absent from `/invoke` delta | PASS — `tests/unit/test_main_node.py::test_merge_output_invoke_mode_omits_ingest_and_feedback_keys` |
| BL-17 | Inner graph mode-dispatch reads `state["mode"]` correctly (regression guard for state projection) | All 3 modes invoked end-to-end | Correct pipeline executes for each mode, no mis-route | PASS — `tests/integration/test_graph.py::test_ingest_then_invoke_then_feedback_full_lifecycle` |

## Test Execution Summary
- Execution date: 2026-07-10
- Total tests: 92
- Pass: 92 / Fail: 0 / Skip: 0
- Command: `pytest tests/ -v` (all suites: `tests/unit/`, `tests/integration/`, `tests/proof_of_boundary/`)
- Coverage: all TC-01〜TC-11 and PB-1〜PB-6 items covered; PB-7 not applicable (no HITL). No stub-only test files (every `test_*` function contains a real assertion).
- Additional coverage includes S-2 staff-name PII redaction and JMA response schema validation in `PreProcessNode._extra_security_gate_input`/`_fetch_forecast_data` (5 tests), `/feedback` `category`/`agent` field persistence in `UpdateIncidentNode._UPDATABLE_FIELDS`, and S-3 case-insensitive assertive-language matching (`re.IGNORECASE`) in `PostProcessNode`.

## Known Limitations
- `_extra_security_gate_input()` on all nodes except `PreProcessNode` remains a documented pass-through — the framework's default S-2 PII scan (on `user_input`/`validated_input`/`llm_response`) is the active input guard for those nodes. `PreProcessNode` additionally implements staff-name PII redaction on free-text payload fields. The tenant fail-closed check lives in `execute()`, not the S-2 hook, since it needs to run after JSON envelope parsing.
- The staff-name PII pattern (`_STAFF_NAME_PII_PATTERN` in `pre_process_node.py`) is a lightweight honorific-suffix heuristic (name + 様/さん/君/氏), not a full Japanese NER pipeline — it will miss names without an honorific suffix. This matches the scope explicitly stated in docs/02_design.md §データモデル概要 ("PII 該当は現場スタッフ氏名程度").
- `InMemoryIncidentStore` (default when `incident_store` is not wired) has no persistence across process restarts — production deployment must inject a durable store via `wire_services()`.
- Real JMA (気象庁) connectivity and a production incident-store backend are outside this test suite; they must be validated in the target deployment environment (per docs/02_design.md §(d)).

---

## 実 LLM 1 回転検証（Live LLM Check）

`config/agent.yaml` は `generation_mode: "llm"` を宣言しているが、これまでのテストは
すべて注入シームに double を刺しており、**実 LLM でプロンプトが出力契約を導けるか**は
未検証だった。本節はその 1 回転検証の記録である。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | Azure OpenAI 上の GPT モデルを `BaseLLM` 実装として注入 |
| 注入シーム | `config["llm"]`（`graph.py` → `AquacultureWorkflowGraphNode` → 内側 graph → `AdvisorySynthesisNode`） |
| 入力 | invoke モード。`tests/conftest.py` の `agent_config` / `case_payload` / `live_input_context`（統合テストと同じ farm-001 / madai / water_temp 31.5 の critical ケース） |
| LLM 呼び出し回数 | 1 回（invoke 経路で LLM を呼ぶのは Stage3 のみ） |
| 結果 | ✅ `status: success` / 縮退なし / **provenance ゲートによる redaction 0 件** |
| 非空だった出力キー | `severity` / `severity_label_ja` / `narrative` / `cited_signal_ids` / `signal_index` / `signal_index_total_count` / `signal_list_total_count` / `candidate_risks_total_count` |

### 検出した欠陥 4 件（すべて修正済み）

本テンプレートは AssetInspectionHistoryAdvisoryAgent と同一の構造・同一の作りであり、**同じ欠陥 4 件をそのまま共有**していた。
機序と修正方針は AssetInspectionHistoryAdvisoryAgent の `docs/03_test_spec.md` と同一である。

#### D1 — 本番では LLM が絶対に配線されない（core path）

`src/api/server.py` の `wire_services()` が **存在しない名前 `AnthropicLLMClient`** を import し
（実クラスは `AnthropicClient`）、さらに**引数なし**で構築していた
（`AnthropicClient.__init__` は `config["api_key"]` 必須）。両方の失敗を
`except Exception: llm = None` が握り潰していたため、`generation_mode: "llm"` を宣言しながら
**本番は毎リクエスト決定論経路**で動作する状態だった。`wire_services()` は CI でも
他のテストでも一度も実行されないため、誰も踏めなかった。

**修正** — 正しいシンボル名 + `secrets_factory(...).require("ANTHROPIC_API_KEY")` で構築。
縮退してよいのは (a) optional extra `[anthropic]` 不在（`ModuleNotFoundError` かつ
`exc.name == "langchain_anthropic"`）、(b) API キー未設定（`MissingSecret`）の 2 条件のみ。
公開シンボルの欠落・コンストラクタ不一致・他モジュールの import 破損は**配線バグとして伝播**させる。
特に (b) を握らないと、キー未設定環境で `MissingSecret` が module import を中断し
`/health` ごと落ちる（「未設定時は fallback し、サービスは停止しない」という運用契約に反する）。


##### 注入シームの取り方について（2026-08-20 の所見）

同系テンプレートで一般的なのは「**`server.py` は具体 LLM クライアントを import も生成もせず、
`config["llm"]` は platform / 配備側が注入する**」形である。

本 repo は `server.py` 側で既定クライアントを構築する形を採っており、そのうえで**クラス名が
実在しなかった**ため一度も成立していなかった。本改版はクラス名（`AnthropicLLMClient` → 実在する
`AnthropicClient`）と例外の握り潰しを直したものであり、注入を配備側へ寄せる設計変更は行っていない。
どちらへ揃えるかは設計判断のため、所見として記録する。

#### D2 — `BaseLLM` 契約違反

3 ノード（`advisory_synthesis` / `structure_with_llm` / `update_incident`）が
`self._llm.complete(prompt_str)` を呼び、戻り値を `str` として使っていた。
実契約は `complete(messages: list) -> dict`。本番が注入する `AnthropicClient` は
引数を `ChatAnthropic.invoke()` へ素通しして **dict を返す**ため、advisory の `narrative` や
`course_narrative` に dict が入るはずだった。

**修正** — `src/services/llm_text.py::complete_text()` に一本化。canonical dict でない・
`content` キーが無い・`content` が非 str・`content` が空、のいずれも例外にして
各呼び出し元の既存縮退経路へ流す。`str()` 強制は行わない。

#### D3 — conftest がハーネスを欠陥側に合わせていた

`tests/conftest.py` が live-check ハーネスの LLM クライアントの `complete` を monkeypatch して
「文字列を受けて文字列を返す」ようにしていた。**ハーネスを AG の欠陥に適合させていた**ため、
実 LLM 1 回転でも D2 を検出できない状態だった。

**修正** — そのパッチを削除。ハーネスのクライアントを無改変で使う。

#### D4 — プロンプトとゲートの粒度不一致で出力が全滅する

`post_process_node._strip_unsupported_claims` は **文単位**（`。！？\n` で分割）で証拠 ID を要求するが、
プロンプトは "Every claim MUST cite an evidence ID" としか言っていなかった。実 LLM は見出し行＋3 文を書き、
引用を各文の `。` の**後ろ**に置いたため、ID は次の断片に属し、**4 断片すべてが**
`[要検証：根拠となる観測シグナル/過去事象IDが確認できません]` に置換された（本文が全滅）。
それでもハーネスはエンベロープのキーが非空なので ✅ を返していた。

**修正** — **ゲートは一切変更していない**。プロンプト側に、各文が ID を持つこと、
`〜の可能性があります[ID]。` のように**閉じ `。` の直前**へ置くこと（`〜あります。[ID]` は不可）、
見出し・導入文・まとめ文を ID なしで書かないことを明記。再実行で redaction 0 件。

#### 併せて修正 — 証拠ゼロで充足不能なプロンプトを送っていた

`signal_list` も `candidate_risks` も空のとき、`valid_ids` が空のまま
「各文を `[]` の中の ID で引用せよ」というプロンプトを送っていた。モデルは ID を捏造するか
引用不能な文を書くかで、いずれもゲートが全文を redact する。証拠ゼロのときは LLM を呼ばず
決定論分岐へ回す。ただし **`llm_unavailable` とは報告しない**（プロバイダは正常であり、
そう報告すると同一 farm で起きた本物の障害を隠すため）。

### 追加・変更したテストと変異検査

| 変異（本番コードを戻す） | 落ちたテスト |
|---|---|
| `complete_text` を `complete(prompt_str)` 直呼びに戻す | 🔴 8 件 |
| `content` の型検査を外し `str()` 強制に戻す | 🔴 `test_non_string_content_is_rejected_not_stringified` |
| 空 `content` の拒否を外す | 🔴 `test_empty_content_is_rejected` |
| `content` キー欠落の拒否を外す | 🔴 `test_missing_content_key_is_rejected` |
| 存在しないシンボル名 `AnthropicLLMClient` に戻す | 🔴 2 件（`test_server_wiring.py`） |
| 引数なしコンストラクタに戻す | 🔴 2 件（同上） |
| `ModuleNotFoundError` の `name` 判定を外す | 🔴 `test_unrelated_missing_module_is_a_wiring_bug_and_propagates` |
| `MissingSecret` 縮退を外す（= import 時クラッシュ） | 🔴 `test_missing_api_key_degrades_to_none_without_crashing` |
| プロンプトの per-sentence 指示を削除 | 🔴 2 件（`TestPromptGateAlignment` 他） |
| プロンプトの配置（`。`の直前）指示を削除 | 🔴 2 件（同上） |
| 証拠ゼロでも LLM を呼ぶ挙動に戻す | 🔴 `test_no_valid_evidence_skips_the_llm_without_claiming_it_is_down` |

`tests/integration/test_graph.py` の `FakeLLM` は**プロンプト従順**かつ**複数文**にした
（配置規則・per-sentence 規則がプロンプトに書かれていなければ、実 LLM が実際にそうしたように
段落末／`。` の後ろに引用を置く）。1 文しか書かない double では「段落末に 1 個だけ引用する」
欠陥を再現できず、プロンプトから指示を消してもテストが緑のままになるため。

### 敵対的コードレビューで追加検出・修正した欠陥

#### D5 — 縮退した事実が呼び出し元に届かない

`AgentBaseGraph.get_output()` は `state["formatted_output"]` / `["result"]` しか `output` に載せず、
`state["degradation_reason"]` は読まない。post_process は縮退理由を `formatted_output` の**外**に
返していたため、**呼び出し元からは常に見えなかった**。気象庁データが取れず LLM も使えなかった実行でも、
利用者には自信ありげなアドバイザリだけが返っていた。

`complete_text()` の導入で不正応答が縮退経路へ流れる頻度が上がるため、本 MR ではこの可視化が必須になる。

**修正** — 縮退理由を `formatted_output` の中へ入れ、`degradation_notice_ja`（日本語の説明文）を併記。

#### D6 — 複数原因の縮退が上書きで 1 つに潰れる

縮退理由を各所が**代入**していたため、後から書いた側が前の理由を消していた。
消失は 2 段階で起きる:

1. `AdvisorySynthesisNode` が `"degradation_reason": "llm_unavailable"` と代入し、
   inner state が既に持っていた理由を潰す。
2. `AssetWorkflowGraphNode.merge_output()` が inner の理由を outer state へ**代入**する。
   outer には pre_process が立てた `forecast_fetch_failed` があるが、inner graph はそれを受け取らないため、
   **両方を持っているのは merge_output だけ**。ここで代入すると outer の理由が消える。

**修正** — `src/services/degradation.py::combine_degradation_reasons()` を新設し、
上記 2 箇所の両方で**累積**する（重複排除・検出順を保持）。
姉妹テンプレート AssetInspectionHistoryAdvisoryAgent は inner 側だけで成立していたが、本テンプレートは
**GraphNode 境界でも合成しないと成立しない**ことを実測で確認した
（`merge_output` を代入に戻す変異で end-to-end テストが赤になる）。

#### D7 — ingest の LLM 出力契約がプロンプトに書かれていない

`_parse_structured_fields()` は**厳密な JSON オブジェクトのみ**を受理するが、プロンプトは
「structured fields を抽出せよ」としか言っていなかった。実モデルは
`onset_date: 2026-05-01, species: madai` のような自然文で答え、`json.loads()` が失敗し、
抽出結果は黙って `{}` になる。さらに監査イベントの `structured_count` は成功件数ではなく
**試行件数**を記録していたため、**全件失敗した ingest と全件成功した ingest が監査上は同一**に見えた。

**修正** — プロンプトに JSON オブジェクト・キー一覧・例を明記（D4 と同じ「ゲートを弱めず契約を伝える」方針）。
`structured_count` は実際の成功件数を数え、1 件でも失敗したら `degradation_reason: "llm_output_invalid"` を返す。

#### D8 — `wire_services()` の try 範囲が広すぎる（レビュー指摘）

`MissingSecret` の握りが `provider.require()` だけでなく `AnthropicClient(...)` の構築まで覆っていた。
将来コンストラクタ内部から `MissingSecret` が飛ぶ配線不良まで縮退扱いになる。
**修正** — `require()` だけを狭い try に入れ、構築は try の外へ出した。
併せて wiring テストで `secrets_factory` の `namespace` / `agent_name`（`mfg` / `mfg-c2-095`）を検証する
（誤った namespace は他エージェントの secret ストアを読むため）。

### 追加の変異検査（レビュー指摘対応分）

| 変異（本番コードを戻す） | 落ちたテスト |
|---|---|
| `update_incident` の LLM 呼び出し**だけ**を直呼びに戻す | 🔴 2 件（レビューが「生き残る変異」として指摘した箇所） |
| 縮退理由の合成を naive な上書きに戻す | 🔴 3 件 |
| `degradation_reason` を `formatted_output` に載せるのをやめる | 🔴 `test_degradation_reason_is_visible_in_the_caller_output` |
| GraphNode 境界（`merge_output`）の合成を上書きに戻す | 🔴 同上 |
| ingest プロンプトから JSON 契約を削除 | 🔴 `test_contract_obeying_model_is_a_clean_success` |
| `structured_count` を試行数に戻し縮退も出さない | 🔴 `test_prose_answering_model_is_reported_as_degraded_not_clean_success` |
| `secrets_factory` の namespace を誤らせる | 🔴 `test_constructs_the_real_client_with_the_provisioned_api_key` |

### 未解決（本 MR の退行ではない・既存の設計課題）

AssetInspectionHistoryAdvisoryAgent と共通の既存課題。詳細は同テンプレートの `docs/03_test_spec.md` を参照。

1. **ingest / feedback 経路の LLM 出力が S-3 を素通りして永続化され得る。**
   framework の S-3 出力ゲートはトップレベルの文字列しか走査せず、`StructureWithLlmNode` は
   LLM テキストを入れ子に置き、`UpdateIncidentNode` は S-3 実行前に DB 副作用を確定する。
2. **provenance ゲートは「同じ断片に有効 ID が 1 つあるか」しか見ない**（その ID がその主張を
   実際に支えるかは検証しない）。文分割も `。！？\n` のみ。
3. **`_build_prompt` が signal_list / candidate_risks / forecast_raw を `repr` のままプロンプトへ入れる**
   （クライアント由来文字列と生 XML を経由した指示注入の余地）。
4. **気象庁の実 feed が判定に使われていない。** `pre_process` は JMA データを保存するだけで、
   `signal_detection_node` は feed を解析せず、クライアント入力の `payload.jma_warning_active` を信用する。
   実 feed に警報があってもシグナルは 0 件、逆にクライアントがフラグを立てるだけで
   `source=jma_bosai_feed` の critical シグナルを作れる。
5. **feedback の入力スキーマが緩い。** `payload=[]` が空 dict 扱いになり、フィールドのない
   インシデントを新規採番して書き込んだうえで `status=success` を返す。
   また post_process の ID 除外キーに単数形 `incident_id` が無く、正規の `inc-farm-...` が
   「検証不能な根拠 ID」としてマスクされる。
6. **`StructureWithLlmNode` の `rec.update(extracted)` が利用者申告値を LLM 抽出値で上書きする**
   （利用者が申告した `onset_date` 等が LLM の推定で置き換わる）。欠損補完のみに限定すべきだが、
   フィールド優先順位はドメイン判断のため本 MR では変更していない。
7. **本番 wiring が非永続の `InMemoryIncidentStore`**（再起動で ingest/feedback が全消失）。
   `write_records_node` の 3 系列書き込みも非トランザクション。
