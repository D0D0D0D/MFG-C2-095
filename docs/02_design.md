# Template Design Specification — MFG-C2-095 AquacultureEnvironmentalEarlyWarning & IncidentHistoryAgent

## Position in AgentCore Architecture

- **Agent Class**: `AquacultureEarlyWarningGraph`
- **L1 Base**: `AgentBaseGraph`（main スロットに `GraphNode` を配置する Cat 2 標準構成）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不使用 — msgpack 非互換のため）
  - Node: L1 継承（Template Method: `execute(self, state: dict) -> dict` のみ override）
  - Graph: composition（`register_nodes()` によるノード差し替え）

## Architecture Overview

固定 5 ノード backbone（`initialize`/`finalize` は framework 自動注入。テンプレは
`pre_process` / `main` / `post_process` の 3 スロットを埋める）。

```
[養殖場管理者/現場責任者]
  --(mode: ingest / invoke / feedback + payload)-->
  pre_process   入力検証・farm_id 解決（テナント fail-closed）・養殖対象種プロファイル選択・S-2 入力ゲート
                （invoke 時は気象庁予報 JSON / 防災情報 XML の取得も担う）
      |
  main (GraphNode → domain_workflow_graph)
      ├ ingest   : 異種フォーマット正規化 → (自由記述は LLM 構造化) → 3 系列 INSERT
      ├ invoke   : Stage1 シグナル検知(決定論) → Stage2 照合(検索) → Stage3 統合解釈(LLM)
      └ feedback : incidents 確定 UPDATE/INSERT + 予兆的中/空振り照合の記録
      |
  post_process  S-3 出力ゲート（断定表現の抑制・免責付与・根拠なし断定の機械的除去）→ output 整形
      |
  finalize
[出力: 予兆アドバイザリ(Markdown/JSON) / 取込結果サマリ / 記録確定応答]
```

### Node Configuration（outer graph）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | セッション初期化・trace_id 発行 | — | — | InitializeNode (default) |
| pre_process | 入力検証・`farm_id` 解決（fail-closed）・養殖対象種プロファイル選択・予報系ソース取得（invoke時）・S-2 入力ゲート | `user_input`（JSON文字列: `mode`, `farm_id`, `payload`） | `validated_input`, `farm_context`, `species_profile`, `forecast_context`, `status` | `PreProcessNode`（`FunctionNode` 継承） |
| main | `GraphNode` — 内側 `domain_workflow_graph` を `mode` で分岐実行 | `validated_input` | `signal_list`, `candidate_risks`, `advisory_output`, `ingest_summary`, `feedback_result`, `status`（mode により実際に埋まるフィールドは異なる。詳細は次項「main（GraphNode）契約」参照） | `AquacultureWorkflowGraphNode`（`GraphNode` 継承） |
| post_process | S-3 出力ゲート・断定抑制・免責付与・**冒頭への severity 明示提示**（§ユーザビリティ設計レビュー 出力面 参照）・output 整形 | `advisory_output` / `ingest_summary` / `feedback_result`（mode 依存）＋ `signal_list`（severity 導出用） | `formatted_output`, `status` | `PostProcessNode`（`FunctionNode` 継承） |
| finalize | 最終出力の確定 | — | — | FinalizeNode (default) |

### `main`（GraphNode）契約 — `AquacultureWorkflowGraphNode`

`src/examples/graph_cat2_sample.py` の `GraphNode` 契約（`get_subgraph()` / `extract_input()` /
`merge_output()` / `error_strategy` / `propagate_hitl`）に対する本テンプレートの設計:

| メソッド / 属性 | 設計方針 |
|---|---|
| `get_subgraph()` | `AquacultureWorkflowGraph`（`src/graph/domain_workflow_graph.py`）を `__init__` でキャッシュして返す（毎回新規生成せず、subgraph 構築コストを避ける） |
| `extract_input()` | `state.get("validated_input", state.get("user_input", ""))` — `pre_process` が正規化した JSON 文字列（`mode`, `farm_id`, `species_code`, `payload`, `farm_context`, `species_profile`, `forecast_context` を含む）をそのまま inner graph へ渡す |
| `merge_output()` | inner graph の `get_output()` が返す `sub_result` から **mode に応じて実際に生成されたフィールドのみ**を outer state へ差分反映する: `/invoke` → `signal_list`, `candidate_risks`, `advisory_output`, `status`（＋データ品質欠陥がある場合のみ `unparseable_observations`, `unparseable_observation_count`）／`/ingest` → `ingest_summary`, `status`／`/feedback` → `feedback_result`, `status`。空表示防止のため、mode に応じて存在しないフィールドのキー自体を返さない（`None` 埋めしない）。**新規 state キーを追加する際は `schemas/state.py`・`get_output()`・`merge_output()` の 3 箇所すべてを通す必要がある**（いずれか 1 つでも欠けると LangGraph の state 射影で黙って捨てられる） |
| `error_strategy` | `"propagate"`（デフォルト）— inner graph のエラーは `SubgraphError` として再送出し outer で `status=error` とする。全滅級リスクを扱うため、grace的に空のアドバイザリを返すより明示的なエラー停止を優先する設計判断（Design Decision Record 参照） |
| `propagate_hitl` | `False`（デフォルト）— 本テンプレートは HITL を採用しないため、inner graph 内で HITL interrupt は発生しない前提。将来 HITL を追加する場合のみ `True` 化を検討する |

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (RETRY, max 3)
                                          pre_process
