"""AgentCore Platform v1.0"""

# Stage 1 — signal detection (docs/02_design.md §予兆判定の3段パイプライン).
# DETERMINISTIC / NON-LLM: multi-year same-week deviation + species warning-range
# check + JMA warning presence. Same input -> same output, unit-testable.
#
# Runs inside the inner AquacultureWorkflowGraph (mode == "invoke" only).

from __future__ import annotations

import hashlib
import math
import re
import statistics
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.incident_store import BaseIncidentStore
from src.services.severity_cap import apply_severity_cap

_DEVIATION_SIGMA_THRESHOLD = 2.0

# Evidence-ID grammar. Must stay in sync with the ASCII-anchored extractor in
# `post_process_node._EVIDENCE_ID_PATTERN`: an ID containing anything outside
# this class fails S-3 provenance verification and gets its sentence redacted.
#
# `-` is EXCLUDED from the per-component safe set (mapped to `_`, same as any
# other unsafe character) even though ordinary inputs (`2026-07-01`,
# `farm-001`) contain it. This is deliberate, not an oversight — see
# `_id_component`'s docstring for the third collision/injection class this
# closes (a client-controlled component can otherwise smuggle a literal
# `sig-...`/`inc-...` lookalike, or a credential-shaped token, straight into
# the human-readable prefix of the real signal_id).
#
# Uniqueness is NOT derived from this sanitized prefix (see `_build_signal_id`
# / `_injective_digest`) — two earlier collision classes were found when
# uniqueness depended on it: (1) a sanitized value can be impersonated by
# clean input that happens to equal the sanitized+digest output, and (2) `-`
# being legal both inside a component and as the component separator made the
# join ambiguous (`("metric-a", "b")` vs `("metric", "a-b")`). Both are closed
# by hashing an unambiguous length-prefixed encoding of the RAW tuple instead
# — that guarantee is untouched by excluding `-` from the readable prefix.
_ID_SAFE_PATTERN = re.compile(r"[^0-9A-Za-z_]")

# Upper bound on each component's contribution to the human-readable prefix,
# applied AFTER `_ID_SAFE_PATTERN` sanitization. The prefix is decorative only
# (see `_id_component`/`_build_signal_id`), so this exists purely to keep a
# client-controlled field (pond_id, metric, observed_at, farm_id) from
# dumping an arbitrarily long, credential-shaped token (e.g. a 40+ char
# `sk-ant-...`-style string with its `-` stripped to `skant...`) into every
# response, log line, and Stage-3 prompt verbatim. 16 was chosen as generous
# headroom over every real-world component observed in this domain (dates
# `20260701` = 8 chars, farm/pond codes typically <= 12 chars) while still
# visibly mangling anything secret-shaped long before 40+ chars survive.
_ID_COMPONENT_MAX_CHARS = 16

# Client-controlled strings are persisted to the checkpoint DB and echoed in every
# response. Bound both the per-field length and the number of records kept.
_MAX_FIELD_CHARS = 200
_MAX_REJECTED_RECORDED = 50

# Upper bound on the number of signals carried past Stage 1 into Stage 2/3 and
# ultimately into State/the response. Without this, a client sending tens of
# thousands of observations in one /invoke produces a `signal_list` that is
# fully echoed into `advisory_output.narrative` (one line per signal, Stage 3
# deterministic-fallback path) and `advisory_output.cited_signal_ids` (one
# entry per signal) — a 20,000-observation payload was measured to produce an
# 11.5MB response, all of which is also written verbatim to the checkpoint DB
# on every hop. Bounding both state and response size prevents amplification.
#
# 500 is chosen as generous headroom over any plausible single-farm daily
# observation volume (docs/02_design.md's reference deployment is O(10-100)
# sensor readings per farm per day) while keeping the worst-case narrative/
# response size in the tens-of-KB range instead of megabytes.
#
# SAFETY-CRITICAL CONSTRAINT: capping must never cause a lethal signal to be
# dropped from view. `_apply_signal_cap` enforces this by keeping the
# HIGHEST-SEVERITY signals first (ties broken by |deviation| descending, most
# dangerous first) rather than truncating in arrival order — so
# AdvisorySynthesisNode._derive_severity(), which runs AFTER this cap on the
# already-capped list, always sees at least one instance of the true maximum
# severity across ALL signals Stage 1 produced. A naive "keep the first N"
# truncation would let an early run of low-severity signals push a single
# late-arriving critical signal out of the list entirely — silently
# downgrading a lethal advisory to "low". That failure mode is exactly what
# this ordering rules out: max(severity of kept) == max(severity of all),
# by construction, for any cap >= 1.
_MAX_SIGNALS = 500


