"""AgentCore Platform v1.0"""

# MFG-C2-095 — single place that speaks the BaseLLM wire contract.
#
# 🔴 Why this module exists (live LLM check, 2026-08-20):
#   The inner nodes used to call `self._llm.complete(prompt_str)` and use the
#   return value as a `str`. No shipped BaseLLM implementation satisfies that:
#   `shared.services.llm.base_llm.BaseLLM.complete(messages: list) -> dict`
#   returns the canonical dict `{"content": str, "tool_calls": [...],
#   "model": str, "usage": {...}}`. `AnthropicClient` — the client
#   `src/api/server.py` wires in production — forwards its argument straight to
#   `ChatAnthropic.invoke()` and returns that dict, so the "narrative text" the
#   node received was a dict, not a string.
#
#   Every unit/integration double in this repo exposed `.complete(prompt) -> str`,
#   matching the defect rather than the production contract, so the whole suite
#   stayed green. Routing all three call sites through this helper keeps the
#   contract in one auditable place and makes a non-conforming client fail loudly
#   at the seam instead of silently placing a dict into the advisory narrative.

from __future__ import annotations

from typing import Any


def complete_text(llm: Any, prompt: str) -> str:
    """Send `prompt` as a single user turn and return the assistant text.

    Raises TypeError when the injected client does not honour the BaseLLM
    return contract. Callers treat that as an LLM failure and degrade — the
    same path they already use for a provider error — rather than letting a
    non-string body reach the advisory narrative.
    """
    response = llm.complete([{"role": "user", "content": prompt}])
    if not isinstance(response, dict):
        raise TypeError(
            "BaseLLM.complete() must return the canonical response dict "
            f"(shared/services/llm/base_llm.py); got {type(response).__name__}."
        )
    if "content" not in response:
        raise ValueError("BaseLLM.complete() response has no 'content' key.")
    content = response["content"]
    if not isinstance(content, str):
        # str()-coercing a dict/list here would stringify a whole object into
        # the advisory narrative (e.g. "{'secret': 'x'}") and present it to the
        # reader as prose. Treat it as an LLM failure instead.
        raise TypeError(f"BaseLLM.complete() 'content' must be str; got {type(content).__name__}.")
    if not content.strip():
        # An empty completion is not a successful generation: callers would
        # otherwise record a disclaimer-only advisory as a NON-degraded result.
        raise ValueError("BaseLLM.complete() returned empty 'content'.")
    return content