```

`route()` は framework 標準（`state["status"]` を読み `success`→post_process / `retry`→pre_process /
他→finalize）。`add_edges()` / `route()` は override しない。

## 予兆判定の 3 段パイプライン（`/invoke` 内側 `domain_workflow_graph`）

> 「予兆に気づくのは決定論、意味と対応を説明するのが LLM」— 完全 LLM 任せへの明確な否定。
> Stage 1/2 を Tool 相当の決定論・検索処理として切り出し、Agent としての価値の核（LLM 必須性）を
> Stage 3 の統合解釈 1 点に集約する。

| Stage | 処理内容 | 実装方式 | 入力 | 出力 |
|---|---|---|---|---|
| **Stage 1: シグナル検知** | ①複数年同時期統計との乖離検出（同週過去平均±σ・上昇速度）／②養殖対象種プロファイルのアラーム閾値照合（§(a) 🔴 スカラ閾値スキーマ）／③公的警報（気象庁予報系フィード）の受信 | **決定論・非 LLM**（同一入力→同一出力・単体テスト可能） | 直近〜当日の `env_observations`/`inputs` + 予報系フィード + 養殖対象種プロファイル | 構造化「逸脱シグナルリスト」（各要素は `signal_id`（`sig-{metric}-{observed_at}-{farm_id}[-{pond_id}]` の決定論的生成。**`pond_id` は同一日・同一メトリクスの複数養殖区画を識別するために必須**——省略すると Stage 3 が同一 ID を引用する見分けのつかない助言文を複数出力する。各構成要素は `[0-9A-Za-z_-]` に**サニタイズ**する。§(a) 🔴 evidence-ID 文法を参照）, `metric`, `deviation`, `severity` を持つ配列） |
| **Stage 2: 照合** | ①自場 `incidents` KB の類似条件検索（環境条件類似度による近傍探索）／②養殖対象種プロファイルの病害トリガー引き当て | **検索**（LLM 推論を介さない） | Stage 1 の逸脱シグナルリスト（`signal_id` 込み） + 自場 `incidents` + 養殖対象種プロファイル | 「候補リスク＋根拠レコード ID」のリスト（各要素は `matched_incident_id`（`incidents.incident_id`）または `matched_signal_id`（Stage1 の `signal_id`）のいずれかを根拠として保持） |
| **Stage 3: 統合解釈** | シグナル×過去事象×知識×予報を統合し、言語化・優先度付け・対応策提示 | **ここだけ LLM** | Stage 1 の逸脱シグナルリスト（`signal_id` 込み） + Stage 2 の候補リスク＋根拠 ID + `forecast_raw`（予報系フィードの未加工ペイロード） | 非断定の予兆アドバイザリ（自然言語＋`signal_id`/`incident_id` 引用） |

**Stage 3 の制約（S-3 出力ゲートで機械的に強制）**:
- LLM は Stage 1/2 が提示した材料にないリスクを追加できない（プロンプトで「根拠 ID 必須・提示ソースのみ
  参照」を制約）。
- 全言明に根拠 ID（Stage 1 の `signal_id` or Stage 2 の `matched_incident_id`）の引用を必須とする。
  各根拠 ID は Stage 1/2 の出力に実在する ID のみが許可され、S-3 が Stage1/2 の出力リストと突合して
  検証する（存在しない ID を LLM が捏造した場合も検出対象）。
- S-3 出力ゲート（`_extra_security_gate_output()`）で根拠なし断定・存在しない根拠 ID 引用を機械的に
  検出し、該当文を除去または「要検証」に置換する。
- Stage 3 への入力は `forecast_context`（`pre_process` が生成した要約 JSON。人間可読なサマリ用途）ではなく
  **`forecast_raw`（気象庁 JSON/XML の未加工ペイロード。§State Definition 参照）** を用いる。要約では
  引用の裏取りができないため、S-3 の provenance 検証は raw ペイロードに対して行う。

## `/ingest` `/feedback` の内部ノード構成（`domain_workflow_graph` 内トポロジ）

`/invoke` の 3 段パイプライン（Stage1〜3）に加え、inner graph（`AquacultureWorkflowGraph`）は
`mode` 分岐により以下の 2 系統も実行する。いずれも BaseGraph 継承・カスタムトポロジ
（`src/examples/domain_workflow_graph_sample.py` 相当）とし、mode ごとに実行するノード集合を切り替える。

### `/ingest`（過去記録の取込・正規化）

```
START → normalize_input → (free-text は structure_with_llm) → validate_records → write_records → END
```

| ノード | 責務 | LLM 使用 |
|---|---|---|
| `normalize_input` | 異種フォーマット（CSV/Excel由来JSON/自由記述テキスト）を共通スキーマ候補へ変換 | 不使用（決定論の列マッピング） |
| `structure_with_llm` | 自由記述の事象メモ（`course_narrative` 相当）のみを対象に、構造化フィールド（発生日・対象種・被害規模等）を抽出 | **使用**（構造化抽出のみ。予兆解釈は行わない） |
| `validate_records` | 必須フィールド充足チェック（§(b) 最小データ要件）。不足行はエラーリストへ | 不使用 |
| `write_records` | `env_observations` / `inputs` / `incidents` へ INSERT。書込件数・エラー行を `ingest_summary` へ集約 | 不使用 |

### `/feedback`（実発生結果の記録）

```
START → resolve_incident → update_incident → reconcile_prediction → END
```

| ノード | 責務 | LLM 使用 |
|---|---|---|
| `resolve_incident` | 対象 `incident_id`（新規なら採番）を解決 | 不使用 |
| `update_incident` | `course_narrative` / `outcome` / `actions_taken` / `status` を確定 UPDATE または新規 INSERT | 不使用（構造化入力を前提。自由記述メモがある場合は `/ingest` と同じ `structure_with_llm` 相当ステップを再利用） |
| `reconcile_prediction` | 直近の `/invoke` `advisory_output`（あれば）と実際の発生結果を突合し、的中/空振りを `feedback_result` に記録。次回 Stage 2 照合の精度向上シグナルとして扱う | 不使用（決定論の突合ロジック） |

`/ingest` `/feedback` はいずれも書込副作用を持つため、S-1 trust level は `VERIFIED_EXTERNAL` 以上を
要求する（§セキュリティ設計方針 参照）。

### 🔴 inner graph の state 型注釈方針（実装契約・必須）

inner graph（`AquacultureWorkflowGraph`）は `mode`（`ingest`/`invoke`/`feedback`）による条件分岐を行う。
このとき **分岐条件関数および `get_output()` の `state` 引数の型注釈** について、以下を実装契約
として拘束的に定める。

- inner graph は **自身の state スキーマ `AquacultureWorkflowState`** を持つ。**この型名は本 Design
  ドキュメントで確定とし、改名せずこの名前で実装する。** `src/graph/domain_workflow_graph.py` に、
  outer の `State`（§State Definition のフィールド契約）と同一のカスタムフィールド
  （`mode` / `farm_id` / `species_code` / `signal_list` 等）を含む形で定義する。
- **`mode` 分岐の条件関数（`add_conditional_edges()` が参照する route 関数）および `get_output()` は、
  `AgentState` ではなく `AquacultureWorkflowState` で `state` を型注釈しなければならない。**
- 理由: LangGraph の内側グラフで route 関数の `state` 引数を `AgentState`（基底スキーマ）で注釈すると、
  **状態射影によってカスタムフィールド（本件では `mode` 等）が脱落し、`mode` を読めずに誤 route する**。
- この制約は本 Design ドキュメントを正本とし、実装とテストで検証する。

## State Definition（`src/schemas/state.py` — `AgentState` 継承 flat TypedDict）

> 下表は `src/schemas/state.py` の `State(AgentState)` に反映するフィールド契約であり、本 Design
> ドキュメントが正本となる。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `"ingest"` / `"invoke"` / `"feedback"` の 3 値 | Yes |
| `farm_id` | `str` | テナント識別子（fail-closed の起点） | Yes |
| `species_code` | `str` | 養殖対象種プロファイル参照キー。**魚類に限定せず甲殻類・二枚貝・海藻類を包含する**（例: `"madai"`, `"kuruma_ebi"`, `"magaki"`, `"nori"`） | Yes |
| `farm_context` | `dict` | `pre_process` が解決した farm メタ情報（pond一覧等） | No |
| `species_profile` | `dict` | 選択された養殖対象種プロファイル（アラーム閾値・病害トリガー知識・対象種固有の投入/処置種別） | No |
| `forecast_context` | `dict` | `/invoke` 時に取得した気象庁予報・防災情報の**要約**（人間可読サマリ用途。JSON） | No |
| `forecast_raw` | `list[str]` | `/invoke` 時に取得した気象庁予報 JSON / 防災情報 XML の**未加工ペイロード**（文字列化して格納。Stage 3 の引用・S-3 の provenance 検証に使用） | No |
| `signal_list` | `list[dict]` | Stage 1 が生成した逸脱シグナルリスト（各要素に `signal_id` を含む） | No |
| `candidate_risks` | `list[dict]` | Stage 2 が生成した候補リスク＋根拠 ID リスト（`matched_incident_id` / `matched_signal_id`） | No |
| `advisory_output` | `dict` | Stage 3 が生成した非断定アドバイザリ（`signal_id`/`incident_id` 引用込み） | No |
| `ingest_summary` | `dict` | `/ingest` の取込結果サマリ（件数・エラー行・正規化後レコード ID 等） | No |
| `feedback_result` | `dict` | `/feedback` の確定結果（更新された `incident_id`・的中/空振り照合結果） | No |
| `status` | `str` | ノードが返す実行状態。**`AgentStatus` の小文字文字列値のみ**（下記 🔴 参照） | Yes |
| `degradation_reason` | `str` | 縮退動作が起きた理由（`"llm_unavailable"` / `"insufficient_incident_history"` 等）。正常時は未設定 | No |

### 🔴 `status` の有効値（実装契約・必須）

`status` に代入してよいのは framework の `AgentStatus` enum が定義する値**のみ**であり、
本テンプレートで使用するのは次の 3 値に限る。

| 値 | 用途 | `AgentBaseGraph.route()` の遷移先 |
|---|---|---|
| `"success"` | 正常完了（**縮退動作を含む** — 下記参照） | `post_process` |
| `"retry"` | 一時障害。`pre_process` からやり直す価値がある場合のみ | `pre_process`（`max_retry` まで） |
| `"error"` | 恒久障害・fail-closed（`farm_id` 解決失敗等）。リトライしない | `finalize` |

- **`"degraded"` は `AgentStatus` に存在しない。絶対に `status` に代入してはならない。**
  `route()` は `AgentStatus(state["status"])` で値を解決するため、enum 外の文字列は
  `UnknownStatusError` を送出し、パイプラインが実行時に停止する。
- **縮退動作（グレースフルデグレード）は `status="success"` のまま**、別フィールド
  `degradation_reason` に理由を格納し、`post_process` が出力の冒頭に縮退した旨を明示することで表現する。
  縮退は「価値を落として処理を継続する」正常系であり、エラー系ではないため。
- `AgentStatus` の残る 4 値（`pending` / `timeout` / `awaiting_human` / `cancelled`）は本テンプレートでは
  使用しない（`pending` は framework が初期値として設定するもので、ノードから返してはならない。
  `awaiting_human` は HITL 用途で本テンプレートは `hitl.enabled: false`）。

**State Constraints（必須）**:
- Flat TypedDict のみ（primitives + JSON シリアライズ可能な型）
- JWT / API キー / credential を State に置かない（checkpoint DB 漏洩を防ぐ）
- `InvocationContext` は `config["configurable"]` 経由のみ（State に含めない）
- Pydantic モデル・dataclass・任意 Python オブジェクトは禁止（msgpack 非互換）
- センシティブ値（`incidents` の個別詳細等）はグラフ内ノード戻り値に直接載せない。API アダプタ層でのみ
  参照 ID から解決する（checkpoint 永続化への漏洩を防ぐ設計判断）
- 上表の `Type` 列は `src/schemas/state.py` の TypedDict 型注釈と一対一で対応させる。
  「上表の全フィールドが宣言済みか」「msgpack 非互換型が混入していないか」を PB-2（State serialization）
  テストで検証する

## データモデル概要（エージェントがスキーマを設計・所有）

共通スキーマ 3 系列 + 養殖対象種プロファイル。対象種差は**スキーマではなく養殖対象種プロファイル（config/KB）**で吸収する（対象種は魚類に限定せず甲殻類・二枚貝・海藻類を包含する。§(a) 参照）。

```
env_observations   環境観測時系列
  farm_id, pond_id, observed_at, metric(water_temp/air_temp/salinity/DO/pH/turbidity/...), value, unit, source(sensor/manual)

