"""Whether a hook the organisation's group policy ships may run on this machine.

A group's hooks arrive in the signed group policy pack (and in the plugins the
pack installs) and run ``bash -c`` as the developer, outside the agent's
permission system — ``--strict`` denies the agent's shell and does not touch
them. The person a developer needs protecting from here is whoever administers
the organisation, who controls the server and writes the pack, so nothing the
server does can be the control. This is the control, and it is the
developer's: ``hooks.group`` in their own ``~/.igni/config.yaml`` or on their
command line, which neither a project, the group policy nor the managed file
can set (``DeveloperOnlyTier`` in ``core/config/tiers/developer.py``).

* ``run`` — the group's hooks run (the default, and the behaviour before this
  existed).
* ``ask`` — before a group hook first runs, the person sees its command and
  where it came from, and it runs only on a yes. The answer is kept per
  version of the hook (a digest of everything it does), so a hook the group
  changes is asked about again. With nobody to ask — a headless run, or an
  event that fires outside a run — an unanswered hook is refused.
  ``--strict`` means at least this.
* ``refuse`` — no group hook runs.

Enforced in :class:`~igni_core.core.hooks.handlers.registry.HookHandlerRegistry`
— the one place every hook is dispatched to its handler — so no path runs a
group hook without passing here. A refused hook is skipped like a hook that
failed (hooks fail open): it neither blocks the tool call nor runs.

The developer's own hooks (user, project, their own plugins) are not affected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from igni_core.core.config.schemas.orchestration import GROUP_HOOK_MODES, GroupHookMode
from igni_core.core.hooks.group_hook_decisions import (
    ForgetfulGroupHookDecisions,
    GroupHookDecisions,
    GroupHookDecisionStore,
)
from igni_core.core.hooks.schemas import HookDefinition

logger = logging.getLogger(__name__)

#: ``(name, details, reason, agent_path) -> True / False / None`` — the
#: session's in-run approval channel (``ToolCallApprovalChannel.ask``); ``None``
#: means nobody can answer. The same shape as the permission pipeline's
#: ``AskPerson``.
AskAboutGroupHook = Callable[[str, dict[str, Any], str, list[str]], Awaitable[bool | None]]

#: The name the approval dialog shows for a group hook.
APPROVAL_NAME = "group_policy_hook"

#: Why the dialog is asking, under the hook's details.
APPROVAL_REASON = (
    "Your organisation's group policy ships this hook. It runs on this "
    "machine as you, outside the agent's permissions — --strict does not "
    "cover it. Run it? (hooks.group: ask — your answer is kept until the "
    "hook changes.)"
)


class GroupHookApprovalDetails(BaseModel):
    """What the approval dialog shows about the hook it asks about."""

    hook: str
    type: str
    event: str
    source: str

    @classmethod
    def of(cls, hook: HookDefinition, event: str) -> GroupHookApprovalDetails:
        return cls(hook=hook.describe(), type=hook.type, event=event, source=hook.group_source)


class GroupHookGate:
    """Decides, per dispatch, whether a group-policy hook may run."""

    def __init__(
        self,
        mode: Callable[[], GroupHookMode] | GroupHookMode = "run",
        ask: AskAboutGroupHook | None = None,
        decisions: GroupHookDecisionStore | None = None,
    ) -> None:
        self._mode = mode if callable(mode) else (lambda: mode)
        self._ask = ask
        self._decisions = decisions if decisions is not None else GroupHookDecisions.in_home()
        #: In-flight questions by digest, so two dispatches of one hook at
        #: once put one question to the person.
        self._asking: dict[str, asyncio.Future[bool]] = {}
        self._logged: set[tuple[str, str]] = set()

    @classmethod
    def unwired(cls) -> GroupHookGate:
        """For a dispatcher nobody handed a gate: no group hook runs.

        Every production path passes the session's gate; a path that forgot
        to must not be the one that runs the organisation's code unchecked.
        """
        return cls(mode="refuse", decisions=ForgetfulGroupHookDecisions())

    def mode(self) -> GroupHookMode:
        mode = self._mode()
        # Anything unrecognised is read as the cautious middle.
        return mode if mode in GROUP_HOOK_MODES else "ask"

    # ── deciding ───────────────────────────────────────────────────

    async def permits(self, hook: HookDefinition, event: str) -> bool:
        """True when ``hook`` may run for ``event``. Not a group hook → True."""
        if not hook.from_group:
            return True
        match self.mode():
            case "run":
                return True
            case "refuse":
                self._log_once(
                    hook,
                    "refuse",
                    "Not running the group policy's %s hook from %s (hooks.group: refuse): %s",
                    event,
                )
                return False
            case "ask":
                return await self._ask_once(hook, event)

    async def _ask_once(self, hook: HookDefinition, event: str) -> bool:
        digest = hook.digest()
        recorded = self._decisions.get(digest)
        if recorded is not None:
            if recorded == "refuse":
                self._log_once(
                    hook,
                    "declined",
                    "Not running the group policy's %s hook from %s (you declined it): %s",
                    event,
                )
            return recorded == "run"

        pending = self._asking.get(digest)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._asking[digest] = future
        try:
            answer = await self._put_to_person(hook, event, digest)
            future.set_result(answer)
            return answer
        except BaseException:
            if not future.done():
                future.set_result(False)
            raise
        finally:
            self._asking.pop(digest, None)

    async def _put_to_person(self, hook: HookDefinition, event: str, digest: str) -> bool:
        answer: bool | None = None
        if self._ask is not None:
            try:
                answer = await self._ask(
                    APPROVAL_NAME,
                    GroupHookApprovalDetails.of(hook, event).model_dump(),
                    APPROVAL_REASON,
                    ["group policy hook"],
                )
            except Exception as exc:  # fail closed
                logger.warning("Asking about a group hook failed (%s); not running it.", exc)
                answer = None
        if answer is None:
            self._log_once(
                hook,
                "unasked",
                "Not running the group policy's %s hook from %s: hooks.group is 'ask' and "
                "nobody is here to answer. %s",
                event,
            )
            return False
        self._decisions.set(digest, "run" if answer else "refuse", hook)
        logger.info(
            "You %s the group policy's hook from %s: %s",
            "allowed" if answer else "declined",
            hook.group_source,
            hook.describe(),
        )
        return answer

    def _log_once(self, hook: HookDefinition, why: str, message: str, event: str) -> None:
        mark = (hook.digest(), why)
        if mark in self._logged:
            return
        self._logged.add(mark)
        logger.warning(message, event, hook.group_source, hook.describe())


__all__ = [
    "APPROVAL_NAME",
    "AskAboutGroupHook",
    "GroupHookApprovalDetails",
    "GroupHookGate",
]
