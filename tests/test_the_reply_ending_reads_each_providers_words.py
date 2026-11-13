"""How a reply's ending is read, and which endings are errors.

The model classes drain real provider objects through the watcher elsewhere;
this pins the vocabulary itself, the shapes ``reason_in`` reads, what the
errors say and whether a retry may follow, and that each watched reply sees
only its own chunks.
"""

from __future__ import annotations

import asyncio
import contextvars
from enum import Enum
from types import SimpleNamespace as NS

import pytest
from agno.exceptions import ModelProviderError

from igni_core.core.config.reply_ending import (
    ANTHROPIC,
    GEMINI,
    OPENAI,
    ModelReplyCut,
    ReplyCutRetries,
    ReplyEnding,
    reason_in,
)


class Finish(Enum):
    SAFETY = "SAFETY"


def choice(reason):
    return NS(finish_reason=reason)


class TestClassify:
    @pytest.mark.parametrize(
        ("provider", "reason", "verdict"),
        [
            (OPENAI, "length", "length"),
            (OPENAI, "stop", "finished"),
            (OPENAI, "tool_calls", "finished"),
            (OPENAI, "function_call", "finished"),
            (OPENAI, "a-vendor-word", "finished"),
            (OPENAI, "max_tokens", "finished"),
            (ANTHROPIC, "max_tokens", "length"),
            (ANTHROPIC, "model_context_window_exceeded", "length"),
            (ANTHROPIC, "end_turn", "finished"),
            (ANTHROPIC, "tool_use", "finished"),
            (ANTHROPIC, "stop_sequence", "finished"),
            (ANTHROPIC, "pause_turn", "finished"),
            (ANTHROPIC, "refusal", "stopped"),
            (ANTHROPIC, "something-new", "stopped"),
            (GEMINI, "MAX_TOKENS", "length"),
            (GEMINI, "STOP", "finished"),
            (GEMINI, "SAFETY", "stopped"),
            (GEMINI, "RECITATION", "stopped"),
            (GEMINI, "stop", "stopped"),
        ],
    )
    def test_each_provider_says_it_in_its_own_words(self, provider, reason, verdict):
        assert provider.classify(reason) == verdict


class TestReasonIn:
    def test_openai_choices_keep_the_last_non_empty_reason(self):
        response = NS(choices=[choice("stop"), choice(None), choice("")])

        assert reason_in(response) == "stop"
        assert reason_in(NS(choices=[choice("stop"), choice("length")])) == "length"

    @pytest.mark.parametrize(
        "response", [NS(choices=[]), NS(choices=None), NS(), NS(choices=[choice("")])]
    )
    def test_nothing_to_read_is_none(self, response):
        assert reason_in(response) is None

    def test_a_gemini_enum_is_read_by_its_value_and_a_string_as_it_is(self):
        assert reason_in(NS(candidates=[choice(Finish.SAFETY)])) == "SAFETY"
        assert reason_in(NS(candidates=[choice("MAX_TOKENS")])) == "MAX_TOKENS"

    def test_anthropic_reads_the_message_the_delta_and_the_accumulated_message(self):
        assert reason_in(NS(stop_reason="end_turn")) == "end_turn"
        assert reason_in(NS(delta=NS(stop_reason="max_tokens"))) == "max_tokens"
        assert reason_in(NS(message=NS(stop_reason="tool_use"))) == "tool_use"

    @pytest.mark.parametrize("stop", [None, "", 3, ["end_turn"]])
    def test_an_anthropic_stop_reason_that_is_not_a_word_is_ignored(self, stop):
        assert reason_in(NS(stop_reason=stop)) is None
        assert reason_in(NS(delta=NS(stop_reason=stop))) is None


class TestTheErrors:
    def test_a_dropped_stream_is_retryable_and_says_so(self):
        cut = ModelReplyCut.no_ending("m-1")

        assert cut.retryable is True
        assert "stream ended without a finish_reason" in str(cut)
        assert cut.model_id == "m-1"

    def test_a_stopped_reply_quotes_the_provider_and_is_not_retryable(self):
        cut = ModelReplyCut.stopped("m-1", "SAFETY")

        assert cut.retryable is False
        assert "stopped before it finished (SAFETY)" in str(cut)

    @pytest.mark.parametrize("reason", ["context window", "Context_Length_Exceeded"])
    def test_a_reason_that_reads_as_a_context_window_error_is_not_quoted(self, reason):
        # Quoted, Agno would send the run down the compaction path.
        cut = ModelReplyCut.stopped("m-1", reason)

        assert reason.lower() not in str(cut).lower()
        assert "stopped before it finished:" in str(cut)

    @pytest.mark.parametrize(("cap", "said"), [(512, " (512 tokens)"), (None, ""), (0, "")])
    def test_a_length_cut_names_the_cap_only_when_it_is_known(self, cap, said):
        cut = ModelReplyCut.at_length("m-1", cap)

        assert cut.retryable is False
        assert f"output length limit{said} before it finished" in str(cut)


class Base:
    def _is_retryable_error(self, error):
        return "base"


class Retrying(ReplyCutRetries, Base):
    pass


