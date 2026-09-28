"""AgentCore Platform v1.0"""

# Shared severity-priority list cap, used by every Stage 1/2 node that must
# bound a client-amplifiable list before it enters State/the response
# because state and response size can become a denial-of-service surface.
#
# SAFETY-CRITICAL CONSTRAINT (shared by every caller): capping must never
# cause the most dangerous item to be dropped from view. `apply_severity_cap`
# keeps the HIGHEST-SEVERITY items first (ties broken by a caller-supplied
# magnitude key descending, most dangerous first) rather than truncating in
# arrival order, so any downstream severity derivation that runs AFTER the
# cap (e.g. AdvisorySynthesisNode._derive_severity /
# PostProcessNode._derive_display_severity) always sees at least one instance
# of the true maximum severity across ALL items produced upstream. A naive
# "keep the first N" truncation would let an early run of low-severity items
# push a single late-arriving critical item out of the list entirely —
# silently downgrading a lethal advisory. That failure mode is exactly what
# this ordering rules out: max(severity of kept) == max(severity of all), by
# construction, for any cap >= 1.
#
# Originally introduced (as a private, non-reusable copy) in
# SignalDetectionNode._apply_signal_cap for `signal_list` (docs/02_design.md
# §予兆判定の3段パイプライン Stage 1). Extracted here so SignalMatchingNode's
# `candidate_risks` cap (Stage 2) shares the exact same ordering guarantee
# instead of re-implementing (and potentially drifting from) it — a Stage 2
# incidents_kb search re-amplifies signal_list into an UNBOUNDED
# candidate_risks list (one entry per matched incident), which bypasses the
# Stage 1 cap entirely if left unbounded on its own.

from __future__ import annotations

from typing import Any, Callable

_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def apply_severity_cap(
    items: list[dict[str, Any]],
    limit: int,
    *,
    severity_key: str = "severity",
    magnitude_fn: Callable[[dict[str, Any]], float] | None = None,
) -> list[dict[str, Any]]:
    """Bound `items` to `limit`, SAFETY-CRITICAL ordering (see module docstring).

    If `len(items) <= limit`, returns `items` unchanged (in original arrival
    order) — the common case never reorders anything.

    Otherwise, keeps the `limit` most dangerous items: sorted by
    `item[severity_key]` descending (critical > high > medium > low/unknown/
    absent), ties broken by `magnitude_fn(item)` descending (defaults to 0.0
    for every item, i.e. no tie-break beyond severity, when the caller has no
    meaningful magnitude — e.g. species-profile trigger matches, which carry
    no `deviation`). The kept subset is returned restored to ORIGINAL arrival
    order — capping changes WHICH items survive, never the order they are
    presented in.
    """
    if len(items) <= limit:
        return items

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, float]:
        _index, entry = item
        rank = _SEVERITY_RANK.get(entry.get(severity_key), -1)
        magnitude = magnitude_fn(entry) if magnitude_fn is not None else 0.0
        return (rank, magnitude)

    indexed = list(enumerate(items))
    indexed.sort(key=_sort_key, reverse=True)
    kept_indices = {index for index, _entry in indexed[:limit]}
    return [entry for index, entry in enumerate(items) if index in kept_indices]
