"""``MeasuredCompression.should_compress``: the decision's edges.

``test_tokens_are_the_servers_count.py`` covers the token threshold, its
parity with a tokenizer, the async twin and the tool-count threshold.
These pin the short-circuits around them.
"""

from __future__ import annotations

from types import SimpleNamespace

from igni_core.core.session.compression import MeasuredCompression

from .test_tokens_are_the_servers_count import message


def _tool(compressed: str | None = None) -> SimpleNamespace:
    m = message("tool", "result")
    m.compressed_content = compressed
    return m


def _heavy() -> list[SimpleNamespace]:
    """Far over any small token limit."""
    return [message("assistant", "a", read=50_000, wrote=0)]


def test_switched_off_never_compresses_whatever_the_limits() -> None:
    compression = MeasuredCompression(
        compress_tool_results=False, compress_token_limit=10, compress_tool_results_limit=1
    )

    assert not compression.should_compress(_heavy() + [_tool()], model=object())


def test_without_a_model_the_token_limit_is_ignored_but_the_tool_count_is_not() -> None:
    compression = MeasuredCompression(compress_token_limit=10, compress_tool_results_limit=2)

    assert not compression.should_compress(_heavy())
    assert not compression.should_compress(_heavy() + [_tool()])
    assert compression.should_compress(_heavy() + [_tool(), _tool()])


def test_a_result_that_is_already_compressed_is_not_counted() -> None:
    compression = MeasuredCompression(compress_tool_results_limit=2)

    assert not compression.should_compress([_tool("short"), _tool()])
    assert compression.should_compress([_tool("short"), _tool(), _tool()])


def test_exactly_at_the_tool_limit_compresses_and_one_under_does_not() -> None:
    compression = MeasuredCompression(compress_tool_results_limit=3)

    assert compression.should_compress([_tool(), _tool(), _tool()])
    assert not compression.should_compress([_tool(), _tool()])


def test_with_no_limit_at_all_nothing_compresses() -> None:
    compression = MeasuredCompression(compress_token_limit=None)
    # agno gives an unset tool limit a default at construction; it can still be cleared after.
    compression.compress_tool_results_limit = None

    assert not compression.should_compress(_heavy() + [_tool()] * 50, model=object())


def test_an_empty_conversation_does_not_compress() -> None:
    compression = MeasuredCompression(compress_token_limit=10, compress_tool_results_limit=1)

    assert not compression.should_compress([], model=object())


def test_the_tools_sent_with_the_request_count_toward_the_limit() -> None:
    compression = MeasuredCompression(compress_token_limit=1000)
    conversation = [message("user", "hi")]
    big_tool = SimpleNamespace(
        name="t", description="d" * 8000, parameters={"type": "object", "properties": {}}
    )

    assert not compression.should_compress(conversation, model=object())
    assert compression.should_compress(conversation, tools=[big_tool], model=object())
