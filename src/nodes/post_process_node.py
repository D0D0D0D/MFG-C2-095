"""AgentCore Platform v1.0"""

# post_process — docs/02_design.md Node Configuration §post_process +
# §ユーザビリティ設計レビュー (severity visibility contract) +
# §(c) S-3 output gate design.
#
# Responsibilities:
#   1. Shape formatted_output per mode (advisory / ingest summary / feedback confirmation)
#   2. For /invoke: prepend an explicit severity heading (decoupled from prose tone,
#      since S-3 flattens assertive language into a uniform non-assertive register)
#   3. S-3 extension (_extra_security_gate_output): assertive-language downgrade,
#      unsupported-claim stripping, fabricated-evidence-ID detection, mandatory
#      disclaimer + expert-consultation recommendation for critical severity.

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inner.signal_detection_node import _truncate

_SEVERITY_LABEL_JA = {
    "low": "軽微",
    "medium": "要注意",
    "high": "警戒",
    "critical": "重大（養殖区画全滅級リスクを含む可能性）",
}

# Assertive language -> non-assertive rewrite. re.IGNORECASE (combined with
# the caller normalizing the subject text via NFKC before matching) gives
# the "NFKC + casefold" bypass-proofing required by docs/02_design.md
# §セキュリティ設計方針 S-3 without permanently lowercasing the displayed
# narrative (casefold() on the display text itself would corrupt evidence
# IDs like "sig-..." into an unreadable-but-still-technically-lowercase form
# — matching case-insensitively while preserving original casing is the
# safer bypass defense here).
#
# NOTE (review F-01, non-blocking/LOW): `(です。$)` uses `$`, which by
# default anchors to the END OF THE WHOLE STRING, not "end of sentence" — a
# mid-narrative "〜です。次に…" is NOT rewritten by this pattern; only a
# trailing "〜です。" at the very end of `narrative` is. This is intentional,
# not a coverage gap in the safety contract: this list runs BEFORE sentence
# splitting, as a best-effort softening pass over the whole narrative, and
# does not need to be exhaustive. The exhaustive, safety-critical net is
# `_strip_unsupported_claims` (step 3, per-sentence, running AFTER this list)
# — every sentence, assertive-sounding or not, that lacks a valid evidence-ID
# citation is redacted there regardless of whether this list happened to
# soften its tone first. Widening `$` to match every "です。" occurrence
# (e.g. via re.MULTILINE per-line anchoring, or dropping `$` and iterating
# sentences) would also rewrite plain, non-assertive declarative sentences
# ("〜観測されています。" style constructs untouched) mid-narrative, reading
# as unnatural Japanese for no safety benefit — so this pattern intentionally
# stays end-of-string-only; do not "fix" it without re-validating the
# resulting Japanese against the corpus in tests/unit/test_post_process_node.py.
_ASSERTIVE_PATTERNS = [
    (re.compile(r"(発生します)", re.IGNORECASE), "発生する可能性があります"),
    (re.compile(r"(が起きます)", re.IGNORECASE), "が起きる可能性があります"),
    (re.compile(r"(確実です)", re.IGNORECASE), "の可能性が高いと考えられます"),
    (re.compile(r"(間違いありません)", re.IGNORECASE), "の可能性があります"),
    (re.compile(r"(です。$)", re.IGNORECASE), "と考えられます。"),
    (re.compile(r"\bwill\s+occur\b", re.IGNORECASE), "may occur"),
    (re.compile(r"\bwill\s+happen\b", re.IGNORECASE), "may happen"),
]

_DISCLAIMER_JA = (
    "\n\n【免責】本アドバイザリは非断定の注意喚起であり、疾病の確定診断ではありません。"
    "重大な予兆が疑われる場合は、水産研究・教育機構の魚病診断窓口等の専門家への相談を推奨します。"
)

# Agent-authored boilerplate that the per-sentence evidence-ID check (step 3)
# must never strip. These sentences carry no provenance requirement because the
# agent, not the LLM, emits them verbatim.
#
# Matched by EXACT sentence equality (after NFKC + whitespace/delimiter
# trimming), never as a substring. Substring matching would be a bypass: an LLM
# could smuggle an unsourced claim through by prefixing a protected phrase —
# "一般的な養殖知識ベースに基づく注意喚起ですが、全滅は確実です。" — and the
# whole sentence, claim included, would be waved past the gate. Exact equality
# means only the agent's own verbatim lines are exempt.
#
# Sources: _DISCLAIMER_JA (this module) and
# AdvisorySynthesisNode._deterministic_fallback (the graceful-degradation path,
# degradation_reason="llm_unavailable" — redacting its framing lines would
# empty the degraded advisory of all meaning).
_PROTECTED_SENTENCES = frozenset(
    unicodedata.normalize("NFKC", s)
    for s in (
        "【免責】本アドバイザリは非断定の注意喚起であり、疾病の確定診断ではありません",
        "重大な予兆が疑われる場合は、水産研究・教育機構の魚病診断窓口等の専門家への相談を推奨します",
        "一般的な養殖知識ベースに基づく注意喚起（詳細解釈は現在利用できません）:",
        "現時点で顕著な逸脱シグナルは検出されていません",
    )
)

