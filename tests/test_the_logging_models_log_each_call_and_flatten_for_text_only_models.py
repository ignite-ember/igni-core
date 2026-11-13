"""``LoggingModel`` and ``LoggingClaude``: what each call logs and hands on.

Every ``invoke``, ``ainvoke``, ``invoke_stream`` and ``ainvoke_stream`` goes
through the injected call logger (method, model id, how many messages, whether
it streams, where it goes) and then to Agno's provider class. A model that is
not ``vision`` is handed text only; one that is, is handed the content as it
was. The provider classes' own network calls are replaced here by recorders:
what is under test is what igni's wrappers log and pass on.
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from agno.models.anthropic import Claude
from agno.models.openai.like import OpenAILike
from agno.models.response import ModelResponse
from agno.run.agent import CustomEvent

from igni_core.core.config.logging_claude import LoggingClaude
from igni_core.core.config.logging_model import LoggingModel

IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


class RecordingLogger:
    """Stands in for ``LlmCallLogger``: keeps what each call was logged as."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def log_call(self, method: str, **fields: Any) -> None:
        self.calls.append({"method": method, **fields})


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "look"}, IMAGE]},
        {"role": "assistant", "content": "plain"},
    ]


def _install_recorders(monkeypatch: pytest.MonkeyPatch, provider: type) -> list[tuple]:
    """Replace the provider class's four calls with ones that record their arguments."""
    seen: list[tuple] = []

    def invoke(self, *args, **kwargs):
        seen.append(("invoke", args, kwargs))
        return "reply"

    async def ainvoke(self, *args, **kwargs):
        seen.append(("ainvoke", args, kwargs))
        return "reply"

    def invoke_stream(self, *args, **kwargs):
        seen.append(("invoke_stream", args, kwargs))
        yield ModelResponse(content="chunk")

    async def ainvoke_stream(self, *args, **kwargs):
        seen.append(("ainvoke_stream", args, kwargs))
        yield ModelResponse(content="chunk")

    for name, fn in (
        ("invoke", invoke),
        ("ainvoke", ainvoke),
        ("invoke_stream", invoke_stream),
        ("ainvoke_stream", ainvoke_stream),
    ):
        monkeypatch.setattr(provider, name, fn, raising=False)
    return seen


def _openai_like(logger: RecordingLogger, **kwargs: Any) -> LoggingModel:
    return LoggingModel(logger=logger, id="m-1", api_key="k", **kwargs)  # type: ignore[arg-type]


def _claude(logger: RecordingLogger, **kwargs: Any) -> LoggingClaude:
    return LoggingClaude(logger=logger, id="claude-x", api_key="k", **kwargs)  # type: ignore[arg-type]


MODELS = [
    pytest.param(_openai_like, OpenAILike, "https://llm.example/v1", id="openai-like"),
    pytest.param(_claude, Claude, "https://claude.example", id="claude"),
]


def _build(make, url: str, logger: RecordingLogger, **kwargs: Any):
    model = make(logger, **kwargs)
    if make is _openai_like:
        model.base_url = url
    else:
        model.client_params = {"base_url": url}
    return model


async def _drain_async(gen) -> list:
    return [chunk async for chunk in gen]


@pytest.mark.parametrize(("make", "provider", "url"), MODELS)
class TestEachCallIsLogged:
    async def test_all_four_calls_log_their_method_and_stream_flag(
        self, monkeypatch, make, provider, url
    ) -> None:
        logger = RecordingLogger()
        _install_recorders(monkeypatch, provider)
        model = _build(make, url, logger)

        model.invoke(_messages())
        await model.ainvoke(_messages())
        stream = model.invoke_stream(_messages())
        next(stream)
        stream.close()
        astream = model.ainvoke_stream(_messages())
        await astream.__anext__()
        await astream.aclose()

        assert [(c["method"], c["stream"]) for c in logger.calls] == [
            ("invoke", False),
            ("ainvoke", False),
            ("invoke_stream", True),
            ("ainvoke_stream", True),
        ]
        assert all(c["model_id"] == model.id and c["n_messages"] == 2 for c in logger.calls)
        assert all(c["url"] == url for c in logger.calls)

    def test_messages_given_by_keyword_are_counted_and_none_is_zero(
        self, monkeypatch, make, provider, url
    ) -> None:
        logger = RecordingLogger()
        _install_recorders(monkeypatch, provider)
        model = _build(make, url, logger)

        model.invoke(messages=_messages(), assistant_message=None)
        model.invoke()

        assert [c["n_messages"] for c in logger.calls] == [2, 0]

    def test_the_arguments_reach_the_provider_unchanged_besides_the_text_flattening(
        self, monkeypatch, make, provider, url
    ) -> None:
        logger = RecordingLogger()
        seen = _install_recorders(monkeypatch, provider)
        model = _build(make, url, logger)

        assert model.invoke(_messages(), assistant_message="a", tools=None) == "reply"

        (_, args, kwargs) = seen[0]
        assert kwargs == {"assistant_message": "a", "tools": None}
        assert [m["content"] for m in args[0]] == ["look", "plain"]

    def test_a_vision_model_is_handed_the_content_as_it_was(
        self, monkeypatch, make, provider, url
    ) -> None:
        logger = RecordingLogger()
        seen = _install_recorders(monkeypatch, provider)
        model = _build(make, url, logger, vision=True)
        messages = _messages()

        model.invoke(messages)

        assert seen[0][1][0][0]["content"] == [{"type": "text", "text": "look"}, IMAGE]

    async def test_the_streams_are_flattened_too(self, monkeypatch, make, provider, url) -> None:
        logger = RecordingLogger()
        seen = _install_recorders(monkeypatch, provider)
        model = _build(make, url, logger)

        stream = model.invoke_stream(_messages())
        next(stream)
        stream.close()
        astream = model.ainvoke_stream(_messages())
        await astream.__anext__()
        await astream.aclose()

        assert [s[1][0][0]["content"] for s in seen] == ["look", "look"]


