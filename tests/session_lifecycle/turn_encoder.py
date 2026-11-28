"""A scripted turn, as the OpenAI chat-completions API would send it."""

from __future__ import annotations

import json
import time

from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice as CompletionChoice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)

from tests.session_lifecycle.schemas.model_turn import ModelTurn


class TurnEncoder:
    """Encodes one :class:`ModelTurn` for one request: streamed chunks or one completion."""

    COMPLETION_ID = "chatcmpl-scripted"

    def __init__(self, turn: ModelTurn, model: str, request_index: int) -> None:
        self.turn = turn
        self.model = model
        self.request_index = request_index

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self.turn.tool_calls else "stop"

    def _call_id(self, index: int) -> str:
        call = self.turn.tool_calls[index]
        return call.call_id or f"call_{self.request_index}_{index}"

    def _usage(self) -> CompletionUsage:
        turn = self.turn
        return CompletionUsage(
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            total_tokens=turn.prompt_tokens + turn.completion_tokens,
        )

    def deltas(self) -> list[ChoiceDelta]:
        """The role, then reasoning, text and each tool call, in chunks of ``chunk_size``."""
        turn = self.turn
        size = max(1, turn.chunk_size)
        out = [ChoiceDelta(role="assistant", content="")]
        out += [
            ChoiceDelta(reasoning_content=turn.reasoning[i : i + size])  # type: ignore[call-arg]
            for i in range(0, len(turn.reasoning), size)
        ]
        out += [
            ChoiceDelta(content=turn.text[i : i + size]) for i in range(0, len(turn.text), size)
        ]
        for index, call in enumerate(turn.tool_calls):
            arguments = json.dumps(call.arguments)
            out.append(
                ChoiceDelta(
                    tool_calls=[
                        ChoiceDeltaToolCall(
                            index=index,
                            id=self._call_id(index),
                            type="function",
                            function=ChoiceDeltaToolCallFunction(name=call.name, arguments=""),
                        )
                    ]
                )
            )
            out += [
                ChoiceDelta(
                    tool_calls=[
                        ChoiceDeltaToolCall(
                            index=index,
                            function=ChoiceDeltaToolCallFunction(arguments=arguments[i : i + size]),
                        )
                    ]
                )
                for i in range(0, len(arguments), size)
            ]
        return out

    def chunk(self, delta: ChoiceDelta, finish: str | None = None) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id=self.COMPLETION_ID,
            object="chat.completion.chunk",
            created=int(time.time()),
            model=self.model,
            choices=[Choice(index=0, delta=delta, finish_reason=finish)],  # type: ignore[arg-type]
        )

    def finish_chunk(self) -> ChatCompletionChunk:
        return self.chunk(ChoiceDelta(), self.finish_reason)

    def usage_chunk(self) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id=self.COMPLETION_ID,
            object="chat.completion.chunk",
            created=int(time.time()),
            model=self.model,
            choices=[],
            usage=self._usage(),
        )

    def completion(self) -> ChatCompletion:
        """The whole turn as one non-streamed completion."""
        calls = [
            ChatCompletionMessageToolCall(
                id=self._call_id(i),
                type="function",
                function=Function(name=call.name, arguments=json.dumps(call.arguments)),
            )
            for i, call in enumerate(self.turn.tool_calls)
        ]
        message = ChatCompletionMessage(
            role="assistant", content=self.turn.text or None, tool_calls=calls or None
        )
        return ChatCompletion(
            id=self.COMPLETION_ID,
            object="chat.completion",
            created=int(time.time()),
            model=self.model,
            choices=[
                CompletionChoice(index=0, message=message, finish_reason=self.finish_reason)  # type: ignore[arg-type]
            ],
            usage=self._usage(),
        )
