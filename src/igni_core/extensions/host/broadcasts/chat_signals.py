"""The conversation as it streams: messages, typing, tool calls, notices, sessions."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from igni_core.extensions.host.broadcasts.signal import Handler, RunRef
from igni_core.extensions.host.signal_values import Emitter, Signal, Windowed
from igni_core.extensions.host.values import SessionInfo
from igni_core.extensions.host.windows import Windows
from igni_core.protocol import messages as msg


class MessageAdded(Signal):
    WIRE: ClassVar[str] = "chat.message_added"

    session_id: str
    text: str
    queued: bool
    from_extension: str | None
    from_service: str | None

    @classmethod
    def received(cls, m: msg.UserMessageReceived) -> MessageAdded:
        return cls(
            session_id=m.session_id,
            text=m.text,
            queued=m.queued,
            from_extension=m.extension_id or None,
            from_service=m.service_client_id or None,
        )


class TypingSignal(Windowed):
    WIRE: ClassVar[str] = "chat.typing"

    session_id: str
    text: str
    #: The window typed in, when the connection is one of a known window.
    window: str | None


class ContentDeltaSignal(Signal):
    WIRE: ClassVar[str] = "chat.content_delta"

    session_id: str
    text: str
    thinking: bool = Field(validation_alias="is_thinking")


class ReasoningStartedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.reasoning_started"


class StreamingDoneSignal(RunRef):
    WIRE: ClassVar[str] = "chat.streaming_done"


class InfoSignal(Signal):
    WIRE: ClassVar[str] = "chat.info"

    session_id: str
    text: str


class ErrorSignal(InfoSignal):
    WIRE: ClassVar[str] = "chat.error"


class ToolStartedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.tool_started"

    tool_name: str
    friendly_name: str
    args_summary: str


class ToolCompletedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.tool_completed"

    summary: str
    is_error: bool
    is_rejected: bool


class ToolErrorSignal(RunRef):
    WIRE: ClassVar[str] = "chat.tool_error"

    error: str


class ModelCompletedSignal(RunRef):
    WIRE: ClassVar[str] = "chat.model_completed"

    input_tokens: int
    output_tokens: int


class SessionsListed(Signal):
    WIRE: ClassVar[str] = "sessions.listed"

    sessions: list[SessionInfo]


class SessionsCleared(Signal):
    WIRE: ClassVar[str] = "sessions.cleared"

    session_id: str
    new_session_id: str
    summary: str


class ChatSignals:
    """Translates what the conversation says; touches the window it was said in."""

    def __init__(self, emitter: Emitter, windows: Windows) -> None:
        self._emitter = emitter
        self._windows = windows

    def handlers(self) -> dict[type[msg.Message], Handler]:
        send_of: dict[type[msg.Message], type[Signal]] = {
            msg.ContentDelta: ContentDeltaSignal,
            msg.ReasoningStarted: ReasoningStartedSignal,
            msg.StreamingDone: StreamingDoneSignal,
            msg.Info: InfoSignal,
            msg.Error: ErrorSignal,
            msg.ToolStarted: ToolStartedSignal,
            msg.ToolCompleted: ToolCompletedSignal,
            msg.ToolError: ToolErrorSignal,
            msg.ModelCompleted: ModelCompletedSignal,
            msg.SessionCleared: SessionsCleared,
        }
        return {
            msg.UserMessageReceived: self.user_message_received,
            msg.Typing: self.typing,
            msg.SessionListResult: self.session_list_result,
            **{kind: self._as(signal) for kind, signal in send_of.items()},
        }

    def _as(self, signal: type[Signal]) -> Handler:
        """A message whose signal is its own fields, renamed at most."""
        return lambda m: self._emitter.send(signal.of(m))

    def user_message_received(self, m: msg.UserMessageReceived) -> None:
        self._windows.registry.touch_connection(m.client_id)
        self._emitter.send(MessageAdded.received(m))

    def typing(self, m: msg.Typing) -> None:
        window = self._windows.registry.by_connection(m.client_id)
        self._emitter.send(TypingSignal(session_id=m.session_id, text=m.text, window=window))
        if window is not None:
            # Typing is using the window. The draft itself is the
            # window's model, written by the window (``ui.composer``).
            self._windows.registry.touch(window)

    def session_list_result(self, m: msg.SessionListResult) -> None:
        sessions = [SessionInfo.from_entry(s.model_dump()) for s in m.sessions]
        self._emitter.send(SessionsListed(sessions=sessions))


__all__ = [
    "ChatSignals",
    "ContentDeltaSignal",
    "ErrorSignal",
    "InfoSignal",
    "MessageAdded",
    "ModelCompletedSignal",
    "ReasoningStartedSignal",
    "SessionsCleared",
    "SessionsListed",
    "StreamingDoneSignal",
    "ToolCompletedSignal",
    "ToolErrorSignal",
    "ToolStartedSignal",
    "TypingSignal",
]