inputs             投入・処置時系列
  farm_id, pond_id, occurred_at, type(辞書参照。魚類例=feeding/seeding/vaccination/medication/water_exchange、
                                       二枚貝例=thinning/net_cleaning、海藻類例=harvest/acid_treatment/... 対象種プロファイルで定義), species, amount, note

incidents          事象レコード（病気・災害の発生経過ナラティブ）★moat の核
  incident_id, farm_id, pond_id, onset_date, category(disease/red_tide/hypoxia/typhoon/heat/cold/...),
  species, agent(病原体/災害種), severity, affected_count/mortality,
  environmental_context(発生前後の env 抜粋), course_narrative(構造化経過記述), actions_taken, outcome,
  confirmed_by(human), status(suspected/confirmed/resolved)
```

- `metric` / `category` / `agent` / `inputs.type` は enum ではなく**辞書参照**とし、
  `config/species/{species_code}.yaml` 養殖対象種プロファイルで有効値・アラーム閾値・病害トリガー知識・
  対象種固有の投入/処置種別を定義する。新規対象種への対応 = プロファイル追加のみ（コード変更なし・
  魚類に限定しない）。
- `incidents.course_narrative` は「発生事象の経過を構造化ナラティブとして記録・蓄積する」概念の安全な
  転用。**患者 PHI は一切扱わない**（養殖場・養殖区画・養殖対象種・環境条件という事業/生物データのみ）。
  PII 該当は現場スタッフ氏名程度で S-2 で除去する。

## 🔴 Design 段階チェックリスト（着手前に確定した事項）

### (a) 養殖対象種プロファイルの整備方針（対象種は非依存・魚類に限定しない）

**本エージェントのスキーマ（`env_observations` / `inputs` / `incidents`）および 3 段パイプラインは対象種に
非依存である。** `metric` / `category` / `agent` / `inputs.type` はいずれも enum ではなく**辞書参照**で
あり、有効値は `config/species/{species_code}.yaml`（養殖対象種プロファイル）側で定義される。「対象が
魚類である」ことをスキーマ・パイプラインのどこにも仮定していない。

- **対象種差は完全に config/KB へ分離**されており、**新規対象種の追加はコード変更を伴わない**（プロファイル
  YAML を 1 本追加するだけ）。これが本テンプレの中核主張であり、ready-made カタログ価値の源泉である。
- **対象範囲は魚類に限定しない。** 甲殻類・二枚貝・海藻類を含む。赤潮・貧酸素水塊による被害はむしろ
  二枚貝・海藻類でこそ深刻であり、本エージェントの災害トリガー（水温・DO・台風進路等の環境シグナル）は対象種を
  問わずそのまま適用できる。`inputs.type` の投入・処置種別も、魚類の給餌・種苗投入・薬浴だけでなく、
  二枚貝の間引き・網掃除、海藻類の収穫・酸処理といった対象種固有の作業を**プロファイル側の辞書定義で
  吸収**する（コード側に対象種前提を持たない）。
- **初期整備する対象種は実装検証用の例示であり、実際の対象種は導入先の養殖品目に合わせる。** 選定時は
  魚類に限定しない。プロファイルは公開文献（水産庁・水産研究・教育機構・都道府県水産試験場の病害・
  災害情報）ベースで整備し、`config/species/{species_code}.yaml` として投入する。
  各プロファイルの主な内容: 監視対象メトリクスとアラーム閾値（下記 🔴 参照）、想定される病害・災害リストと環境
  トリガー知識、対象種固有の投入・処置種別。

#### 🔴 メトリクスのアラーム閾値スキーマ（実装契約・必須）

Stage 1 の範囲照合は、**OPC UA `LimitAlarmType` 準拠のスカラ閾値**（OPC 10000-9 §5.8.11）で表現する。
各メトリクスは以下 4 つの閾値を**任意**に宣言し、順序制約 `low_low_limit ≤ low_limit ≤ high_limit ≤ high_high_limit`
を満たす（最低 1 つは宣言する）。

| 条件 | `severity` |
|---|---|
| `value < low_low_limit` または `value > high_high_limit` | `critical` |
| `value < low_limit` または `value > high_limit` | `medium` |
| 上記いずれにも該当しない | シグナルなし |

- **片側のみの宣言＝その側に危険域がない**という意味。`turbidity` は `high_*` のみ、`dissolved_oxygen` は
  `low_*` のみ、`water_temp` は 4 つすべてを宣言する。
- `normal_range` は**説明・表示専用**であり、判定には使用しない。
- **区間（`normal_range`/`warning_range`/`critical_range`）で表現してはならない。** 区間表現は
  「`critical_range` の外側の値」という判定不能な状態を生み、実装がこれを `None`（シグナルなし）で
  扱うと、マダイ 31℃ が `critical`・**34℃/38℃（致死域）がシグナルなし**という重大度逆転を
  招く。スカラ閾値では「critical の外側」という概念自体が存在しない。
- 比較は**厳密不等号**（`<` / `>`）。閾値ちょうどの値はシグナルとしない（閾値は危険域の境界であって
  危険域の内側ではない）。
#### 🔴 evidence-ID 文法（実装契約・必須）

`signal_id` は単なるキーではない。**Stage 3 が根拠として引用し、S-3 出力ゲートが
ASCII アンカーの正規表現（`post_process_node._EVIDENCE_ID_PATTERN`）で再抽出して照合する。**

- ID の各構成要素（`metric` / `observed_at` / `farm_id` / `pond_id` — いずれもクライアント入力）は
  `[0-9A-Za-z_]` 以外の文字を `_` に置換した上で、成分あたり `_ID_COMPONENT_MAX_CHARS`
  （16 文字）に切り詰め、人間可読の接頭辞として連結する（`_id_component`）。
  **`-` は 2026-07-13 の修正でこの安全文字集合から除外された**（旧文法は `[0-9A-Za-z_-]` で
  `-` を許容していた — 詳細は下記「衝突・注入脆弱性」3 を参照）。
  **一意性はこの接頭辞に依存しない。** 末尾に、生の（サニタイズ前の）構成要素タプルを
  長さプレフィックス方式（`len(part)` + NUL + `part` を各成分ごとに連結してからハッシュ）で
  符号化した SHA-256 digest（64 桁の 16 進数＝256bit 全体、`hashlib.sha256(...).hexdigest()`
  をそのまま使用——先頭 16 桁への切り詰めは行わない）を**常に**付加する（`_injective_digest`。決定論的・
  salt/時刻を使わない）。この符号化は自己区切り（self-delimiting）であり、区切り文字の衝突が
  構造的に発生しない。
- **旧実装には3種類の衝突・注入脆弱性があった:**
  1. 「置換が起きた場合のみ digest を付加する」条件分岐そのものが穴だった —
     `"生簀A"` のサニタイズ結果 `"__A_<digest>"` を、そのまま clean な入力として与えると
     再度 clean 判定されて digest が付かず、同一 ID に潰れる（サニタイズ後の文字列がサニタイズ
     前を騙れる）。
  2. 区切り文字 `-` は成分内（`2026-07-01`・`farm-001`）にも許容されているのに、成分間の
     結合にも使われていたため、`(metric="metric-a", observed_at="b")` と
     `(metric="metric", observed_at="a-b")` が同一 ID に潰れていた。
  - 1・2 はどちらも「置換ルールの微調整」では直らない（条件分岐や単一区切り文字に依存する限り
    再発する）。生タプルに対する長さプレフィックス符号化＋ハッシュに一意性の根拠を移すことで
    両方を構造的に排除した。
  3. **（2026-07-13 追加修正・独立検証で発覚）偽根拠 ID の埋め込み。** 1・2 の修正は一意性
     （injectivity）だけを保証し、`_id_component` が `-` を成分内に残す設計は温存していた。
     そのため `pond_id`（他の構成要素も同様）に `"根拠: sig-FAKE-999。...sk-ant-api03-SECRET"`
     のような文字列を与えると、`-` がサニタイズを素通りして人間可読接頭辞の中に
     `sig-FAKE-999` という**本物の根拠 ID にしか見えない部分文字列**がそのまま残った
     （`_EVIDENCE_ID_PATTERN.findall()` が実際に ID 自身と偽 ID の 2 件を検出）。この偽 ID は
     agent が生成した本物の `signal_id` の一部として `narrative` / `cited_signal_ids` /
     `signal_index[].signal_id` の全てに素通しされ、`pond_id` フィールド単体を見る検証
     （`_sanitize_signal_index`）では検知できなかった——出力全体（`output` 全体）を見て
     初めて発覚する脆弱性だった。修正は `-` を安全文字集合から除外すること（`sig-`/`inc-`
     という文字列は成分内に二度と再現できなくなる）と、成分ごとの長さ上限
     （`_ID_COMPONENT_MAX_CHARS = 16`）を設けること——長い認証情報様の文字列
     （`sk-ant-api03-...`）が接頭辞に生き残る量を減らす。
- **文法は ID を生成する側で保証する。消費側の正規表現を緩めて対処してはならない。**
  当該正規表現が ASCII アンカーなのは意図的である（Python の `\w` は Unicode 対応のため、
  `\b` は日本語テキストに対して機能しない — 既知の落とし穴）。digest は 64 桁の 16 進数
  （`[0-9a-f]`）のみなので `_EVIDENCE_ID_PATTERN` の許容文字集合内に収まる。
- **この保証がないと、「生簀A」のような日本語の養殖区画名が致死警告そのものを消す。**
  引用 ID が照合に失敗し、S-3 ゲートが「根拠なし」と判断して文ごと `[要検証]` に置換するため。
- `signal_index` のクライアント制御フィールド（`pond_id`/`metric`/`observed_at`）は
  `_sanitize_signal_index`（S-3 拡張フック）が別途、根拠 ID もどき・制御指示・認証情報様
  文字列（共有 `shared.security.credential_detector.detect_credentials` ＋
  Anthropic 鍵形状用のローカル補完パターン）をマスクする。この処理は `signal_id` 自体には
  一切触れない——`signal_id` の安全性は生成時点（`_id_component`）で保証する設計であり、
  この点は変わらない。
- サニタイズにより ID は人間には読めなくなるため、`output.signal_index` に
  `signal_id → {pond_id, metric, observed_at, value, severity}` の対応表を構造化データとして返す。

#### 🔴 State サイズ境界（実装契約・必須）

`unparseable_observations` の各フィールドはクライアント入力であり、checkpoint DB に永続化される。
1 フィールド 200 文字・記録件数 50 件で切り詰める。**ただし件数（`unparseable_observation_count`）は
決して切り詰めない**——件数を黙って打ち切ることは、本節が防いでいる握り潰しの再発そのものである。

- **Stage 1 が評価できなかった観測は、例外なくすべて `unparseable_observations` に記録する。**
  `execute()` 内のいかなる `continue` も、記録なしに行ってはならない。記録される事由:
  `missing_metric` / `missing_value` / `missing_observed_at` / `unreadable_value` /
  `metric_not_in_species_profile`。
  - `metric_not_in_species_profile`（プロファイル未定義・メトリクス名のタイポ）は**記録した上で
    履歴乖離チェックは継続する**（自農場の記録から異常と判定できる場合があるため）。範囲照合が
    できないだけで「安全」を意味しない。
- 観測値は境界で**有限な** `float` に強制変換する（`"38.0"` のような文字列も数値として解釈する）。
  数値として解釈できない観測は **`unparseable_observations` に記録して運用者に提示**し、決して
  黙って読み飛ばさない（読めない観測は「リスク不在」ではなく**データ品質の欠陥**である）。
  運用者へは `output.data_quality_warnings` として**構造化データで**提示する（narrative に混ぜない。
  根拠 ID を持たない文は S-3 ゲートが除去するため）。
  - `bool` は `int` の派生型のため明示的に拒否する（`True` が観測値 1.0 として採点されるのを防ぐ）。
  - **非有限値（`NaN` / `inf`）は拒否する。** `float("nan")` は変換に成功してしまい、NaN は
    **すべての閾値との比較が False** になるため、「シグナルなし」と採点されながら
    `unparseable_observations` にも残らない＝同じ握り潰しが再発する。`json.loads` は裸の
    `NaN` / `Infinity` リテラルを受理するため実ペイロードから到達可能。`inf` も測定値ではなく
    センサ故障であり、`critical` シグナルとして採点してはならない。
  - 蓄積履歴側も同様に非有限値を除外する。履歴に `NaN` が 1 件混入すると平均が NaN になり、
    以降そのメトリクスの乖離検出が恒久的に機能しなくなる（`pstdev` は不可解な `AttributeError`
    を送出する）。
- プロファイルの閾値が不正な型の場合は `TypeError` を送出する。**設定ミスが「安全」と区別できない
  状態にしてはならない。**
- センサ故障値の棄却は Stage 1 で**行わない**。必要になった場合は明示的な `plausible_range` として
  実装し、極端値を黙って握り潰す早期 return は禁止する。
- **数値そのものは導入先の品目と環境に対して未検証（`limits_source: UNVERIFIED`）。** 実装検証用の
  プレースホルダであり、運用判断に用いてはならない。各プロファイルの `limits_source` に出典を記録する。

### (b) `/ingest` が前提とする最小データ要件

- 環境メトリクス: 最低 1 種（水温）× 複数年（推奨 2 年以上、最低 1 年）。塩分・DO・pH 等は任意項目として
  スキーマに保持するが必須としない。
- 事象記録: 発生日・対象種・被害規模（概算可）・簡潔な経過メモの 4 項目を最小必須とし、`course_narrative`
  の詳細度は自由記述で許容する（`/ingest` の LLM 構造化ステップが粒度を均す）。
- データが薄い場合は §「グレースフルデグレード」に記載の縮退動作へ移行する。

### (c) 全滅級誤予測に対する非断定・免責・専門家相談の S-3 出力ゲート設計

- 出力は**非断定の注意喚起＋専門家相談推奨**に限定する。断定表現（「〜が発生します」等）を検出し
  「〜の可能性があります」等の非断定表現へ強制変換する語彙ルールを `_extra_security_gate_output()` に実装。
- 全 Stage 3 言明に根拠 ID 引用を必須とし、根拠 ID を伴わない言明は S-3 で機械的に除去または
  「要検証」に置換する（§ 3 段パイプライン参照）。
- 重大予兆（養殖区画（生簀 / 筏 / 垂下連 等）全滅級リスクと判定されたシグナル）を検出した場合は出力末尾に固定の免責文言と
  「専門家（水産研究・教育機構の魚病診断窓口等）への相談を推奨」という定型文を必ず付与する。
- 自動対応（増餌停止等の制御アクション）は一切実装しない。疾病の確定診断も行わない（公的機関の
  人手診断に委ねる導線のみ提示する）。

### (d) 外部データソースの実仕様裏取り結果 ★最重要確認事項

> 公開仕様と実 URL を一次情報で確認し、取得方式を確認できなかったソースは v1 スコープから明確に外す。

| ソース | 提供元 | 提供形態（一次確認結果） | v1 スコープ | 判断理由 |
|---|---|---|---|---|
| 天気予報 JSON | 気象庁 | **実 API 確認済み**。エンドポイント例: `https://www.jma.go.jp/bosai/forecast/data/forecast/{area_code}.json`（`area_code` は `https://www.jma.go.jp/bosai/common/const/area.json` で解決）。レスポンスは配列で `publishingOffice`/`reportDatetime`/`timeSeries`（`weatherCodes`/`weathers`/`pops`/`temps` 等）を含む。政府標準利用規約下で公開されるが「非公式API」扱いで仕様変更リスクあり | **含む** | 実エンドポイント・構造とも確認済み。仕様変更リスクは低頻度ポーリング＋レスポンス形状のスキーマ検証で許容範囲 |
| 防災情報 XML（警報・台風進路） | 気象庁 | **実 Atom Feed 確認済み**。PULL 型、HTTPS GET のみで取得可（サーバ不要）。高頻度フィード例: `https://www.data.jma.go.jp/developer/xml/feed/extra.xml`（警報等）・長期フィード `extra_l.xml`。`If-Modified-Since` ヘッダでの条件付き取得が推奨される正式な技術要件 | **含む** | 実フィード URL・取得方式・推奨ポーリング作法まで確認済み |
| 海面水温・海況予測（FRA-ROMS 等） | 水産研究・教育機構 | **API/CSV/NetCDF 提供は未確認**。公式サイト（`fra-roms.fra.go.jp`）は地図・グラフの Web 閲覧が主機能で、プログラム的に取得可能な形式は現時点で確認できなかった。水産関係試験研究機関向けには別途利用者申請による研究データ提供の記載があるが、一般利用者向け API は見当たらない | **v1 スコープ外**（抽象インターフェースで逃げず、明確に除外） | Web 掲載のみのソースを実装契約に含めると取得方式を確定できないため、v1 では海況予測は入力ソースに含めない。Stage 1 の複数年同時期水温比較は自場 `env_observations`（センサー/手動記録）のみで実施する |
| 赤潮ネット・貧酸素水塊予測 | 水産研究・教育機構／都道府県水産試験場 | **地域限定・個別システム分立を確認**。「赤潮ネット」（九州・瀬戸内海対象、水産研究・教育機構水産技術研究所運営）はポータルサイトでの情報公表が中心。貧酸素水塊予測は都道府県ごとに個別サイト（千葉県・大阪府等）が別々に運用しており、統一 API は存在しない | **v1 スコープ外**（`farm_id` 単位の config 拡張点として設計のみ用意） | 地域・都道府県ごとに提供元 URL・形式が異なり、v1 で全国統一の自動取得を実装するのは非現実的。`config/species/*.yaml` とは別に `config/farm_regional_sources.yaml`（任意）で `farm_id` ごとに手動設定可能な拡張ポイントとして設計だけ用意し、実装（自動取得）は v2 以降の検討事項とする |

