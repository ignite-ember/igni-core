"""What the free-text summariser sends the model, and what it keeps of the reply."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from igni_core.core.session.compaction import FallbackSummariser


def _run(*rows):
    return SimpleNamespace(messages=list(rows))


def _row(role, content):
    return SimpleNamespace(role=role, content=content)


class _ExplodingMessages:
    def __iter__(self):
        raise RuntimeError("unreadable")


def _model(reply="a recap"):
    return SimpleNamespace(aresponse=AsyncMock(return_value=SimpleNamespace(content=reply)))


def _sent(model) -> str:
    return model.aresponse.await_args.kwargs["messages"][0].content


async def _summarise(*runs, model=None):
    model = model or _model()
    out = await FallbackSummariser(lambda: model).summarise(SimpleNamespace(runs=list(runs)))
    return out, model


@pytest.mark.asyncio
async def test_the_prompt_carries_the_instructions_and_the_labelled_transcript() -> None:
    out, model = await _summarise(
        _run(_row("user", "  fix it  "), _row("tool", "noise"), _row("assistant", "done"))
    )

    sent = _sent(model)
    assert out == "a recap"
    assert sent.startswith("Summarise the following conversation in 4-8 sentences.")
    assert "--- CONVERSATION ---\nUSER: fix it\n\nASSISTANT: done\n--- END ---" in sent
    assert "noise" not in sent


@pytest.mark.asyncio
async def test_a_message_is_cut_after_800_characters_with_an_ellipsis() -> None:
    _, model = await _summarise(_run(_row("user", "a" * 800), _row("assistant", "b" * 801)))

    sent = _sent(model)
    assert "USER: " + "a" * 800 + "\n" in sent
    assert "ASSISTANT: " + "b" * 800 + "…\n" in sent
    assert "b" * 801 not in sent


@pytest.mark.asyncio
async def test_only_the_last_sixty_rows_are_sent_across_runs() -> None:
    runs = [_run(*[_row("user", f"m{i}") for i in range(r * 30, r * 30 + 30)]) for r in range(3)]

    _, model = await _summarise(*runs)

    sent = _sent(model)
    assert "USER: m29\n" not in sent
    assert "USER: m30\n" in sent
    assert "USER: m89\n" in sent


@pytest.mark.asyncio
async def test_a_run_whose_messages_cannot_be_read_is_skipped_and_others_kept() -> None:
    _, model = await _summarise(
        SimpleNamespace(messages=_ExplodingMessages()), _run(_row("user", "kept ünï 🙂"))
    )

    assert "USER: kept ünï 🙂" in _sent(model)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("before <think>hidden</think>\n after", "before after"),
        ("<think>a</think>x<think>b</think>y", "xy"),
        ("answer <think>never closed", "answer"),
        (None, ""),
        ("", ""),
    ],
)
async def test_the_reply_is_stripped_of_think_blocks(reply, expected) -> None:
    out, _ = await _summarise(_run(_row("user", "hi")), model=_model(reply))

    assert out == expected


@pytest.mark.asyncio
async def test_no_runs_means_no_model_call() -> None:
    model = _model()
    out = await FallbackSummariser(lambda: model).summarise(SimpleNamespace(runs=None))

    assert out == ""
    model.aresponse.assert_not_called()


@pytest.mark.asyncio
async def test_the_model_is_read_at_call_time_so_a_switch_applies() -> None:
    first, second = _model("one"), _model("two")
    live = {"m": first}
    summariser = FallbackSummariser(lambda: live["m"])
    session = SimpleNamespace(runs=[_run(_row("user", "hi"))])

    assert await summariser.summarise(session) == "one"
    live["m"] = second
    assert await summariser.summarise(session) == "two"
