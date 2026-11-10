"""``/agents`` slash command implementation.

Extracted from :mod:`igni_core.backend.command_handler` — the
old inline ``_cmd_agents`` body handled four sub-commands
(``promote``, ``discard``, ``ephemeral``, and default → open
panel) in a linear if/elif ladder. Now split into verb methods
on :class:`AgentsCommand` with a ``dispatch(args)`` router.

:meth:`EphemeralAgentStore.promote` / :meth:`EphemeralAgentStore.discard`
raise; the coordinator catches once
and packages the outcome into a :class:`PromoteResult` /
:class:`DiscardResult` so the try/except lives in a single spot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.command_result import CommandResult
from igni_core.backend.schemas_agents import (
    DiscardResult,
    EphemeralAgentsView,
    PromoteResult,
)
from igni_core.protocol.messages import CommandAction

if TYPE_CHECKING:
    from igni_core.backend.command_handler import CommandHandler
    from igni_core.core.session import Session


class AgentsCommand:
    """Coordinator for the ``/agents`` slash-command family.

    Holds a :class:`Session` reference (not the handler) so we
    never reach into ``handler._session`` from inside the
    coordinator. Verb methods are ``promote / discard /
    list_ephemeral / open_panel``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    async def dispatch(self, args: str) -> CommandResult:
        parts = args.strip().split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        match subcommand:
            case "promote":
                if not sub_args:
                    return CommandResult.error(self.PROMOTE_USAGE)
                return self.promote_args(sub_args)
            case "discard":
                if not sub_args:
                    return CommandResult.error("Usage: /agents discard <name>")
                return self.discard(sub_args.strip())
            case "ephemeral":
                return self.list_ephemeral()
            case _:
                return self.open_panel()

    PROMOTE_USAGE = (
        "Usage: /agents promote <name> [--project | --personal]: --project (the "
        "default) keeps it in .igni/agents/, committed and shared with the team; "
        "--personal in .igni/agents.local/, yours only"
    )

    # ── Verb methods ─────────────────────────────────────────────

    def promote_args(self, sub_args: str) -> CommandResult:
        """``<name> [--project | --personal]``, either order."""
        words = sub_args.split()
        flags = [w for w in words if w.startswith("--")]
        names = [w for w in words if not w.startswith("--")]
        if len(names) != 1 or len(flags) > 1:
            return CommandResult.error(self.PROMOTE_USAGE)
        target = flags[0][2:] if flags else "project"
        if target not in ("project", "personal"):
            return CommandResult.error(self.PROMOTE_USAGE)
        return self.promote(names[0], target)

    def promote(self, name: str, target: str = "project") -> CommandResult:
        try:
            dest = self._session.pool.ephemeral.promote(name, self._session.project_dir, target)
            result = PromoteResult(ok=True, destination=str(dest))
        except (ValueError, RuntimeError) as exc:
            result = PromoteResult(ok=False, error=str(exc))
        return result.to_command_result(name)

    def discard(self, name: str) -> CommandResult:
        try:
            self._session.pool.ephemeral.discard(name)
            result = DiscardResult(ok=True)
        except (ValueError, RuntimeError) as exc:
            result = DiscardResult(ok=False, error=str(exc))
        return result.to_command_result(name)

    def list_ephemeral(self) -> CommandResult:
        pool = self._session.pool
        agents = pool.ephemeral.list_agents() if pool.has_ephemeral else []
        return EphemeralAgentsView(agents=agents).to_command_result()

    def open_panel(self) -> CommandResult:
        return CommandResult.for_action(CommandAction.AGENTS)


async def cmd_agents(handler: CommandHandler, args: str) -> CommandResult:
    """Two-line shim for :class:`AgentsCommand`."""
    return await AgentsCommand(handler.session).dispatch(args)