# Redaction markers this gate emits. A sentence that has ALREADY been fully
# replaced by one of these needs no further checking (idempotency), but a
# sentence that merely CONTAINS one — e.g. step 2 rewrote a fabricated ID,
# leaving "[要検証]によれば赤潮のリスクがあります。" — is still an unsourced
# claim and MUST fall through to the step-3 provenance check. Treating any
# occurrence as "protected" would let a fabricated citation shield the very
# claim it was invented to support.
_REDACTION_MARKER_INVALID_ID = "[要検証]"
_REDACTION_MARKER_NO_EVIDENCE = "[要検証：根拠となる観測シグナル/過去事象IDが確認できません]"
_REDACTION_MARKER_CONTROL = "[要検証：制御指示は本AGの対象外]"

# `signal_index` is built from CLIENT-CONTROLLED fields (pond_id / metric /
# observed_at come straight from the invoke payload — see SignalDetectionNode)
# and is echoed verbatim to the operator AND persisted to the checkpoint DB.
# Unlike `narrative`, this structured data never passes through the
# per-sentence provenance check above, so it needs its own bound:
#   - length: reuse SignalDetectionNode's _MAX_FIELD_CHARS / _truncate rather
#     than inventing a second limit that could drift out of sync.
#   - count: same shape as unparseable_observations' cap-detail/keep-count
#     pattern below (_MAX_REJECTED_RECORDED in signal_detection_node.py).
#     Detail may be capped; the true count is always surfaced.
_MAX_SIGNAL_INDEX_RECORDED = 50