**設計への反映**: `/invoke` の pre_process が取得する予報系ソースは v1 では **気象庁天気予報 JSON + 防災情報
XML の 2 種のみ**とする。Stage 1 のシグナル検知ロジックは「予報系フィード入力が空でも動作する」設計とし
（`forecast_context` が空辞書でも Stage 1 は自場データのみで逸脱検知を継続する）、海況予測・赤潮ネットは
将来の config 拡張点としてスキーマ上の余地（`species_profile` の `regional_source_refs` フィールド）だけ
残す。

## セキュリティ設計方針（S-1〜S-5）

| 層 | 方針 |
|---|---|
| **S-1** | 全 `FunctionNode` に `required_trust_level` を明示宣言（暗黙の `ANONYMOUS` 継承は不可）。有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ。読取専用の `/invoke` は `VERIFIED_EXTERNAL`。書込副作用のある `/ingest` `/feedback` も `VERIFIED_EXTERNAL` 以上（本エージェントでは自主管理業務データの書込のため INTERNAL までは要求しない） |
| **S-2** | `FunctionNode` は `_extra_security_gate_input()` フックで拡張（`@final` の `_security_gate_input()` は触らない）。外部予報系 API のレスポンスは **untrusted 扱い**とし、パース前にスキーマ検証を行う。現場スタッフ氏名程度の PII を除去 |
| **S-3** | `_extra_security_gate_output()` で断定表現の抑制・免責付与・**根拠 ID なし断定の機械的除去**を実装（§(c) 参照）。養殖区画全滅級リスクを扱うため断定禁止を設計原則とする |
| **S-4** | 各ノードの `execute()` 内で `emit_trace_event()` を最低 1 回（副作用の有無を問わず）。`node_start`/`node_complete` は framework 側が自動発行するため execute 内で重複させない |
| **S-5** | secrets は `ctx.secrets.require()` のみ（`os.environ` 禁止）。`config/agent.yaml` の `requires.secrets` に宣言（本テンプレは LLM API キー等を想定。気象庁の公開 API/フィードは無認証のため secrets 不要） |

