"""``host.decide``: typed questions to igni's local decision model."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from igni_core.core.decisions import (
    Decision,
    DecisionRequest,
    DecisionsUnavailable,
    DecisionTooLong,
)
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.base import Call, Members, call
from igni_core.extensions.host.spec import ErrorCode


class DecisionMembers(Members):
    """Typed questions to igni's local decision model, behind the Bypass guard."""

    @call("decide")
    async def decide(self, c: Call) -> Decision:
        decisions = await self._decide([c.params["context"]], c.params["questions"])
        return decisions[0]

    @call("decide_batch")
    async def decide_batch(self, c: Call) -> list[Decision]:
        return await self._decide(c.params["contexts"], c.params["questions"])

    async def _decide(self, contexts: list[Any], questions: list[Any]) -> list[Decision]:
        try:
            request = DecisionRequest(contexts=contexts, questions=questions)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise HostModelError(
                ErrorCode.INVALID, f"{where}: {first['msg']}" if where else first["msg"]
            ) from None
        try:
            decided = await self.host.decisions.decide(request)
        except DecisionTooLong as exc:
            raise HostModelError(ErrorCode.INVALID, str(exc)) from None
        except DecisionsUnavailable as exc:
            raise HostModelError(ErrorCode.UNAVAILABLE, str(exc)) from None
        # Dumped by :func:`~igni_core.extensions.host.plain.plain` on the way out.
        return list(decided)


__all__ = ["DecisionMembers"]
