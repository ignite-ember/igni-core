"""``SettingsMergePlan`` — the ordered tier pipeline that used to
live as a hardcoded 7-step imperative body in
:meth:`SettingsLoader.load`.

Each precedence tier is a :class:`~igni_core.core.config.tiers.base.Tier`
subclass (in :mod:`igni_core.core.config.tiers`) with an ``apply`` method
that folds itself into a :class:`SettingsAccumulator`. The plan carries an
ordered list of tiers and its :meth:`SettingsMergePlan.run` walks them in
sequence. Adding a new tier is data (append a Tier instance to the list) —
not editing the middle of a method.

Precedence (highest first — later tiers in the list win because
their ``apply`` runs last):

    1. Managed policy (sysadmin-controlled, OS-specific path)
    2. Org-group policy (from the server) — with one exception: it
       cannot loosen a ``permissions`` field the CLI set to ``deny``.
       See ``GroupPolicyTier``.
    3. CLI flags
    4. .igni/config.local.yaml + settings.local.json (project)
    5. .igni/config.yaml + settings.json (project)
    6. ~/.igni/settings.local.json (permissions fragment)
    7. ~/.igni/settings.json (permissions fragment)
    8. ~/.igni/config.yaml (user global)
    9. Built-in defaults (from ``Settings.default_dict()`` — seeded
       into the accumulator BEFORE the plan runs)

Two kinds of block step outside that order: ``workspace_trust`` and
``telemetry`` come only from the organisation (``OrganisationOnlyTier``),
and ``hooks.group`` only from the developer (``DeveloperOnlyTier``).

Managed sits ABOVE CLI on purpose — the whole point is that a user
can't override an org policy by adding ``--auto-approve`` on the
command line.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.tiers.base import Tier
from igni_core.core.config.tiers.cli import CliRatchet, CliTier
from igni_core.core.config.tiers.cloud_migration import CloudMigrationTier
from igni_core.core.config.tiers.developer import DeveloperOnlyTier
from igni_core.core.config.tiers.files import JsonFragmentTier, YamlTier
from igni_core.core.config.tiers.group_policy import GroupPolicyTier
from igni_core.core.config.tiers.managed import ManagedOnlyTier, ManagedTier
from igni_core.core.config.tiers.organisation import TelemetryTier, WorkspaceTrustTier
from igni_core.core.config.tiers.project import ProjectJsonFragmentTier, ProjectYamlTier
from igni_core.core.layer import Layers
from igni_core.core.paths import home_config_dir, project_config_dir

if TYPE_CHECKING:
    from igni_core.core.config.group_policy.pack import GroupPolicyPack
    from igni_core.core.config.models import CliOverrides
    from igni_core.core.config.schemas.models import ModelsConfig
    from igni_core.core.config.settings import Settings
    from igni_core.core.workspace_trust import WorkspaceTrust


class SettingsMergePlan:
    """Ordered pipeline of :class:`Tier` instances.

    :meth:`run` walks the tiers in order, threading the
    :class:`SettingsAccumulator` through each :meth:`Tier.apply`.
    Use :meth:`default` to build the standard stack from filesystem
    paths, or construct directly for tests and one-off migrations.
    """

    def __init__(
        self,
        tiers: list[Tier],
        *,
        accumulator: SettingsAccumulator,
        settings_cls: type[Settings],
    ) -> None:
        self._tiers = tiers
        self._accumulator = accumulator
        self._settings_cls = settings_cls

    @classmethod
    def default(
        cls,
        *,
        project_dir: Path | None,
        cli: CliOverrides | None,
        accumulator: SettingsAccumulator,
        settings_cls: type[Settings],
        defaults_models: ModelsConfig,
        workspace_trust: WorkspaceTrust,
        managed_path_provider: Callable[[], Path | None] = ManagedPolicySource.platform_path,
        group_policy_fetcher: Callable[[], GroupPolicyPack | None] | None = None,
    ) -> SettingsMergePlan:
        """Build the standard plan from filesystem paths.

        Ordering here IS the precedence contract — the tier list
        below is the ONE place the merge order is specified, and
        both the loader docstring and the test-suite precedence
        assertions reference this method's output. If a future
        refactor needs to reorder tiers, this is the seam.

        ``workspace_trust`` says whether the folder is trusted; an untrusted
        folder's config may not set ``permissions`` or ``safety``.
        """
        # Through the helpers, not ``/ CONFIG_DIR`` — they fall back to
        # the pre-rename directory while that is the one on disk. Built
        # by hand here, this tier list was the one reader that could not
        # see an unmigrated install, and it fails silently: no config
        # found is indistinguishable from no config set, so a session
        # would come up on the built-in defaults without saying so.
        user_ember = home_config_dir()
        folder = project_dir if project_dir is not None else Path.cwd()
        project_ember = project_config_dir(folder)

        def project_trusted() -> bool:
            return workspace_trust.is_trusted(folder, what="permission and safety settings")

        # What the command line tightened, for the tiers above it that may
        # not loosen it: the group's deny ratchet and ``hooks.group``.
        ratchet = CliRatchet.of(cli)

        # A layer may read no settings file of its own — not the user's
        # under ``~/.igni``, not the workspace's ``.igni`` (the cloud's:
        # what it runs with comes from the image and from igni server).
        local_tiers: list[Tier] = (
            []
            if not Layers.current().reads_local_settings()
            else [
                # User global (lowest priority above built-in defaults)
                YamlTier(user_ember / "config.yaml"),
                JsonFragmentTier(user_ember / "settings.json"),
                JsonFragmentTier(user_ember / "settings.local.json"),
                # Project (committed). An untrusted folder's ``permissions``
                # and ``safety`` are dropped (``core/workspace_trust``).
                ProjectYamlTier(project_ember / "config.yaml", trusted=project_trusted),
                ProjectJsonFragmentTier(project_ember / "settings.json", trusted=project_trusted),
                # Project local (gitignored)
                ProjectYamlTier(project_ember / "config.local.yaml", trusted=project_trusted),
                ProjectJsonFragmentTier(
                    project_ember / "settings.local.json", trusted=project_trusted
                ),
            ]
        )
        tiers: list[Tier] = [
            *local_tiers,
            # CLI
            CliTier(cli),
            # ``workspace_trust`` back to its defaults: only the organisation
            # tiers below (and the environment) may set it.
            WorkspaceTrustTier(),
            # ``telemetry`` likewise: where traces go is the organisation's call.
            TelemetryTier(),
            # Org-group policy — wins over CLI, loses to sysadmin
            # ManagedPolicy. Handed the CLI's ratchet so a group tightens
            # freely but cannot loosen a permission the operator denied on
            # the command line. See ``GroupPolicyTier``.
            *(
                [GroupPolicyTier(fetcher=group_policy_fetcher, ratchet=ratchet)]
                if group_policy_fetcher
                else []
            ),
            # Managed policy — last, so it wins over CLI and group policy.
            ManagedTier(path_provider=managed_path_provider),
            # The settings that decide whether group policy is enforced at
            # all: from the managed file (or the environment), or not at all.
            ManagedOnlyTier(path_provider=managed_path_provider),
            # And the settings only the developer may set — ``hooks.group``,
            # whether the group's hooks run here: from their own config and
            # command line, whatever the tiers above said.
            DeveloperOnlyTier(
                user_ember / "config.yaml" if local_tiers else None,
                ratchet=ratchet,
            ),
            # Migration — replaces cloud rows with shipping defaults.
            CloudMigrationTier(defaults=defaults_models),
        ]
        return cls(
            tiers,
            accumulator=accumulator,
            settings_cls=settings_cls,
        )

    def run(self) -> Settings:
        """Execute the tier pipeline and return the validated
        :class:`Settings`. Shape errors surface here (via the final
        ``Settings(**payload)`` validation) rather than mid-pipeline.
        """
        accumulator = self._accumulator
        for tier in self._tiers:
            accumulator = tier.apply(accumulator)
        return accumulator.to_settings(self._settings_cls)
