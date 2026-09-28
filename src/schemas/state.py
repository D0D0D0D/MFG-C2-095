"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# Field contract source of truth: docs/02_design.md §State Definition.
# See docs/02_design.md §🔴 status の有効値 for the allowed `status` values
# ("success" / "retry" / "error" only — "degraded" is NOT an AgentStatus value;
# graceful degradation is expressed via status="success" + degradation_reason).

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for MFG-C2-095 AquacultureEarlyWarningGraph.

    mode: "ingest" | "invoke" | "feedback" — selects which internal
    workflow domain_workflow_graph.py executes.
    """

    # ── Common to all modes ──────────────────────────────────────────────
    mode: str
    farm_id: str
    species_code: str
    # Mode-specific request body (e.g. current_observations for /invoke,
    # records for /ingest, incident_id/note_free_text for /feedback).
    payload: dict

    # ── Populated by pre_process ─────────────────────────────────────────
    farm_context: dict
    species_profile: dict
    forecast_context: dict
    # list[str]: raw JMA forecast JSON / bosai XML payloads, stringified.
    # Used by Stage 3 LLM citation and S-3 provenance verification —
    # NOT the human-readable forecast_context summary.
    forecast_raw: list

    # ── /invoke: 3-stage pipeline outputs ────────────────────────────────
    # Bounded to _MAX_SIGNALS (signal_detection_node.py) — the highest-severity
    # signals are kept, never a naive first-N truncation (a lethal signal must
    # never be silently dropped from the list Stage 3 derives severity from).
    signal_list: list
    # True count of signals Stage 1 produced, even when `signal_list` itself is
    # capped to _MAX_SIGNALS (state/response-size bound). Same disclosure
    # contract as unparseable_observation_count below.
    signal_list_total_count: int
    # Present (True) only when signal_list_total_count > len(signal_list).
    signal_list_truncated: bool
    # Observations whose `value` could not be read as a number. Stage 1 refuses
    # to drop these silently — an unreadable reading is a data-quality defect,
    # not an absence of risk. Must be declared here or LangGraph's state_schema
    # projection discards it (see the /ingest note below).
    unparseable_observations: list
    # True count of rejected observations, even when `unparseable_observations`
    # retains only the first N records (state-size bound).
    unparseable_observation_count: int
    # Bounded to _MAX_CANDIDATE_RISKS (signal_matching_node.py) — the
    # highest-severity_baseline candidates are kept, never a naive first-N
    # truncation (mirrors signal_list's cap contract one stage down: a lethal
    # risk must never be silently dropped from the list Stage 3 derives
    # severity from).
    candidate_risks: list
    # True count of candidates Stage 2 produced, even when `candidate_risks`
    # itself is capped to _MAX_CANDIDATE_RISKS (state/response-size bound).
    # Same disclosure contract as signal_list_total_count above.
    candidate_risks_total_count: int
    # Present (True) only when candidate_risks_total_count > len(candidate_risks).
    candidate_risks_truncated: bool
    advisory_output: dict

    # ── /ingest intermediate pipeline fields ─────────────────────────────
    # These flow between normalize_input -> structure_with_llm ->
    # validate_records -> write_records inside the inner graph. They must
    # be declared here (not just implicitly returned by nodes) because
    # LangGraph's StateGraph filters node I/O through state_schema — an
    # undeclared field is silently dropped between node hops (the same
    # projection pitfall documented for `mode` in
    # docs/02_design.md §🔴 inner graph の state 型注釈方針).
    #
    # NOTE (review F-04, non-blocking/LOW): declared on the OUTER
    # AgentState purely to satisfy the inner graph's state_schema projection
    # above — these four fields are INNER-GRAPH-SCOPED ONLY (the /ingest
    # sub-pipeline). The outer graph's post_process / finalize nodes MUST NOT
    # read them; the outer-visible /ingest result is `ingest_summary` below,
    # which is what post_process._format_ingest_summary consumes.
    normalized_records: list
    records_needing_llm_structuring: list
    valid_records: list
    validation_errors: list

    # ── /ingest outputs ──────────────────────────────────────────────────
    ingest_summary: dict

    # ── /feedback intermediate pipeline fields ───────────────────────────
    resolved_incident_id: str
    updated_incident_fields: dict

    # ── /feedback outputs ────────────────────────────────────────────────
    feedback_result: dict

    # ── post_process output ──────────────────────────────────────────────
    formatted_output: dict

    # ── Graceful degradation marker (NOT part of AgentStatus) ───────────
    # Set alongside status="success" when a mode ran in a reduced-value
    # path (e.g. LLM unavailable, thin incident history). Absent on the
    # normal path.
    degradation_reason: str