**テナント fail-closed**: `pre_process` で `farm_id` の解決に失敗した場合は即座に `status=error` を返し
処理を止める（他場のデータが混ざらないようにするための必須ガード）。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, permissions, credential handle)
- [ ] ConnectionPolicy (retry/timeout strategy) — 予報系 API 取得の再試行方針として `/invoke` の
      `pre_process` に実装予定（実装フェーズで確定）
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — 予報系レスポンスのスキーマ検証・PII 除去
- [x] S-3: `_extra_security_gate_output()` — 断定表現抑制・免責付与・根拠なし断定の機械的除去
- [x] S-4: `emit_trace_event()` — 各ノード execute() 内で最低 1 回

### Composition Pattern

- **Pattern**: GraphNode（subgraph）
- **Composition target**: `src/graph/domain_workflow_graph.py`（`AquacultureWorkflowGraph`。3 モード分岐
  + `/invoke` 内 3 段パイプライン）
- **Error propagation strategy**: `propagate`（内側グラフのエラーは `SubgraphError` として再送出し
  outer で `status=error` として扱う。フェイルセーフより明示的なエラー可視化を優先する設計判断 —
  誤って空のアドバイザリを返すより処理停止の方が全滅級リスクの誤誘導を防げるため）

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only（`agents/base/` は不要）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 3 モードは固定パイプライン（think-act ループ不要）。決定論(Stage1)→検索(Stage2)→LLM(Stage3) の役割分離が明確なため自律ループより固定 backbone が適切 |
| Composition pattern | Standalone main | GraphNode（subgraph） | **GraphNode** | mode 分岐＋3段パイプラインという多段ドメインロジックを inner subgraph に隠蔽し、outer backbone は標準のまま保つ（Cat2 標準構成） |
| 予報系ソース範囲 | 4 ソース全て実装 | 実在確認済み 2 ソースのみ | **気象庁 JSON+XML の 2 ソースのみ** | §(d) の確認結果に基づき、取得方式を確認できたソースだけを実装契約に含めるため |
| LLM 呼び出し方式 | モデル ID をコード内ハードコード | `config["llm"]` 注入シーム | **注入シーム** | 実行環境からモデルを注入できるよう、`graph.py` が `config.get("llm")` で取り出し、ノードは `None` のとき deterministic fallback |
| HITL | 必須（重大予兆で中断） | 任意（提示に留める） | **HITL なし** | 養殖区画全滅級の緊急時に承認待ちで処理を止めるより、非断定・免責付き即時提示 + 専門家相談推奨の方が運用上安全。Proposal §「その他の設計方針」の判断を踏襲 |

