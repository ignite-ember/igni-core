"""Command handler — thin router for slash commands.

:class:`CommandHandler` composes three collaborators and dispatches
in three tiers:

* :class:`BuiltinCommandRegistry` — the ``/name → shim`` table plus
  ``/name → description`` catalog and dispatch method.
* :class:`MarkdownCommandDispatcher` — tier-2 fallback for
  markdown-authored ``.igni/commands/*.md`` files.
* :class:`SkillCommandDispatcher` — tier-3 fallback for
  user-invocable skills.

Skills lose to markdown-file names on collision because the user
explicitly imported the skill, whereas markdown files are best-effort
drop-ins.

The markdown tier's discovery is a constructor argument
(:attr:`CommandHandler.discover`), so a test can hand the handler a
stub instead of patching a module-level symbol.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from igni_core.backend.builtin_command_registry import (
    BUILTIN_REGISTRY,
    BuiltinCommand,  # noqa: F401 — public re-export
    BuiltinCommandRegistry,
)
from igni_core.backend.command_result import CommandResult
from igni_core.backend.markdown_command_dispatcher import (
    DiscoverMarkdownCommands,
    MarkdownCommandDispatcher,
)
from igni_core.backend.skill_command_dispatcher import SkillCommandDispatcher
from igni_core.core.contributed_commands import contributed_commands
from igni_core.core.utils.markdown_commands import MarkdownCommand
from igni_core.protocol.messages import (  # noqa: F401 — public re-export
    CommandAction,
    CommandResultKind,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from igni_core.core.session import Session


class CommandHandler:
    """Thin slash-command router.

    Composes the built-in registry plus two fallback dispatchers and
    holds a reference to the owning :class:`Session`.
    """

    _REGISTRY: ClassVar[BuiltinCommandRegistry] = BUILTIN_REGISTRY

    def __init__(
        self,
        session: Session,
        *,
        discover: DiscoverMarkdownCommands | None = None,
    ) -> None:
        """Compose the tiers.

        ``discover`` is the markdown tier's discovery callable. It
        defaults to :meth:`MarkdownCommand.discover`; a test hands
        in a stub instead of patching a module-level symbol, which
        is the same thing the dependency injection is for.
        """
        self._session = session
        self._markdown_dispatcher = MarkdownCommandDispatcher(
            session,
            discover if discover is not None else MarkdownCommand.discover,
        )
        self._skill_dispatcher = SkillCommandDispatcher(session)

    @property
    def session(self) -> Session:
        """Public accessor for the owned :class:`Session` — used by
        every ``cmd_*.py`` coordinator to avoid private-attribute
        reach-in (Rule 6)."""
        return self._session

    async def handle(self, command: str) -> CommandResult:
        """Dispatch a slash command and return its result.

        Four tiers, in order: built-in registry, markdown fallback,
        installed extensions, skill fallback.

        Extensions go third on purpose. Before the built-ins would
        mean an extension could shadow ``/clear``, and after the skill
        fallback would mean never, because that tier is the catch-all
        — which is exactly where ``contributes.commands`` used to end
        up, and why declaring one did nothing.
        """
        stripped = command.strip()
        cmd = stripped.split()[0].lower()
        args = stripped[len(cmd) :].strip()

        builtin = await self._REGISTRY.dispatch(self, cmd, args)
        if builtin is not None:
            return builtin

        md_result = await self._markdown_dispatcher.try_render(cmd, args)
        if md_result is not None:
            return md_result

        extension = await self._try_extension(cmd, args)
        if extension is not None:
            return extension

        return await self._skill_dispatcher.match_and_render(stripped)

    async def _try_extension(self, cmd: str, args: str) -> CommandResult | None:
        """Run a command an installed extension answers, or ``None``.

        ``None`` means "not ours" and falls through to the next tier.
        A *failure* is a result rather than a fall-through: once the
        command is known to belong to an extension, dropping to the
        skill fallback would answer a user's ``/compliance`` with
        whatever a fuzzy skill match turned up, which is a stranger
        outcome than being told what went wrong.
        """
        found = contributed_commands.get(cmd)
        if found is None:
            return None
        try:
            return CommandResult.markdown(await found.invoke(args))
        except Exception as exc:
            logger.info("extension command %s failed: %s", cmd, exc)
            return CommandResult.error(str(exc))

    async def _handle_markdown_command(self, cmd: str, args: str) -> CommandResult | None:
        """Delegate to :meth:`MarkdownCommandDispatcher.try_render`."""
        return await self._markdown_dispatcher.try_render(cmd, args)

    async def _handle_skill(self, stripped: str) -> CommandResult:
        """Delegate to :meth:`SkillCommandDispatcher.match_and_render`."""
        return await self._skill_dispatcher.match_and_render(stripped)

    @classmethod
    def builtin_names(cls) -> list[str]:
        """Bare (no-slash) names of every built-in command — the
        catalog surface consumed by :class:`SlashCommandsCatalog`."""
        return cls._REGISTRY.names()

    @classmethod
    def describe(cls, name: str) -> str:
        """One-liner description for a built-in command."""
        return cls._REGISTRY.describe(name)
