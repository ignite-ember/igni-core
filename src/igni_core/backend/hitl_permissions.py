"""Permission rules for one session, as the HITL flow reads and writes them.

:class:`HitlPermissions` answers the wire-side permission RPCs
(``check_permission`` / ``save_permission_rule``) and persists a
person's sticky approval choice ("always" / "similar" / "deny") as a
rule, both in ``.igni/settings.local.json`` and in the session's live
:class:`PermissionEvaluator`, so the very next call is not asked again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from igni_core.backend.schemas_hitl import (
    PersistChoiceResult,
    RequirementTool,
    RunRequirement,
    ToolCallArgs,
)
from igni_core.core.config.permission_eval import PermissionRule
from igni_core.core.config.tool_permissions import (
    PermissionLevel,
    ToolInvocation,
    ToolNameResolver,
    ToolPermissions,
)
from igni_core.protocol import messages as msg

if TYPE_CHECKING:
    from igni_core.core.session import Session

logger = logging.getLogger(__name__)


class HitlPermissions:
    """Checks and saves the permission rules of one session."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._resolver = ToolNameResolver()

    def check(self, tool_name: str, func_name: str, tool_args: ToolCallArgs) -> PermissionLevel:
        """Permission level for a tool call.

        The catalog name for the Agno function (``run_shell_command`` →
        ``Bash``) wins over the caller-supplied ``tool_name``: it is the
        authoritative name for permission dispatch.
        """
        registry_name = self._resolver.catalog_for(func_name) or tool_name
        return self._store().check(registry_name, func_name, tool_args.as_dict())

    def save_rule(self, rule: str, level: PermissionLevel) -> None:
        """Persist a permission rule, and hold the running session to it at
        once rather than at its next settings reload."""
        self._store().save_rule(rule, level)
        self._remember_live(rule, level)

    def persist_choice(
        self, decision: msg.HITLDecision, req: RunRequirement
    ) -> PersistChoiceResult:
        """Persist the person's HITL choice as a permission rule when
        they picked a sticky option ("always" / "similar" / "deny").

        ``ok=False`` with a ``reason`` when the choice was a
        per-invocation ``once`` or the requirement names no tool.
        :meth:`msg.HITLChoice.is_persistent` owns which choices persist.
        """
        choice = msg.HITLChoice((decision.choice or "").lower())
        if not choice.is_persistent():
            return PersistChoiceResult(
                ok=False, reason=f"choice '{decision.choice}' is not persistable"
            )
        tool = RequirementTool.of(req)
        if not tool.name:
            logger.warning(
                "persist_choice: missing tool metadata on requirement; choice=%s degraded to no-op",
                choice.value,
            )
            return PersistChoiceResult(ok=False, reason="missing tool metadata on requirement")
        # The catalog name keeps the persisted rule readable in
        # ``.igni/settings.local.json`` whether the requirement arrived
        # tagged with the Agno function name or the catalog name.
        canonical = self._resolver.catalog_for(tool.name) or tool.name
        invocation = ToolInvocation.from_raw(canonical, tool.args)
        if choice is msg.HITLChoice.SIMILAR:
            rule = invocation.pattern_rule()
        else:
            rule = invocation.exact_rule()
        level: PermissionLevel = "deny" if choice is msg.HITLChoice.DENY else "allow"
        self._store().save_rule(rule, level)
        self._remember_live(rule, level)
        logger.info("Persisted HITL rule from choice=%s: %s %s", choice.value, level, rule)
        return PersistChoiceResult(ok=True, rule=rule, level=level)

    def _store(self) -> ToolPermissions:
        return ToolPermissions(project_dir=self._session.project_dir)

    def _remember_live(self, rule: str, level: PermissionLevel) -> None:
        """Add ``rule`` to the session's live evaluator, when it has one."""
        evaluator = self._session.permission_evaluator
        if evaluator is None:
            return
        parsed = PermissionRule.parse(rule)
        if parsed is None:
            return
        evaluator.remember_at(parsed, level)


__all__ = ["HitlPermissions"]