## ユーザビリティ設計レビュー（設計判断への反映）

エンドユーザー（養殖場管理者・現場責任者）のユースケースを想定して以下をレビューし、設計を修正した。

**入力面（修正した点）**:
- 当初案では `/ingest` の入力を厳密な列名指定 CSV と想定していたが、現場は手書き日誌・自由書式 Excel が
  主流という利用実態を踏まえ、`/ingest` の事象記録・観測メモは
  **自由記述テキストを許容し LLM 構造化ステップで正規化する**設計に修正した（§データモデル・(b) 参照）。
  列名を厳密指定する構造化入力を要求すると「養殖専用アプリの個別開発を ready-made で解消する」という
  提案の中核価値と矛盾するため。
- `farm_id` / `species_code` のみを必須入力とし、環境メトリクスは水温 1 種のみで動作を開始できる
  設計とした（§(b)）。全メトリクス揃わないと使えない設計は現場の初期導入障壁になるため。

**出力面（修正した点）**:
- Stage 3 の出力は「根拠 ID の羅列」ではなく、**現場責任者がそのまま次の判断（増餌抑制・出荷前倒し・
  専門家相談）に使える自然文アドバイザリ＋根拠の脚注形式**とする（§6-2 の出力イメージを踏襲）。
  技術的な内部 ID（signal_id 等）をそのまま前面に出さず、`formatted_output` で日本語の平易な文章に
  変換してから提示する（`post_process` の責務として明記）。
