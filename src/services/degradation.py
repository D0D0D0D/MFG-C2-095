"""AgentCore Platform v1.0"""

# MFG-C2-095 — degradation_reason accumulation.
#
# 🔴 A run can degrade for more than one reason at a time (the JMA fetch fails
# AND the LLM is unavailable). Each writer used to assign its own reason
# directly, so whichever wrote last erased the others and the operator learned
# about only one cause. Two places must accumulate rather than assign:
#   - AdvisorySynthesisNode, when Stage 3 degrades on top of an inner reason
#   - AssetWorkflowGraphNode.merge_output, where the INNER graph's reason
#     crosses back into the outer state that already carries pre_process's
#     reason. The inner graph never receives the outer reason, so combining
#     inside the node alone cannot cover this boundary.

from __future__ import annotations


def combine_degradation_reasons(existing: str | None, new_reason: str | None) -> str | None:
    """Merge two "+"-joined degradation reason strings, dropping duplicates.

    Either side may itself already be "+"-joined by an earlier stage. Order is
    preserved so the earliest-detected cause reads first.
    """
    if not existing:
        return new_reason or None
    if not new_reason:
        return existing
    reasons = existing.split("+")
    for reason in new_reason.split("+"):
        if reason and reason not in reasons:
            reasons.append(reason)
    return "+".join(reasons)
