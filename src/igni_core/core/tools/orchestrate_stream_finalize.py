"""Reading a finished run's answer back.

When a stream ends, the answer the parent agent gets has to come from
somewhere. Agno's ``Agent`` / ``Team`` do not expose a
``run_response`` attribute — reading it raises ``AttributeError`` —
so the supported way to fetch the canonical ``RunOutput`` after a
streaming run is ``aget_run_output(run_id, session_id)``.

That lookup is not guaranteed to answer: the async write that backs
it may not have flushed by the time the stream ends. The streamed
``RunCompletedEvent.content`` is the fallback, and it is captured in
the state as the run goes.

:class:`RunFinalizer` owns that read, and :class:`FinalizeResult`
is what it hands back — a typed envelope rather than a broad
``except Exception`` around the whole thing (CODE_STANDARDS.md
Pattern 3: a Result for an expected failure, not a raised exception
caught one layer up).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from igni_core.core.tools.orchestrate_stream_state import StreamState

_stream_log = logging.getLogger("igni_core.llm_calls")


class FinalizeResult(BaseModel):
    """What one attempt to read a finished run's answer found."""

    model_config = ConfigDict(populate_by_name=True)

    #: The answer the store holds, when it holds one.
    content: str | None = None
    #: Why the read failed, when it failed.
    error: str | None = None
    #: The run's status, as the store reports it.
    status: str | None = None
    #: Whether the store had a run for this id at all.
    found: bool = False


class RunFinalizer:
    """Reads the final answer of one finished run.

    One instance per stream, built by the handler when its stream
    ends. It holds the runnable it asks, the state it falls back on,
    and the path it logs under.
    """

    __slots__ = ("_path", "_runnable", "_state")

    def __init__(self, runnable: Any, state: StreamState, path: list[str]) -> None:
        self._runnable = runnable
        self._state = state
        self._path = list(path)

    async def final_content(self) -> str:
        """The run's final answer, ready to hand to the parent.

        The store is asked first; when it has nothing, the content
        the stream itself carried is used.
        """
        result = await self.fetch()
        content = result.content
        if not content and self._state.completed_content:
            content = self._state.completed_content
            _stream_log.info(
                "stream_handler: used RunCompletedEvent fallback path=%s len=%d",
                self._path,
                len(content),
            )
        return self.clean_final(content)

    async def fetch(self) -> FinalizeResult:
        """Ask Agno's session store for the run's output.

        Only ``AttributeError`` / ``KeyError`` / ``TimeoutError`` — the
        expected "the store hasn't flushed yet" shapes seen in the
        field — are turned into a failed result. Anything else
        re-raises, so an unexpected bug isn't hidden behind an empty
        answer.
        """
        run_id = self._state.current_run_id
        session_id = self._state.current_session_id
        _stream_log.info(
            "stream_handler: stream ended path=%s run_id=%s session_id=%s completed_content_len=%d",
            self._path,
            run_id,
            session_id,
            len(self._state.completed_content or ""),
        )
        try:
            run_output = None
            if run_id and session_id:
                run_output = await self._runnable.aget_run_output(
                    run_id=run_id, session_id=session_id
                )
            if run_output is None and session_id:
                run_output = await self._runnable.aget_last_run_output(session_id=session_id)
        except (AttributeError, KeyError, TimeoutError) as exc:
            _stream_log.info(
                "stream_handler: expected read failure path=%s err=%s",
                self._path,
                exc,
            )
            return FinalizeResult(error=str(exc))

        content = getattr(run_output, "content", None) if run_output else None
        status = getattr(run_output, "status", None) if run_output else None
        _stream_log.info(
            "stream_handler: db lookup path=%s found=%s status=%s content_len=%d",
            self._path,
            run_output is not None,
            status,
            len(str(content)) if content else 0,
        )
        return FinalizeResult(
            content=str(content) if content else None,
            # Agno reports a run's status as ``RunStatus``, a string
            # enum. Coercing it here keeps the field a plain string
            # whatever the store hands back.
            status=str(status) if status is not None else None,
            found=run_output is not None,
        )

    @staticmethod
    def clean_final(content: str | None) -> str:
        """Strip the ``<think>`` scaffolding and trim.

        Some models leak ``<think>...</think>`` blocks the tokenizer
        template didn't strip — safe to remove verbatim from the final
        response we hand back to the parent agent.
        """
        if not content:
            return ""
        return content.replace("<think>", "").replace("</think>", "").strip()


__all__ = ["FinalizeResult", "RunFinalizer"]
