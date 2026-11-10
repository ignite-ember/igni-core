"""Tool-event hook factory + permission-evaluator owner.

Owns two invariants:

* The evaluator is built from ``settings.permissions.*`` and cached —
  every ``create()`` call reuses the same instance so the mode flip
  performed by :class:`RuntimeModeCoordinator` survives hook rebuilds.
* The :class:`ToolEventHook` is composed with the cached evaluator plus
  the session's current :class:`HookExecutor` (read from
  :class:`SessionHooks` on every build) and ``session_id``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from igni_core.core.config.permission_eval import PermissionEvaluator, ToolCategoryCatalog
from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.bypass_explainer import CallExplainer
from igni_core.core.hooks.bypass_guard import Ask, BypassGuard
from igni_core.core.hooks.permission.evaluator_stage import AskPerson
from igni_core.core.hooks.tool_hook import ToolEventHook
from igni_core.core.mcp.hints import ToolHintsRegistry
from igni_core.core.session.hooks import SessionHooks
from igni_core.core.utils.rules_index import RulesIndex


class ToolEventHookFactory:
    """Factory + owner of the :class:`ToolEventHook` used by the main
    team's ``tool_hooks`` list.

    Replaced on every plugin reload; the session seeds the replacement
    with the previous evaluator so a mode the person chose survives.
    """

    def __init__(
        self,
        settings: Settings,
        rules_index: RulesIndex,
        project_dir: Path,
        hooks: SessionHooks,
        session_id_ref: Callable[[], str],
        permission_evaluator: PermissionEvaluator | None = None,
        tool_hints: ToolHintsRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._rules_index = rules_index
        self._project_dir = project_dir
        self._hooks = hooks
        self._session_id_ref = session_id_ref
        # Without the seed a replacement factory rebuilds the evaluator from
        # ``settings.permissions.mode`` — dropping a person out of plan mode
        # because they installed a plugin.
        self._permission_evaluator: PermissionEvaluator | None = permission_evaluator
        # What an extension's tools say about themselves. The modes and
        # Bypass's last check both read it, so both are given the one
        # table — the one the installer fills.
        self._tool_hints = tool_hints
        #: Bypass mode's last check; set by the session once it can ask.
        self.bypass_guard: BypassGuard | None = None
        #: The session's in-run approval channel, for a call the evaluator
        #: wants asked about that was built without an approval pause.
        self.ask_person: AskPerson | None = None

    @property
    def permission_evaluator(self) -> PermissionEvaluator:
        """The cached evaluator, built from the ``permissions`` block on
        first access (explicit lists, per-category levels, ``--read-only``)."""
        if self._permission_evaluator is None:
            self._permission_evaluator = PermissionEvaluator.from_permissions(
                self._settings.permissions,
                catalog=self._catalog(),
            )
        return self._permission_evaluator

    def _catalog(self) -> ToolCategoryCatalog | None:
        """The catalog the modes classify tools with.

        ``None`` leaves the default one, which reads the process's
        table of what an extension's tools say they are.
        """
        if self._tool_hints is None:
            return None
        return ToolCategoryCatalog(tool_hints=self._tool_hints)

    @property
    def in_bypass(self) -> bool:
        """Whether the live evaluator is in Bypass mode, read per call."""
        return self.permission_evaluator.mode is PermissionMode.BYPASS_PERMISSIONS

    def build_bypass_guard(
        self, ask: Ask, model_ref: Callable[[], Any], tool_hints: ToolHintsRegistry
    ) -> BypassGuard:
        """Bypass mode's last check, asking through ``ask``.

        ``model_ref`` is called per explanation, so the session's current
        model explains the call even after its settings are replaced.
        """
        config = self._settings.safety.bypass_guard
        return BypassGuard(
            config=config,
            project_dir=self._project_dir,
            in_bypass=lambda: self.in_bypass,
            ask=ask,
            tool_hints=tool_hints,
            explainer=(
                CallExplainer(
                    model_ref, project_dir=self._project_dir, timeout=config.explain_timeout
                )
                if config.explain
                else None
            ),
        )

    def create(self) -> ToolEventHook:
        """A fresh :class:`ToolEventHook` bound to the current executor,
        session id and cached evaluator."""
        return ToolEventHook(
            executor=self._hooks.executor,
            session_id=self._session_id_ref(),
            protected_paths=self._settings.safety.protected_paths,
            blocked_commands=self._settings.safety.blocked_commands,
            rules_index=self._rules_index,
            project_dir=self._project_dir,
            permission_evaluator=self.permission_evaluator,
            bypass_guard=self.bypass_guard,
            ask_person=self.ask_person,
        )
