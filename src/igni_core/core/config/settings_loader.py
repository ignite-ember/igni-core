"""``SettingsLoader`` — runs the multi-tier settings merge.

Precedence (highest first — later merges win):

    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. CLI flags
    3. .igni/config.local.yaml (project, gitignored)
    4. .igni/config.yaml (project, committed)
    5. ~/.igni/settings.json (permissions fragment)
    6. ~/.igni/config.yaml (user global)
    7. Built-in defaults (from ``Settings.default_dict()``)

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line. Same precedence ordering as Claude Code's managed >
CLI > local > project > user stack.

The tier ORDER is owned by :meth:`SettingsMergePlan.default`. The
organisation's own tiers, read apart from the merge for the trust and
mode policies, are :class:`OrganisationTiers`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.merge_plan import SettingsMergePlan
from igni_core.core.config.organisation_tiers import OrganisationTiers
from igni_core.core.config.permission_eval.schemas import PermissionMode
from igni_core.core.config.settings import Settings
from igni_core.core.workspace_trust import WorkspaceTrust

if TYPE_CHECKING:
    from collections.abc import Callable

    from igni_core.core.config.cli_overrides import CliOverrides
    from igni_core.core.config.group_policy.pack import GroupPolicyPack


logger = logging.getLogger(__name__)


class SettingsLoader:
    """Runs the full multi-tier merge and applies the organisation's policies."""

    @classmethod
    def load(
        cls,
        cli_overrides: CliOverrides | None = None,
        project_dir: Path | None = None,
        group_policy_fetcher: Callable[[], GroupPolicyPack | None] | None = None,
        *,
        workspace_trust: WorkspaceTrust | None = None,
    ) -> Settings:
        """Run the full multi-tier merge and return a validated ``Settings``.

        ``workspace_trust`` is the trust service the organisation's
        ``workspace_trust`` policy is set on before the merge (whether the
        folder is trusted decides whether its config may set ``permissions``
        and ``safety``). Omitted, it is the process's own service.
        """
        organisation = OrganisationTiers(
            managed_path_provider=ManagedPolicySource.platform_path,
            group_policy_fetcher=group_policy_fetcher,
        )
        trust = workspace_trust if workspace_trust is not None else WorkspaceTrust.shared()
        trust.set_policy(organisation.workspace_trust_policy())

        plan = SettingsMergePlan.default(
            project_dir=project_dir,
            cli=cli_overrides,
            accumulator=SettingsAccumulator.from_defaults(Settings.default_dict()),
            settings_cls=Settings,
            defaults_models=Settings.defaults().models,
            workspace_trust=trust,
            managed_path_provider=organisation.managed_path,
            group_policy_fetcher=organisation.group_policy_fetcher,
        )
        settings = plan.run()
        cls._apply_mode_policy(settings, organisation)
        # Two readers compare a category level against its default rather than
        # asking who set it (``PermissionsConfig.category_rules`` and
        # ``ToolPermissions``), so a managed ``shell_execute: ask`` would read as
        # unset. Recording the organisation's fields makes them set, and final.
        settings.permissions.set_final_fields(organisation.final_permission_fields())
        return settings

    @staticmethod
    def _apply_mode_policy(settings: Settings, organisation: OrganisationTiers) -> None:
        """Record the organisation's mode policy and clamp the starting mode.

        Clamped here, after every tier, so ``--auto-approve`` cannot start a
        session in ``bypassPermissions`` when the organisation forbids it.
        """
        policy = organisation.mode_policy()
        perms = settings.permissions
        perms.set_mode_policy(policy)
        start = PermissionMode(perms.mode)
        clamped = policy.clamp_start(start)
        if clamped is not start:
            logger.warning(
                "Starting permission mode %s is not allowed by your organisation's "
                "policy; starting in %s instead.",
                start.value,
                clamped.value,
            )
            perms.mode = clamped


__all__ = ["SettingsLoader"]