def _truncate(raw: Any, limit: int = _MAX_FIELD_CHARS) -> Any:
    """Bound a client-controlled string before it enters State."""
    if not isinstance(raw, str) or len(raw) <= limit:
        return raw
    return raw[:limit] + f"...[truncated, {len(raw)} chars]"


def _safe_field_repr(raw: Any) -> Any:
    """Bound a client-controlled identity/label field (metric/observed_at/
    pond_id) before it enters State, INCLUDING the case where the client sent
    something other than a scalar (list/dict) for it.

    `_truncate` alone is not enough here: it passes non-str values through
    UNCHANGED, so a rejected observation whose `observed_at` is a deeply
    nested list (the malformed_field_type case below) would otherwise still
    carry that raw, arbitrarily-deep structure into `unparseable_observations`
    -> State -> the checkpoint DB, relying entirely on
    post_process._sanitize_client_echoes' depth cap to keep it from crashing
    downstream. Collapse anything non-scalar to a short, depth-free
    description at the SOURCE instead — belt-and-suspenders with the
    post_process depth cap, not a replacement for input rejection.
    """
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return _truncate(raw)
    return f"<{type(raw).__name__}, rejected: not a scalar>"


class SignalDetectionNode(FunctionNode):
    """Stage 1: produce a deterministic list of deviation signals."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, incident_store: BaseIncidentStore) -> None:
        super().__init__()
        self._store = incident_store

    def execute(self, state: dict) -> dict:
        farm_id = state.get("farm_id", "")
        species_profile = state.get("species_profile", {}) or {}
        payload = state.get("payload", {}) or {}
        current_observations: list[dict[str, Any]] = payload.get("current_observations", [])
        forecast_context = state.get("forecast_context", {}) or {}

        signals: list[dict[str, Any]] = []

        # ① multi-year same-week deviation + ② species warning-range check
        unparseable: list[dict[str, Any]] = []
        rejected_total = 0

        def _reject(obs: dict[str, Any], reason: str) -> None:
            """Record a rejected observation. NEVER `continue` without calling this:
            an observation Stage 1 cannot assess is a data-quality defect, not
            evidence of safety.

            Every field here is client-controlled and is persisted to the
            checkpoint DB, so each is truncated: an unbounded `repr()` of a 10MB
            string value would bloat state and every response, so both dimensions
            must remain bounded.
            """
            nonlocal rejected_total
            rejected_total += 1
            # Cap the recorded detail, never the COUNT — silently capping the
            # count would reintroduce exactly the silent-drop bug this guards.
            if len(unparseable) >= _MAX_REJECTED_RECORDED:
                return
            unparseable.append(
                {
                    "metric": _safe_field_repr(obs.get("metric")),
                    "observed_at": _safe_field_repr(obs.get("observed_at", "")),
                    "pond_id": _safe_field_repr(obs.get("pond_id")),
                    "value": _truncate(repr(obs.get("value"))),
                    "reason": reason,
                }
            )

        for obs in current_observations:
            metric = obs.get("metric")
            raw_value = obs.get("value")
            observed_at = obs.get("observed_at", "")
            pond_id_raw = obs.get("pond_id")

            if not metric:
                _reject(obs, "missing_metric")
                continue
            if raw_value is None:
                _reject(obs, "missing_value")
                continue
            if not observed_at:
                _reject(obs, "missing_observed_at")
                continue

            # `metric` / `observed_at` / `pond_id` are identity/label fields and
            # must be scalars (str). A client can send an arbitrarily deeply
            # nested list/dict here instead (e.g. observed_at=[[[...]]]) — this
            # flows unchanged into signal_list/signal_index, and
            # post_process._sanitize_client_echoes() recurses into it without a
            # depth bound, so a ~1000-level-deep structure blows the Python
            # recursion limit (RecursionError) and kills the WHOLE invocation —
            # a remote DoS. Reject non-scalar identity fields at the boundary,
            # the same "unreadable -> report, never silently score" contract as
            # _coerce_value for `value`. (Depth-limiting the sanitizer itself is
            # the second, independent layer of defense — see
            # post_process_node._sanitize_client_echoes.)
            if not isinstance(metric, str):
                _reject(obs, "malformed_field_type")
                continue
            if not isinstance(observed_at, str):
                _reject(obs, "malformed_field_type")
                continue
            if pond_id_raw is not None and not isinstance(pond_id_raw, str):
                _reject(obs, "malformed_field_type")
                continue

            # Coerce at the boundary. A reading that cannot be interpreted as a
            # finite number MUST NOT be silently skipped: this agent exists to catch
            # extreme values, and a JSON payload carrying "38.0" as a string
            # would otherwise vanish from BOTH the range check and the historical
            # deviation check (which filters on isinstance(..., (int, float))).
            value = self._coerce_value(raw_value)
            if value is None:
                _reject(obs, "unreadable_value")
                continue

            metric_profile = (species_profile.get("metrics") or {}).get(metric)
            # An unconfigured metric cannot be range-checked. Historical deviation
            # needs >=3 prior points, so the first readings of an unknown/misspelled
            # metric would score "no signal" however lethal they are. Report it.
            if not metric_profile:
                _reject(obs, "metric_not_in_species_profile")

            # Range-based severity from the species profile (non-LLM lookup).
            range_severity = self._range_severity(value, metric_profile)

            # Historical deviation from the farm's own multi-year record.
            history = self._store.query_env_observations(
                farm_id=farm_id, pond_id=obs.get("pond_id"), metric=metric, since_years=5
            )
            deviation, deviation_severity = self._historical_deviation(value, history)

            severity = self._max_severity(range_severity, deviation_severity)
            if severity is None:
                continue

            # `pond_id` is part of the identity. Without it two ponds sampled on
            # the same date collide onto one signal_id — and since Stage 3 cites
            # signal_ids as evidence, a heat kill in pond A and a cold kill in
            # pond B would render as two identical advisory lines citing the same
            # ID, with no way for the operator to tell them apart. Deterministic
            # (docs/02_design.md §Stage 1: same input -> same output).
            pond_id = obs.get("pond_id")
            signal_id = self._build_signal_id(metric, observed_at, farm_id, pond_id)
            signals.append(
                {
                    "signal_id": signal_id,
                    "metric": metric,
                    "value": value,
                    "observed_at": observed_at,
                    # The human-readable label is kept here, verbatim and unsanitized.
                    # signal_id carries only the ASCII-safe form.
                    "pond_id": pond_id,
                    "deviation": deviation,
                    "severity": severity,
                    "source": "range_and_history",
                }
            )

        # ③ public warning presence (JMA forecast/bosai — already summarized upstream)
        if forecast_context.get("bosai_feed_available") is False:
            pass  # feed unavailable is a degradation signal, not a risk signal
        if payload.get("jma_warning_active"):
            jma_observed_at = payload.get("jma_warning_observed_at", "")
            signals.append(
                {
                    # Built through the same injective _build_signal_id as the
                    # range/history signals above (not a hand-rolled f-string) so
                    # this branch gets the identical uniqueness guarantee and
                    # cannot collide with a range/history ID that happens to
                    # share farm_id/observed_at — "jma_warning" is baked into the
                    # digest input as the `metric` component, keeping the
                    # namespaces disjoint.
                    "signal_id": self._build_signal_id("jma_warning", jma_observed_at, farm_id, None),
                    "metric": "jma_warning",
                    "value": True,
                    "observed_at": jma_observed_at,
                    "pond_id": None,
                    "deviation": None,
                    "severity": "critical",
                    "source": "jma_bosai_feed",
                }
            )

        total_signal_count = len(signals)
        capped_signals = self._apply_signal_cap(signals)

        emit_trace_event(
            "stage1_signal_detection",
            {
                "farm_id": farm_id,
                "signal_count": len(capped_signals),
                "signal_count_total": total_signal_count,
                "unparseable_observation_count": rejected_total,
            },
            state,
        )

        result: dict[str, Any] = {
            "signal_list": capped_signals,
            # Surfaced, never swallowed: an observation we could not read is a data
            # quality defect the operator must see, not an absence of risk.
            "unparseable_observations": unparseable,
            # The TRUE count, even when only the first _MAX_REJECTED_RECORDED
            # records are retained.
            "unparseable_observation_count": rejected_total,
            # The TRUE signal count, even when signal_list itself is capped to
            # _MAX_SIGNALS (state/response-size bound). Same disclosure contract
            # as unparseable_observation_count above: capping detail is fine,
            # hiding the fact that capping happened is not.
            "signal_list_total_count": total_signal_count,
            "status": AgentStatus.SUCCESS.value,
        }
        if total_signal_count > len(capped_signals):
            result["signal_list_truncated"] = True
        return result

    # ── helpers (pure functions — unit-testable in isolation) ──────────────

    @staticmethod
    def _id_component(raw: Any) -> str:
        """Reduce an ID component to the evidence-ID grammar `[0-9A-Za-z_]+`,
        for READABILITY ONLY. This value alone must never be trusted to keep two
        different components distinct — see `_build_signal_id` for the actual
        uniqueness guarantee (a digest over an unambiguous encoding of the raw
        tuple). Three independent collision/injection classes were found in
        this sanitizer and none is fixable by tweaking the replacement rule
        alone:

        1. A "sanitized" value can impersonate a "clean" one. `_ID_SAFE_PATTERN`
           previously appended a digest suffix only when a character had to be
           rewritten — but nothing stopped an attacker from supplying the digest
           suffix directly as clean input: `"生簀A"` sanitizes to `"__A_<digest>"`,
           and the literal string `"__A_<digest>"` is already clean, so both
           produced the identical signal_id. Any conditional-on-rewrite digest
           scheme has this hole; the fix is to stop relying on this function for
           uniqueness at all.
        2. Component-boundary ambiguity. `-` was allowed *inside* a component
           (ordinary inputs like `2026-07-01`, `farm-001` need it) but was also
           used to *join* components, so `("metric-a", "b")` and
           `("metric", "a-b")` both joined to `"metric-a-b"`. No single-character
           separator choice fixes this in general when the separator is also a
           legal in-component character. (This is why `_ID_SAFE_PATTERN` no
           longer allows `-` inside a component at all — see below.)
        3. Fake-evidence-ID / credential smuggling. `pond_id` (and `metric` /
           `observed_at` / `farm_id`) is client-controlled and lands verbatim,
           once ASCII-filtered, in the human-readable prefix of the REAL
           `signal_id` — which Stage 3 cites as evidence and which the S-3
           gate treats as provenance-safe (`post_process_node._sanitize_client_echoes`
           exempts the `signal_id` key by design, since its uniqueness comes
           from `_injective_digest`, not this prefix). Before
           this fix, a `pond_id` of `"根拠: sig-FAKE-999。...sk-ant-api03-SECRET"`
           sanitized to `..._sig-FAKE-999_..._sk-ant-api03-SECRET` — `-` was
           preserved, so the literal substring `sig-FAKE-999` (and a
           credential-shaped token) survived intact inside the real
           `signal_id`, embedded in `output.narrative`,
           `output.cited_signal_ids`, and `output.signal_index[].signal_id`.
           An operator or downstream system could then mistake it for a
           genuine citation. Excluding `-` from the safe set (mapped to `_`
           like any other unsafe character) means a component can never
           reproduce the literal substring `sig-` or `inc-` required by
           `_EVIDENCE_ID_PATTERN`, regardless of what the client supplies.
           `_ID_COMPONENT_MAX_CHARS` additionally bounds how much of a long
           credential-shaped string can survive at all.

        `_build_signal_id` closes all three holes; this function now exists
        purely to keep the human-readable prefix pleasant to read, and is
        deliberately strict about what "pleasant to read" is allowed to mean.
        """
        text = "" if raw is None else str(raw)
        return _ID_SAFE_PATTERN.sub("_", text)[:_ID_COMPONENT_MAX_CHARS]

    @staticmethod
    def _injective_digest(*raw_parts: Any) -> str:
        """A digest that is injective over the input TUPLE (not just its
        concatenation), by length-prefixing every component before hashing.

        Concatenating `str(len(s))` + NUL + `s` for each part, in order, and then
        hashing the result is a standard unambiguous (self-delimiting) encoding:
        two different tuples can only hash to the same digest via an actual
        SHA-256 collision, never via a join-boundary ambiguity — because the
        decoder (conceptually) can always tell where one component's length
        prefix ends and its payload begins, and payload length is explicit
        rather than inferred from a separator character that might also appear
        inside the payload.

        `None` is encoded as a value distinct from any string (including the
        empty string), so `pond_id=None` cannot collide with `pond_id=""`.

        Full 64 hex chars (256 bits) of SHA-256 are kept — NOT truncated.
        `signal_id` is a safety-critical provenance identifier cited by Stage 3
        and trusted by the S-3 gate (`_EVIDENCE_ID_PATTERN`); an attacker who
        controls one or more of the raw input fields (metric/observed_at/
        farm_id/pond_id) could otherwise mount a birthday attack against a
        truncated digest. A prior 16-hex-char (64-bit) truncation made this
        feasible in ~2^32 attempts (GPU-feasible in minutes); the full 256-bit
        digest makes a collision attack (~2^128 work) computationally
        infeasible. `hashlib.sha256(...).hexdigest()` is all lowercase hex
        (`[0-9a-f]`), which is a strict subset of `_ID_SAFE_PATTERN`'s allowed
        `[0-9A-Za-z_]` and of `_EVIDENCE_ID_PATTERN`'s `[0-9A-Za-z_-]` body, so
        widening the digest cannot introduce a character that breaks the
        evidence-ID grammar or the S-3 gate's `sig-.../inc-...` match.
        """
        buf = bytearray()
        for part in raw_parts:
            if part is None:
                buf += b"N"
            else:
                text = str(part).encode("utf-8")
                buf += b"S" + str(len(text)).encode("ascii") + b"\x00" + text
        return hashlib.sha256(bytes(buf)).hexdigest()

    @classmethod
    def _build_signal_id(cls, metric: Any, observed_at: Any, farm_id: Any, pond_id: Any) -> str:
        """Deterministic, ASCII-safe, INJECTIVE evidence ID.

        Uniqueness is carried entirely by `_injective_digest` over the raw
        (unsanitized) component tuple — length-prefixed encoding rules out both
        known collision classes (see `_id_component` docstring) by construction,
        independent of what characters the client-supplied strings contain.
        This function must keep passing the RAW tuple to `_injective_digest`
        (never the sanitized/truncated `readable` components) — the digest is
        the only source of the injectivity guarantee.

        The sanitized, length-capped components are still included as a
        human-readable prefix (kept for operators skimming logs/prompts), but
        the prefix is decorative only: even if two different tuples produced
        the identical sanitized prefix, they still produce different digests
        and therefore different signal_ids. Every input to the digest is the
        ORIGINAL value, including `pond_id` when absent (`None` vs `""` are
        kept distinct).

        The prefix is also SECURITY-RELEVANT despite being decorative for
        uniqueness: `_id_component` forbids `-` inside a component (mapping it
        to `_`) specifically so that no client-controlled field can smuggle a
        literal `sig-`/`inc-` lookalike substring — or a long credential-shaped
        token — into the real signal_id that Stage 3 cites and that the S-3
        gate trusts as provenance (see `_id_component` collision class 3).
        Components are still joined with `-` here; that is safe only because
        no individual component can itself contain `-`, so the join points are
        the only `-` characters in the whole readable prefix.
        """
        readable = [cls._id_component(metric), cls._id_component(observed_at), cls._id_component(farm_id)]
        if pond_id:
            readable.append(cls._id_component(pond_id))
        digest = cls._injective_digest(metric, observed_at, farm_id, pond_id)
        return "sig-" + "-".join(readable) + "-" + digest

    @staticmethod
    def _coerce_value(raw: Any) -> float | None:
        """Interpret an observation value as a finite float, or None if impossible.

        `bool` is rejected explicitly: it is a subclass of `int`, so `True` would
        otherwise coerce to 1.0 and be scored as a real reading.

        Non-finite values are rejected. `float("nan")` parses successfully, and NaN
        compares False against EVERY threshold — so a NaN reading would score as
        "no signal" while never being recorded as unparseable: the same silent-drop
        failure this module exists to prevent. `json.loads` accepts the bare literals
        `NaN` / `Infinity` / `-Infinity`, so this is reachable from a real payload.
        `inf` is likewise a broken sensor, not a measurement, and must not be scored
        (it would otherwise land in an advisory as a genuine `critical` signal).

        Callers MUST treat a None return as "unparseable — report it", never as
        "no signal". See `execute()`'s `unparseable_observations`.
        """
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
        elif isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                return None
        else:
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _range_severity(value: float, metric_profile: dict[str, Any] | None) -> str | None:
        """Score an observation against the species profile's alarm limits.

        Limits follow the OPC UA `LimitAlarmType` convention (OPC 10000-9 §5.8.11):
        four **scalar thresholds**, each optional, ordered

            low_low_limit <= low_limit <= high_limit <= high_high_limit

        and a reading is scored by how far past them it sits::

            value < low_low_limit   or  value > high_high_limit  -> "critical"
            value < low_limit       or  value > high_limit       -> "medium"
            otherwise                                            -> None (no signal)

        Omit a limit to say "no danger zone on that side": `turbidity` declares only
        the `high_*` pair, `dissolved_oxygen` only the `low_*` pair, `water_temp` all
        four. `normal_range` is retained for display/explanation only and is NOT
        consulted here.

        Why thresholds instead of the previous `normal_range`/`warning_range`/
        `critical_range` intervals: intervals force every profile to answer questions
        the domain does not ask — whether the bands nest, abut, or leave a gap — and
        the four species profiles answered inconsistently (madai `water_temp` nested,
        the other 17 metrics did not; `salinity` left the gap (15,20) in no band; the
        one-sided metrics needed a sentinel `999.0` upper edge). Worse, an interval
        invites the question "what if the value is outside even `critical_range`?",
        and the original code answered it with `return None  # data anomaly`. That
        inverted the severity ladder: madai at 31°C scored "critical" while 34°C and
        38°C — lethal for the species — produced no signal at all, and the agent
        answered "no notable deviation detected". With scalar limits the concept of
        "outside the critical band" does not exist, so the failure is unrepresentable.

        Comparisons are STRICT: a reading exactly equal to a limit is not a signal.
        The limit is the first value of the danger zone's boundary, not inside it.

        `value` must already be a float — coerce with `_coerce_value()` at the call
        site. A malformed limit in the profile raises `TypeError` rather than being
        swallowed: a config error must never be indistinguishable from "safe".

        Sensor-fault rejection is deliberately NOT done here; if it is ever needed it
        belongs in an explicit `plausible_range`, never in a silent early return that
        suppresses exactly the extreme readings this agent exists to catch.
        """
        if not metric_profile:
            return None
        low_low = metric_profile.get("low_low_limit")
        low = metric_profile.get("low_limit")
        high = metric_profile.get("high_limit")
        high_high = metric_profile.get("high_high_limit")

        if low_low is not None and value < low_low:
            return "critical"
        if high_high is not None and value > high_high:
            return "critical"
        if low is not None and value < low:
            return "medium"
        if high is not None and value > high:
            return "medium"
        return None

    @staticmethod
    def _historical_deviation(
        value: float, history: list[dict[str, Any]]
    ) -> tuple[float | None, str | None]:
        # A single NaN in stored history poisons `mean` to NaN — which would
        # disable deviation detection for every future reading of this metric —
        # and makes `pstdev` raise an opaque AttributeError. bool is excluded for
        # the same reason as in `_coerce_value`.
        values: list[float] = [
            float(h["value"])
            for h in history
            if isinstance(h.get("value"), (int, float))
            and not isinstance(h["value"], bool)
            and math.isfinite(h["value"])
        ]
        if len(values) < 3:
            return None, None
        mean = statistics.mean(values)
        stdev = statistics.pstdev(values) or 1e-9
        deviation = (value - mean) / stdev
        if abs(deviation) >= _DEVIATION_SIGMA_THRESHOLD:
            return round(deviation, 2), "high" if abs(deviation) >= 3.0 else "medium"
        return round(deviation, 2), None

    @staticmethod
    def _max_severity(a: str | None, b: str | None) -> str | None:
        order = {"medium": 1, "high": 2, "critical": 3}
        candidates = [s for s in (a, b) if s]
        if not candidates:
            return None
        return max(candidates, key=lambda s: order.get(s, 0))

    @staticmethod
    def _apply_signal_cap(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound signal_list to `_MAX_SIGNALS`, SAFETY-CRITICAL ordering.

        Delegates to the shared `apply_severity_cap` helper (src/services/
        severity_cap.py) — see that module's docstring for the full ordering
        rationale (severity descending, ties broken by |deviation|
        descending). Extracted there so SignalMatchingNode's `candidate_risks`
        cap (Stage 2) shares the exact same guarantee instead of drifting.

        This guarantees, for ANY cap >= 1: max(severity of kept signals) ==
        max(severity of ALL signals). AdvisorySynthesisNode._derive_severity()
        runs on the returned (capped) list, so the top-level advisory severity
        can never be silently downgraded by truncation — a critical signal is
        dropped from the DETAIL list only after every other critical/high/
        medium signal has already been dropped, never before. See the
        `_MAX_SIGNALS` docstring for the incident this fixes (11.5MB response
        from 20,000 observations) and why naive first-N truncation is unsafe.
        """

        def _deviation_magnitude(sig: dict[str, Any]) -> float:
            deviation = sig.get("deviation")
            return abs(deviation) if isinstance(deviation, (int, float)) else 0.0

        return apply_severity_cap(
            signals, _MAX_SIGNALS, severity_key="severity", magnitude_fn=_deviation_magnitude
        )

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