class TestRetry:
    def test_a_non_retryable_cut_is_refused_directly_or_as_a_cause(self):
        direct = ModelReplyCut.at_length("m", 5)
        wrapped = ModelProviderError("wrapped")
        wrapped.__cause__ = ModelReplyCut.stopped("m", "refusal")

        assert Retrying()._is_retryable_error(direct) is False
        assert Retrying()._is_retryable_error(wrapped) is False

    def test_a_dropped_stream_and_other_errors_are_left_to_agno(self):
        wrapped = ModelProviderError("wrapped")
        wrapped.__cause__ = ModelReplyCut.no_ending("m")

        assert Retrying()._is_retryable_error(ModelReplyCut.no_ending("m")) == "base"
        assert Retrying()._is_retryable_error(wrapped) == "base"
        assert Retrying()._is_retryable_error(ModelProviderError("x")) == "base"

    def test_a_cyclic_cause_chain_ends(self):
        one, two = ModelProviderError("one"), ModelProviderError("two")
        one.__cause__, two.__cause__ = two, one

        assert Retrying()._is_retryable_error(one) == "base"


class TestWatching:
    def test_a_chunk_is_recorded_on_the_reply_being_watched(self):
        ending = ReplyEnding("m", None)

        with ending.watching():
            ReplyEnding.note_chunk(NS(choices=[choice("stop")]))

        assert ending.reason == "stop"

    def test_a_later_chunk_without_a_reason_does_not_erase_it(self):
        ending = ReplyEnding("m", None)

        with ending.watching():
            ReplyEnding.note_chunk(NS(choices=[choice("length")]))
            ReplyEnding.note_chunk(NS(choices=[choice(None)]))
            ReplyEnding.note_chunk(NS())

        assert ending.reason == "length"

    def test_a_chunk_noted_while_nothing_is_watched_goes_nowhere(self):
        ReplyEnding.note_chunk(NS(choices=[choice("stop")]))
        ending = ReplyEnding("m", None)

        with ending.watching():
            pass

        assert ending.reason is None
        assert ReplyEnding._current.get() is None

    def test_a_chunk_after_the_watch_ends_is_not_recorded(self):
        ending = ReplyEnding("m", None)
        with ending.watching():
            pass

        ReplyEnding.note_chunk(NS(choices=[choice("stop")]))

        assert ending.reason is None

    def test_two_replies_streaming_at_once_keep_their_own_endings(self):
        async def reply(reason: str, pause: float) -> ReplyEnding:
            ending = ReplyEnding(reason, None)
            with ending.watching():
                await asyncio.sleep(pause)
                ReplyEnding.note_chunk(NS(choices=[choice(reason)]))
                await asyncio.sleep(pause)
            return ending

        async def both():
            return await asyncio.gather(reply("stop", 0.01), reply("length", 0.02))

        first, second = asyncio.run(both())

        assert (first.reason, second.reason) == ("stop", "length")

    def test_closing_the_watch_from_another_context_does_not_raise(self):
        ending = ReplyEnding("m", None)
        watch = ending.watching()
        contextvars.copy_context().run(watch.__enter__)

        watch.__exit__(None, None, None)


class TestCheck:
    def test_no_reason_is_a_dropped_stream(self):
        with pytest.raises(ModelReplyCut) as raised:
            ReplyEnding("m", 9).check()

        assert raised.value.retryable is True

    def test_a_length_cut_says_the_cap(self):
        ending = ReplyEnding("m", 9)
        ending.reason = "length"

        with pytest.raises(ModelReplyCut, match=r"\(9 tokens\)") as raised:
            ending.check()

        assert raised.value.retryable is False

    def test_a_stopped_reply_quotes_the_providers_word(self):
        ending = ReplyEnding("m", None, ANTHROPIC)
        ending.reason = "refusal"

        with pytest.raises(ModelReplyCut, match=r"\(refusal\)"):
            ending.check()

    @pytest.mark.parametrize(
        ("provider", "reason"),
        [(OPENAI, "stop"), (OPENAI, "weird"), (ANTHROPIC, "pause_turn"), (GEMINI, "STOP")],
    )
    def test_a_finished_reply_is_silent(self, provider, reason):
        ending = ReplyEnding("m", None, provider)
        ending.reason = reason

        ending.check()

    def test_a_whole_completion_is_checked_the_same_way(self):
        ReplyEnding.check_completion(NS(choices=[choice("stop")]), "m", 5)
        with pytest.raises(ModelReplyCut, match=r"\(5 tokens\)"):
            ReplyEnding.check_completion(NS(choices=[choice("length")]), "m", 5)
        with pytest.raises(ModelReplyCut, match="cut off"):
            ReplyEnding.check_completion(NS(choices=[]), "m", 5)
        with pytest.raises(ModelReplyCut):
            ReplyEnding.check_completion(NS(stop_reason="max_tokens"), "m", 5, ANTHROPIC)
        ReplyEnding.check_completion(NS(stop_reason="pause_turn"), "m", 5, ANTHROPIC)


def test_the_watched_variable_does_not_carry_the_old_product_name():
    assert "ember" not in ReplyEnding._current.name