def test_an_openai_like_model_with_no_base_url_logs_default(monkeypatch) -> None:
    logger = RecordingLogger()
    _install_recorders(monkeypatch, OpenAILike)
    model = _openai_like(logger)
    model.base_url = None

    model.invoke(_messages())

    assert logger.calls[0]["url"] == "default"


def test_a_claude_with_no_client_params_logs_default_and_reports_it(monkeypatch) -> None:
    logger = RecordingLogger()
    _install_recorders(monkeypatch, Claude)
    model = _claude(logger)
    model.client_params = None

    assert model.base_url == "default"
    model.invoke(_messages())
    assert logger.calls[0]["url"] == "default"

    model.client_params = {"base_url": ""}
    assert model.base_url == "default"
    model.client_params = {"base_url": "https://claude.example"}
    assert model.base_url == "https://claude.example"


def _tool_chunk(index: int, *, call_id: str | None, name: str | None, args: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            {
                "index": index,
                "id": call_id,
                "function": {"name": name, "arguments": args},
            }
        ]
    )


def _deltas(events: list) -> list[tuple[str, str, str]]:
    return [
        (e.tool_call_id, e.tool_name, e.arguments_partial)
        for e in events
        if isinstance(e, CustomEvent) and e.event == "tool_call_input_delta"
    ]


async def test_tool_argument_bytes_are_interleaved_as_progressive_events(monkeypatch) -> None:
    chunks = [
        _tool_chunk(0, call_id="c1", name="visualize", args=""),
        _tool_chunk(0, call_id=None, name=None, args='{"spec":'),
        ModelResponse(content="text"),
        _tool_chunk(0, call_id=None, name=None, args=' {"a": 1}}'),
    ]

    def process(self, *args, **kwargs):
        yield from chunks

    async def aprocess(self, *args, **kwargs):
        for c in chunks:
            yield c

    monkeypatch.setattr(OpenAILike, "process_response_stream", process, raising=False)
    monkeypatch.setattr(OpenAILike, "aprocess_response_stream", aprocess, raising=False)
    model = _openai_like(RecordingLogger())

    sync_out = list(model.process_response_stream())
    async_out = await _drain_async(model.aprocess_response_stream())

    for out in (sync_out, async_out):
        assert [c for c in out if isinstance(c, ModelResponse)] == chunks
        assert _deltas(out) == [
            ("c1", "visualize", '{"spec":'),
            ("c1", "visualize", '{"spec": {"a": 1}}'),
        ]
        # each event follows the chunk that carried its bytes
        assert [type(o) is CustomEvent for o in out] == [False, False, True, False, False, True]


async def test_each_stream_accumulates_apart_from_the_others(monkeypatch) -> None:
    def process(self, *args, **kwargs):
        yield _tool_chunk(0, call_id="c", name="t", args="x")

    monkeypatch.setattr(OpenAILike, "process_response_stream", process, raising=False)
    model = _openai_like(RecordingLogger())

    first = _deltas(list(model.process_response_stream()))
    second = _deltas(list(model.process_response_stream()))

    assert first == second == [("c", "t", "x")]


class TestTheClaudeClient:
    async def test_the_builders_httpx2_client_is_handed_to_the_sdk(self) -> None:
        http = httpx2.AsyncClient()
        model = _claude(RecordingLogger(), http_client=http)

        client = model.get_async_client()

        assert client._client is http
        await http.aclose()

    async def test_an_open_client_is_reused_and_a_closed_one_replaced(self) -> None:
        http = httpx2.AsyncClient()
        model = _claude(RecordingLogger(), http_client=http)

        first = model.get_async_client()
        assert model.get_async_client() is first

        await first.close()
        second = model.get_async_client()

        assert second is not first
        assert second is model.async_client
        await second.close()

    async def test_a_client_that_is_not_httpx2_is_left_to_the_sdk(self) -> None:
        stranger = object()
        model = _claude(RecordingLogger(), http_client=stranger)

        client = model.get_async_client()

        assert client._client is not stranger
        await client.close()
