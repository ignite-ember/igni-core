"""The team the message pipeline runs a turn on.

The pipeline talks to Agno through one small seam: the
:class:`MessageTeam` protocol. Agno's ``Team`` offers it, and so does
its ``Agent`` — the headless path runs either. Only the two calls the
pipeline makes are declared, so nothing else of Agno's surface leaks
into the pipeline and a test can offer the same seam without Agno at
all.
"""

from __future__ import annotations

from typing import Any, Protocol

from agno.run.agent import RunOutput
from agno.run.requirement import RunRequirement
from agno.run.team import TeamRunOutput

#: What a run hands back: an agent's run output or a team's.
AnyRunOutput = RunOutput | TeamRunOutput


class MessageTeam(Protocol):
    """The team (or the agent) one turn runs on.

    ``arun``'s message is positional-only, so the protocol is
    satisfied by Agno's own ``arun(input, ...)`` whatever that
    parameter is called there.
    """

    async def arun(
        self,
        message: str,
        /,
        *,
        stream: bool = False,
        images: list[Any] | None = None,
        audio: list[Any] | None = None,
        videos: list[Any] | None = None,
        files: list[Any] | None = None,
    ) -> AnyRunOutput: ...

    async def acontinue_run(
        self,
        run_response: AnyRunOutput,
        *,
        requirements: list[RunRequirement] | None = None,
        session_id: str | None = None,
        stream: bool = False,
    ) -> AnyRunOutput: ...


__all__ = ["AnyRunOutput", "MessageTeam"]