- **severity（重大度）の視認性を確保する（`post_process` 実装契約）**: §(c) の S-3 出力ゲート
  は断定表現を「〜の可能性があります」へ強制変換するため、**文末表現が一律に揃い、全滅級の重大予兆と
  軽微な逸脱が同じトーンで並んで重大度差が現場に伝わらない**（狼少年化して読み飛ばされる運用リスク）。
  これを防ぐため:
  - `post_process` の `formatted_output` は、**アドバイザリ本文の冒頭に severity を明示的に提示する**。
    severity を本文中の語調に依存させず、**冒頭の構造化された見出しとして分離提示**する。
  - severity は Stage 1 の逸脱シグナル（`signal_list[].severity`）および Stage 2 の候補リスクから
    **決定論的に導出**する（LLM の主観判断に委ねない）。
  - 重大予兆（養殖区画全滅級リスク）と判定された場合は、§(c) の免責文言・専門家相談推奨の定型文に加え、
    **冒頭 severity 表示でも最上位として区別可能**とする。
  - 設計意図: 「非断定」と「重大度が伝わらない」は別問題である。断定を避けつつ緊急度は正確に伝える。
  - この項目は `post_process` の実装契約として拘束力を持つ。

**エラー/フォールバック面（設計に明記した点）**:
- 予報系 API（気象庁）が取得失敗した場合、`/invoke` は**処理を止めずに** `forecast_context` を空辞書に
  して Stage 1 を自場データのみで継続する（グレースフルデグレード。§(d) 設計への反映）。
