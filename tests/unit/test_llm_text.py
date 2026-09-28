# MFG-C2-095 — Unit Tests: src/services/llm_text.complete_text()
#
# The one place that speaks the BaseLLM wire contract. Every malformed shape
# below was previously accepted and turned into "narrative text":
#   - a bare str return (the original defect: nodes called complete(prompt_str)
#     and used the result as text, which no shipped BaseLLM satisfies)
#   - a dict with no "content"      -> silently became ""
#   - a dict with a non-str content -> str()-coerced a whole object into prose
#   - an empty completion           -> recorded as a NON-degraded advisory
# Callers treat the raised error as an LLM failure and degrade.

import pytest

from src.services.llm_text import complete_text


class _Returns:
    def __init__(self, value):
        self.value = value
        self.seen: list = []

    def complete(self, messages):
        self.seen.append(messages)
        return self.value


class TestCompleteText:
    def test_sends_a_single_user_turn_as_a_message_list(self):
        llm = _Returns({"content": "ok", "tool_calls": [], "model": "m", "usage": {}})
        assert complete_text(llm, "PROMPT") == "ok"
        # The wire shape BaseLLM implementations actually receive.
        assert llm.seen == [[{"role": "user", "content": "PROMPT"}]]

    def test_bare_string_return_is_rejected(self):
        # AnthropicClient never returns a str; a double that does hides the bug.
        with pytest.raises(TypeError):
            complete_text(_Returns("just text"), "PROMPT")

    def test_missing_content_key_is_rejected(self):
        # Distinct from the empty-completion case below: a response with no
        # "content" key at all means the client is not a BaseLLM, while an
        # empty string means the model returned nothing. An operator debugging
        # a provider needs to tell those apart, so the message is asserted.
        with pytest.raises(ValueError, match="no 'content' key"):
            complete_text(_Returns({"tool_calls": [], "model": "m"}), "PROMPT")

    def test_non_string_content_is_rejected_not_stringified(self):
        # str()-coercion would put "{'secret': 'x'}" into the advisory prose.
        with pytest.raises(TypeError):
            complete_text(_Returns({"content": {"secret": "x"}}), "PROMPT")

    def test_empty_content_is_rejected(self):
        with pytest.raises(ValueError, match="empty 'content'"):
            complete_text(_Returns({"content": "   "}), "PROMPT")