_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _select_highest_severity(signals: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Select up to `limit` signals for display, SAFETY-CRITICAL ordering:
    highest severity first (ties broken by |deviation| descending), never a
    naive first-N slice.

    Mirrors SignalDetectionNode._apply_signal_cap's ordering rationale one
    layer down: `signal_list` arriving here is already capped upstream (also
    severity-priority-ordered), but display-layer truncation must independently
    preserve the invariant max(severity of shown) == max(severity of eligible)
    — a plain slice can still drop the one critical entry if it happens to sit
    past position `limit` in arrival order (e.g. appended last, with `limit`
    medium-severity signals ahead of it). Returns the selected subset restored
    to ORIGINAL arrival order (selection changes WHICH signals are kept, not
    the order they are displayed in).
    """
    if len(signals) <= limit:
        return signals

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, float]:
        _index, sig = item
        rank = _SEVERITY_RANK.get(sig.get("severity"), -1)
        deviation = sig.get("deviation")
        magnitude = abs(deviation) if isinstance(deviation, (int, float)) else 0.0
        return (rank, magnitude)

    indexed = list(enumerate(signals))
    indexed.sort(key=_sort_key, reverse=True)
    kept_indices = {index for index, _sig in indexed[:limit]}
    return [sig for index, sig in enumerate(signals) if index in kept_indices]

# A client-supplied field (pond_id, metric, observed_at, farm_id, value, ...)
# that CONTAINS a string shaped like a real evidence ID ("sig-..." / "inc-...")
# could be mistaken by an operator (or a downstream system) for a genuine
# citation — e.g. pond_id="根拠: sig-FAKE-999。". These fields are raw echoes,
# never validated against the real signal_id/incident_id set the way narrative
# citations are, so any such lookalike is unconditionally fabricated and is
# masked outright (not compared against valid_ids: a client cannot supply a
# real signal_id here even legitimately, since signal_id is agent-generated).
_REDACTION_MARKER_FIELD_ID = "[検証不能な根拠ID表記のためマスク]"

# The framework's own S-3 credential scan (FunctionNode._security_gate_output,
# @final, `shared.security.credential_detector.detect_credentials`) only
# scans TOP-LEVEL string values of the `result` dict returned by execute() —
# `result["formatted_output"]` is itself a dict, so a credential-shaped token
# riding inside a nested signal_index field (pond_id/metric/observed_at) is
# invisible to it, exactly like the evidence-ID lookalike case above. Reuse
# the SAME detector here (never hand-roll a second credential pattern set
# that could drift out of sync with the framework's).
_REDACTION_MARKER_CREDENTIAL = "[検証不能な認証情報様文字列のためマスク]"

# Supplementary pattern for a KNOWN gap in the shared detector, not a
# duplicate of it. `detect_credentials`'s "openai_key" pattern is
# `sk-[a-zA-Z0-9]{20,}` — it requires an unbroken alnum run immediately after
# `sk-`, so it does not match Anthropic-style keys
# (`sk-ant-api03-<base64url with '-'/'_'>`), whose payload legitimately
# contains `-`/`_`. This is a shared-framework limitation (`shared/` is
# vendored and must not be edited by this template) that this template
# cannot fix upstream, so it is closed locally, narrowly, only for the
# `sk-ant-...` shape, and only as a supplement layered AFTER `detect_credentials`
# — never a replacement for it.
_ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")

# ── general S-3 output sanitizer (formatted_output-wide, allowlist-based) ──
#
# `signal_index` was the first place a client-controlled field was found
# leaking fabricated evidence IDs / control instructions past S-3 (only
# `narrative` was scrubbed). Fixing that one path field-by-field just moves
# the same bug to the next dict that echoes client input — exactly what
# happened with `data_quality_warnings.unreadable_observations` (built from
# the SAME client-controlled current_observations payload, but assembled by
# SignalDetectionNode._reject / _format_advisory, entirely separate code from
# signal_index). Denylisting "the fields we know about" recreates this gap
# every time a new field is added.
#
# So this is inverted into an ALLOWLIST over the whole `formatted_output`
# tree: every string is sanitized UNLESS its dict key is in
# _SANITIZE_EXEMPT_KEYS (an agent-generated / already-safe value) or it lives
# under the `narrative` key (handled by the dedicated per-sentence provenance
# pipeline above — running this generic pass over narrative too would
# double-process it and fight with _strip_unsupported_claims' sentence-level
# semantics).
#
# Keys exempt from generic sanitization:
#   - narrative              : handled by steps 1-4 above (own pipeline)
#   - severity                : controlled vocabulary (low/medium/high/critical);
#                                masking it could hide a critical warning
#   - severity_label_ja       : derived 1:1 from severity, same reasoning
#   - signal_id               : agent-generated (_build_signal_id /
#                                _injective_digest) — already safe by
#                                construction, must not be altered here
#   - cited_signal_ids        : agent-generated citation list (LLM's cited
#                                IDs, ALREADY filtered against valid_ids by
#                                step 2 above by the time this runs — see
#                                execute() ordering)
#   - cited_incident_ids      : same as cited_signal_ids
#   - incident_ids            : store-generated (WriteRecordsNode /
#                                BaseIncidentStore.write_incidents), not
#                                client input
#   - reason                  : a fixed enum value written by
#                                SignalDetectionNode._reject
#                                (e.g. "unreadable_value"), not client text
_SANITIZE_EXEMPT_KEYS = frozenset(
    {
        "narrative",
        "severity",
        "severity_label_ja",
        "signal_id",
        "cited_signal_ids",
        "cited_incident_ids",
        "incident_ids",
        "reason",
    }
)


def _sanitize_scalar_string(value: str) -> str:
    """Apply the full client-input sanitization pipeline to one string:
    evidence-ID lookalike masking -> control-action suppression ->
    credential detection/masking -> length bound. Order mirrors
    _sanitize_signal_index's original per-field pipeline (credential scan
    last, on already-masked text, so surrounding context survives).
    """
    sanitized = _EVIDENCE_ID_PATTERN.sub(_REDACTION_MARKER_FIELD_ID, value)
    for pattern, replacement in _CONTROL_ACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    for finding in detect_credentials(sanitized):
        sanitized = sanitized.replace(finding["value"], _REDACTION_MARKER_CREDENTIAL)
    sanitized = _ANTHROPIC_KEY_PATTERN.sub(_REDACTION_MARKER_CREDENTIAL, sanitized)
    return _truncate(sanitized)


# Depth ceiling for _sanitize_client_echoes. This is the last line of defense
# against a RecursionError DoS: SignalDetectionNode now rejects non-scalar
# metric/observed_at/pond_id at the boundary (see
# signal_detection_node.execute's "malformed_field_type" checks), but this
# sanitizer walks the ENTIRE formatted_output tree unconditionally (step 5 of
# _extra_security_gate_output) and must never assume upstream validation was
# applied everywhere client-controlled data can enter it (e.g. a future field,
# or the jma_warning branch's observed_at, which is not run through the same
# scalar check). A client sending e.g. `observed_at=[[[...]]]` nested ~1000
# levels deep would otherwise hit Python's recursion limit and raise
# RecursionError, crashing the ENTIRE invocation. 8 is generous headroom over
# any legitimate formatted_output shape produced by this agent (deepest real
# structure is signal_index[].* at 2 levels) while stopping arbitrarily deep
# client-supplied nesting well before the interpreter's recursion ceiling.
_SANITIZE_MAX_DEPTH = 8

_DEPTH_TRUNCATED_MARKER = "[構造が深すぎるため打ち切り]"


def _sanitize_client_echoes(node: Any, *, exempt: bool = False, _depth: int = 0) -> Any:
    """Recursively sanitize every string in `node` that is NOT reached via an
    exempt key (see _SANITIZE_EXEMPT_KEYS). Walks dicts and lists to any
    depth so newly-added nested fields are covered automatically — this is
    the property the old field-enumeration approach (_sanitize_signal_index)
    did not have.

    `exempt` propagates down from an exempt key: once a value is reached via
    e.g. "cited_signal_ids", every string inside it (including list
    elements) is left untouched, since the whole subtree is agent-generated.

    MUST NEVER raise RecursionError: `_depth` bounds the walk at
    `_SANITIZE_MAX_DEPTH` and anything deeper is replaced with a safe marker
    rather than recursed into further (see `_SANITIZE_MAX_DEPTH` docstring —
    this is a DoS defense, not a display nicety).
    """
    if _depth >= _SANITIZE_MAX_DEPTH and isinstance(node, (dict, list)):
        return _DEPTH_TRUNCATED_MARKER
    if isinstance(node, dict):
        return {
            key: _sanitize_client_echoes(
                val, exempt=exempt or key in _SANITIZE_EXEMPT_KEYS, _depth=_depth + 1
            )
            for key, val in node.items()
        }
    if isinstance(node, list):
        return [_sanitize_client_echoes(item, exempt=exempt, _depth=_depth + 1) for item in node]
    if isinstance(node, str):
        return node if exempt else _sanitize_scalar_string(node)
    # int / float / bool / None pass through untouched.
    return node

# Evidence-ID matcher. Boundaries are expressed as "not an ASCII word char /
# hyphen" rather than \b or (?<![\w-]): Python's \w is Unicode-aware, so a
# Japanese character counts as a word char and "水温がsig-1で" would NOT match
# — silently dropping a VALID citation and causing the step-3 gate to redact a
# properly-sourced sentence. Evidence IDs are ASCII by construction
# (docs/02_design.md: "sig-{metric}-{observed_at}-{farm_id}"), so anchoring on
# ASCII boundaries is both correct and CJK-safe.
_EVIDENCE_ID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z-])(sig-[0-9A-Za-z_-]+|inc-[0-9A-Za-z_-]+)(?![0-9A-Za-z-])"
)

# Sentence splitter for the per-sentence evidence-ID check (docs/02_design.md
# §(c): "根拠 ID を伴わない言明は S-3 で機械的に除去または「要検証」に置換する").
# Splits on Japanese 。！？ and their fullwidth/newline variants while keeping
# the delimiter attached to its sentence, so the reassembled narrative reads
# naturally after redaction.
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？\n])")

# Provenance is enforced as an ALLOWLIST, not a denylist.
#
# docs/02_design.md §(c) requires that "全 Stage 3 言明に根拠 ID 引用を必須と
# し、根拠 ID を伴わない言明は S-3 で機械的に除去または「要検証」に置換する".
# The safe reading is: a sentence survives only if it PROVES its provenance.
# Screening on a keyword list of "risky-sounding" words would be a denylist —
# an LLM phrasing an unsourced claim outside the vocabulary ("明日、生簀の魚が
# すべて死にます。") would sail through. The gate must not depend on having
# enumerated every dangerous phrasing in Japanese.
#
# So: every sentence requires a valid citation, EXCEPT sentences that make no
# claim at all. We recognize those structurally rather than lexically — a
# non-claim is a heading or a label fragment, i.e. it carries no predicate.
# Anything with sentence-ending punctuation is treated as a claim.
_NON_CLAIM_FRAGMENT_PATTERN = re.compile(
    r"""^\s*
    (?:
        [#＃]+\s*.*            # markdown-style heading
      | [-*・]\s*[^。！？]{0,20}$  # short bullet label with no predicate
      | 【[^】]*】\s*$          # 【severity】-style heading
      | [^。！？\n]{0,20}[:：]\s*$ # "根拠:" style label
    )
    \s*$""",
    re.VERBOSE,
)

# Control-action imperatives are additionally never permitted regardless of
# citation (docs/02_design.md §(c): "自動対応（増餌停止等の制御アクション）は
# 一切実装しない" — the advisory suggests consulting an expert, it does not
# issue operational commands). These phrasings are rewritten to a
# non-directive form even when they cite a valid evidence ID.
_CONTROL_ACTION_PATTERNS = [
    (re.compile(r"直ちに[^。！？\n]*?(?:してください|すること)", re.IGNORECASE), _REDACTION_MARKER_CONTROL),
    (re.compile(r"[^。！？\n]*?を(?:稼働|停止|操作)してください", re.IGNORECASE), _REDACTION_MARKER_CONTROL),
]


class PostProcessNode(FunctionNode):
    """Format and finalize output; S-3 output gate is the primary safety net
    for the non-assertive / provenance-verified advisory contract.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        mode = state.get("mode", "")
        formatted: dict[str, Any]

        if mode == "invoke":
            formatted = self._format_advisory(state)
        elif mode == "ingest":
            formatted = self._format_ingest_summary(state)
        elif mode == "feedback":
            formatted = self._format_feedback_result(state)
        else:
            formatted = {"summary": "unknown mode", "mode": mode}

        # 🔴 AgentBaseGraph.get_output() (framework default) only surfaces
        # state["formatted_output"]/["result"] to the caller as `output` — it
        # does NOT read state["degradation_reason"]. A degradation_reason set
        # by an inner node therefore vanished from the caller-visible response
        # even though it was present in State/the checkpoint: an operator saw a
        # confident-looking advisory with no indication that the JMA fetch had
        # failed or that the LLM had been unavailable. Fold it into
        # formatted_output itself (with explicit Japanese framing) so the
        # 縮退した旨 always reaches the operator.
        if state.get("degradation_reason"):
            formatted = {**formatted, "degradation_reason": state["degradation_reason"]}
            formatted["degradation_notice_ja"] = self._degradation_notice(state["degradation_reason"])

        emit_trace_event("post_process_formatted", {"mode": mode}, state)

        result: dict[str, Any] = {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
        if state.get("degradation_reason"):
            result["degradation_reason"] = state["degradation_reason"]
        return result

    @staticmethod
    def _degradation_notice(degradation_reason: str) -> str:
        notices = []
        for reason in degradation_reason.split("+"):
            if reason == "llm_unavailable":
                notices.append("LLMが利用できないため決定論的な簡易所見に縮退しています。")
            elif reason == "forecast_fetch_failed":
                notices.append("気象庁データを取得できなかったため、気象要因を加味せずに判定しています。")
            elif reason == "species_profile_not_found":
                notices.append("この魚種の魚種プロファイルが未整備のため、魚種固有の閾値・リスク照合をスキップしています。")
            else:
                notices.append(f"縮退動作: {reason}")
        return " ".join(notices)

    # ── mode-specific formatting ─────────────────────────────────────────

    def _format_advisory(self, state: dict) -> dict[str, Any]:
        advisory = state.get("advisory_output", {}) or {}
        signal_list = state.get("signal_list", []) or []
        severity = self._derive_display_severity(advisory, signal_list)

        narrative = advisory.get("narrative", "")

        formatted = {
            # Severity as a structured heading, decoupled from prose tone
            # (docs/02_design.md §ユーザビリティ設計レビュー — "非断定" と
            # "重大度が伝わらない" は別問題).
            "severity": severity,
            "severity_label_ja": _SEVERITY_LABEL_JA.get(severity, severity),
            "narrative": narrative,
            "cited_signal_ids": advisory.get("cited_signal_ids", []),
            "cited_incident_ids": advisory.get("cited_incident_ids", []),
        }

        # signal_id is ASCII-sanitized so the S-3 provenance gate can verify it
        # (see SignalDetectionNode._id_component). That makes it unreadable for a
        # pond named 「生簀A」. Give the operator the map back to the real labels —
        # otherwise a multi-pond farm cannot tell which pond an advisory is about.
        #
        # pond_id / metric / observed_at are CLIENT-CONTROLLED and are persisted
        # to the checkpoint DB + echoed in every response, exactly like
        # unparseable_observations below — so bound them the same way with the
        # SAME shared _truncate/_MAX_FIELD_CHARS (never a second, drifting
        # limit). signal_id itself is agent-generated (not client-controlled)
        # and is left untouched — see _build_signal_id / _injective_digest,
        # which must not be altered here.
        # severity/value are never truncated: a truncated severity could hide
        # "critical" from an operator, which is strictly worse than any
        # display-length concern.
        eligible = [s for s in signal_list if s.get("signal_id")]
        total_signal_count = len(eligible)
        # SAFETY-CRITICAL: select by severity-priority, NOT a naive first-N
        # slice. `signal_list` arrives here already capped to _MAX_SIGNALS by
        # SignalDetectionNode (also severity-priority-ordered, so a critical
        # signal always survives THAT cap) — but a plain `eligible[:N]` slice
        # here re-introduces the exact same failure one layer down: if the
        # critical signal happens to sit past position N within the already-
        # capped list (e.g. it was appended last and every signal ahead of it
        # is `medium`), it silently drops out of the operator-visible
        # `signal_index` even though `formatted["severity"]` correctly says
        # "critical" — an operator reading only the detail list would see zero
        # critical entries. `_select_highest_severity` guarantees the
        # displayed subset always contains the true maximum severity present.
        kept = _select_highest_severity(eligible, _MAX_SIGNAL_INDEX_RECORDED)
        signal_index = [
            {
                "signal_id": s["signal_id"],
                "metric": _truncate(s.get("metric")),
                "pond_id": _truncate(s.get("pond_id")),
                "observed_at": _truncate(s.get("observed_at")),
                "value": s.get("value"),
                "severity": s.get("severity"),
            }
            for s in kept
        ]
        if signal_index:
            formatted["signal_index"] = signal_index
            # Detail may be capped for state/response size; the TRUE count is
            # never hidden (same contract as unparseable_observation_count).
            # severity/signal_list themselves are NEVER affected by this cap —
            # a lethal signal must never disappear because the display index
            # ran out of room.
            #
            # NOTE: `total_signal_count` here is len(signal_list) as received by
            # this node — i.e. AFTER SignalDetectionNode's own _MAX_SIGNALS cap
            # (severity-priority-ordered) has already been applied upstream. The
            # TRUE end-to-end total (before ANY capping) is
            # state["signal_list_total_count"], surfaced separately below —
            # the two numbers can differ and both are disclosed rather than
            # conflated, so an operator can tell "how many of the signals we
            # kept are shown" (signal_index_total_count) apart from "how many
            # signals existed in total before any capping"
            # (signal_list_total_count).
            formatted["signal_index_total_count"] = total_signal_count
            if total_signal_count > len(signal_index):
                formatted["signal_index_truncated"] = True

        # True count of signals BEFORE SignalDetectionNode's _MAX_SIGNALS cap,
        # and whether that cap actually dropped anything. Distinct from
        # signal_index_total_count above (which counts post-cap signal_list).
        # Surfaced unconditionally whenever Stage 1 ran, mirroring
        # unparseable_observation_count's "always disclose the true count"
        # contract — an operator must be able to tell "500 shown, 20000 total"
        # apart from "500 shown, 500 total".
        if "signal_list_total_count" in state:
            formatted["signal_list_total_count"] = state["signal_list_total_count"]
            if state.get("signal_list_truncated"):
                formatted["signal_list_truncated"] = True

        # True count of Stage 2 candidate_risks BEFORE SignalMatchingNode's
        # _MAX_CANDIDATE_RISKS cap, and whether that cap actually dropped
        # anything. Same "always disclose the true count" contract as
        # signal_list_total_count directly above — an operator must be able to
        # tell "200 shown, 3000 total" apart from "200 shown, 200 total".
        # cited_incident_ids above is already derived from the (capped)
        # advisory_output.cited_incident_ids, so this count is the only place
        # the pre-cap volume is visible to the operator.
        if "candidate_risks_total_count" in state:
            formatted["candidate_risks_total_count"] = state["candidate_risks_total_count"]
            if state.get("candidate_risks_truncated"):
                formatted["candidate_risks_truncated"] = True

        # An observation Stage 1 could not read is a data-quality defect, not an
        # absence of risk. Surface it as STRUCTURED data (never folded into the
        # narrative, which the S-3 provenance gate would redact for citing no
        # evidence ID). docs/02_design.md §(a) 🔴.
        unparseable = state.get("unparseable_observations") or []
        if unparseable:
            # The TRUE count may exceed len(unparseable): detail records are capped
            # to bound state size, but the count never is.
            total = state.get("unparseable_observation_count") or len(unparseable)
            warnings: dict[str, Any] = {
                "unreadable_observation_count": total,
                "unreadable_observations": unparseable,
                "message_ja": (
                    f"{total} 件の観測値を評価できませんでした。"
                    "これらは判定に含まれていません。入力データを確認してください。"
                ),
            }
            if total > len(unparseable):
                warnings["detail_truncated"] = True
                warnings["detail_records_shown"] = len(unparseable)
            formatted["data_quality_warnings"] = warnings
        return formatted

    @staticmethod
    def _format_ingest_summary(state: dict) -> dict[str, Any]:
        summary = state.get("ingest_summary", {}) or {}
        return {"ingest_summary": summary}

    @staticmethod
    def _format_feedback_result(state: dict) -> dict[str, Any]:
        result = state.get("feedback_result", {}) or {}
        return {"feedback_result": result}

    @staticmethod
    def _derive_display_severity(advisory: dict[str, Any], signal_list: list[dict[str, Any]]) -> str:
        if advisory.get("severity"):
            return advisory["severity"]
        levels = [s.get("severity") for s in signal_list if s.get("severity")]
        order = ["low", "medium", "high", "critical"]
        ranked = [lv for lv in levels if lv in order]
        if not ranked:
            return "low"
        return max(ranked, key=order.index)

    # ── S-3 output gate extension ────────────────────────────────────────

    def _extra_security_gate_output(self, result: dict) -> dict:
        """docs/02_design.md §(c) S-3 output gate. Five ordered steps:
          1) assertive-language downgrade (断定表現の非断定化)
          2) fabricated evidence-ID detection (存在しない根拠 ID の「要検証」置換)
          3) per-sentence evidence-ID check (根拠 ID を伴わない実質的言明の除去/
             「要検証」置換) + control-action directive suppression
          4) mandatory disclaimer + expert consultation for critical severity
          5) formatted_output-wide client-echo sanitization (evidence-ID
             lookalike masking + control-instruction suppression + credential
             masking + length bound, applied recursively to every string in
             `formatted_output` EXCEPT the ones reached via an allowlisted
             key — see _SANITIZE_EXEMPT_KEYS / _sanitize_client_echoes)

        Order matters: step 2 must run before step 3 so a sentence whose ONLY
        citation was a fabricated ID (now "[要検証]") is treated as
        "no valid citation" by step 3, not left standing. Step 3 skips
        sentences it already replaced IN FULL, so the two steps do not
        double-redact — but a sentence that merely CONTAINS step 2's marker
        is still unsourced and is redacted by step 3.

        NFKC normalization + re.IGNORECASE matching gives the "NFKC + casefold"
        bypass-proofing required by docs/02_design.md §セキュリティ設計方針 S-3
        without lowercasing the displayed narrative.

        Step 5 is independent of steps 1-4 and runs UNCONDITIONALLY (for every
        mode: /invoke, /ingest, /feedback — not gated on "narrative" being
        present), covering ALL client-echoing structures in
        `formatted_output` (signal_index, data_quality_warnings.
        unreadable_observations, ingest_summary.error_rows/farm_id,
        feedback_result, and any field added later), which are untouched by
        the per-sentence narrative logic above (never folded into
        `narrative` — see _format_advisory). The framework's own S-3
        credential scan (FunctionNode._security_gate_output, @final) only
        scans TOP-LEVEL string values of `result`; `formatted_output` is a
        dict, so nested fields are invisible to it. Without step 5, a
        client-controlled field anywhere in formatted_output could echo a
        fabricated "sig-..."/"inc-..." citation, a control-action
        imperative, or a credential straight to the operator, fully
        unchecked — this is an ALLOWLIST (not a per-field denylist)
        specifically so a newly-added output field is protected by default
        rather than requiring a matching new sanitizer call each time.
        `narrative` itself is exempt from step 5 (own key in
        _SANITIZE_EXEMPT_KEYS) — it is fully handled by steps 1-4 below,
        which have their own sentence-level provenance semantics; running
        step 5 over it too would double-process it.
        """
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            return result

        formatted = _sanitize_client_echoes(formatted)
        result["formatted_output"] = formatted

        if "narrative" not in formatted:
            return result

        narrative = formatted.get("narrative", "") or ""
        valid_ids = set(formatted.get("cited_signal_ids", []) or []) | set(
            formatted.get("cited_incident_ids", []) or []
        )
        # None entries can appear when incident matching contributes no
        # matched_incident_id — do not let them count as "valid".
        valid_ids.discard(None)
        valid_ids_cf = {str(v).casefold() for v in valid_ids}

        # 1) assertive -> non-assertive rewrite. NFKC-normalize the display
        #    text first (full/half-width bypass defense); every pattern is
        #    re.IGNORECASE (equivalent to casefold-before-match) so case
        #    variants can't bypass, while original casing is preserved.
        rewritten = unicodedata.normalize("NFKC", narrative)
        for pattern, replacement in _ASSERTIVE_PATTERNS:
            rewritten = pattern.sub(replacement, rewritten)

        # 2) fabricated evidence-ID detection: any cited ID token not in
        #    valid_ids is replaced with "[要検証]". Casefolded comparison so
        #    ID casing differences (LLM "SIG-..." vs stored "sig-...") aren't
        #    false positives.
        cited_in_text = set(_EVIDENCE_ID_PATTERN.findall(rewritten.casefold()))
        fabricated = cited_in_text - valid_ids_cf
        for fake_id in fabricated:
            rewritten = re.sub(re.escape(fake_id), _REDACTION_MARKER_INVALID_ID, rewritten, flags=re.IGNORECASE)

        # 3) per-sentence evidence-ID check (docs/02_design.md §(c): "根拠 ID を
        #    伴わない言明は S-3 で機械的に除去または「要検証」に置換する").
        #    Allowlist: every sentence must cite a valid evidence ID unless it
        #    is agent boilerplate or a structural non-claim; anything else is
        #    replaced with a "[要検証]" marker rather than shown to the customer.
        #    Also suppress control-action directives (docs/02_design.md §(c):
        #    "自動対応...は一切実装しない") even when they cite a valid ID.
        rewritten = self._strip_unsupported_claims(rewritten, valid_ids_cf)

        formatted["narrative"] = rewritten

        # 4) mandatory disclaimer + expert consultation for critical severity.
        severity = formatted.get("severity")
        if severity == "critical" and _DISCLAIMER_JA.strip() not in rewritten:
            formatted["narrative"] = rewritten + _DISCLAIMER_JA

        result["formatted_output"] = formatted
        return result

    @staticmethod
    def _strip_unsupported_claims(narrative: str, valid_ids_cf: set[str]) -> str:
        """Sentence-level provenance enforcement (step 3 of the S-3 gate).

        Allowlist semantics: a sentence is emitted to the customer only if it
        proves its provenance. Concretely, for each sentence:
          - Keep it if it EXACTLY equals an agent-authored line (_PROTECTED_SENTENCES:
            the disclaimer, the degraded-mode framing) or a marker this gate already
            wrote in full. Exact equality, never substring — otherwise an LLM could
            prefix a protected phrase onto a claim and ride it past the gate.
          - Keep structural non-claims (headings, bullet labels) — recognized
            by shape, not by vocabulary.
          - Suppress control-action directives outright, cited or not
            (docs/02_design.md §(c): the agent never issues operational commands).
          - Otherwise REQUIRE at least one valid evidence ID. A sentence citing
            none — or citing only IDs that step 2 already invalidated — is
            replaced with a "[要検証]" marker.

        The default is redaction. An LLM cannot escape the gate by inventing a
        phrasing we failed to enumerate; it can only escape by citing a real
        signal_id / matched_incident_id, which is exactly the design intent.
        """
        sentences = _SENTENCE_SPLIT_PATTERN.split(narrative)
        out: list[str] = []
        for sentence in sentences:
            if not sentence.strip():
                out.append(sentence)
                continue

            # Exempt only EXACT agent-authored lines and sentences this gate
            # already replaced in full (idempotency). Both tests compare the
            # whole trimmed sentence, never a substring: prefixing a protected
            # phrase onto a claim, or leaving step 2's "[要検証]" marker inside
            # an otherwise unsourced sentence, must NOT buy an exemption.
            bare = sentence.strip().rstrip("。！？\n").strip()
            if bare in _PROTECTED_SENTENCES or bare in (
                _REDACTION_MARKER_NO_EVIDENCE,
                _REDACTION_MARKER_CONTROL,
            ):
                out.append(sentence)
                continue

            # Structural non-claim (heading, bullet label): carries no assertion.
            if _NON_CLAIM_FRAGMENT_PATTERN.match(sentence):
                out.append(sentence)
                continue

            # Control-action directives are never permitted (docs/02_design.md §(c)).
            control_suppressed = sentence
            for pattern, replacement in _CONTROL_ACTION_PATTERNS:
                control_suppressed = pattern.sub(replacement, control_suppressed)
            if control_suppressed != sentence:
                out.append(control_suppressed)
                continue

            # Everything else is a claim and must cite valid provenance.
            cited = {cid.casefold() for cid in _EVIDENCE_ID_PATTERN.findall(sentence)}
            if cited & valid_ids_cf:
                out.append(sentence)
            else:
                # No valid provenance — replace the claim, preserving the
                # trailing delimiter so the reassembled text still reads as
                # discrete sentences.
                delimiter = sentence[len(sentence.rstrip("。！？\n")):]
                out.append(_REDACTION_MARKER_NO_EVIDENCE + delimiter)
        return "".join(out)

    # NOTE: signal_index / data_quality_warnings / ingest_summary / etc. are no
    # longer sanitized by a per-structure method here. That approach (former
    # `_sanitize_signal_index`, a denylist of known fields) is exactly what
    # let `data_quality_warnings.unreadable_observations` leak fabricated
    # evidence IDs and control instructions unmasked — it lived in a
    # completely different code path (SignalDetectionNode._reject) that the
    # old per-field method never visited. See _sanitize_client_echoes /
    # _SANITIZE_EXEMPT_KEYS (module level) for the replacement: a single
    # allowlist-based recursive pass over the whole `formatted_output` tree,
    # invoked unconditionally in _extra_security_gate_output (step 5) so
    # newly-added fields are protected by default.