- 蓄積データが薄い初期（`incidents` が少数）の場合は、Stage 2 の自場照合結果が空になっても Stage 3 は
  「一般病害知識ベースの注意喚起」に縮退して出力を継続する（moat が育つ前でも最低限の価値を提供する
  ためのグレースフルデグレード設計）。
- `farm_id` 解決失敗時は空のアドバイザリを返さず明示的にエラーを返す（テナント fail-closed。
  他場データ混入より処理停止の方が安全という判断）。

## その他の設計方針

- **LLM モデル ID のハードコード禁止**: `config["llm"]` 注入シームのみを使用する。`graph.py` が
  `config.get("llm")` で取り出し、ノードは `None` のとき deterministic fallback（Stage 3 が呼べない
  場合は Stage 1/2 の結果のみを返す）。このとき **`status` は `"success"` のまま**とし、
  `degradation_reason="llm_unavailable"` を付与する（§`status` の有効値 参照。`"degraded"` は
  `AgentStatus` に存在しないため `status` に代入してはならない）。
- **グレースフルデグレード**: 蓄積データが薄い初期は一般病害知識ベースの注意喚起に縮退し、蓄積が
  進むほど自場照合（Stage 2）へ比重が移行する。
- **HITL は必須としない**: 重大予兆でも「専門家相談を推奨」提示に留め、断定・自動対応はしない
  （§ Design Decision Record 参照）。
