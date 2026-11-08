"""Top-level orchestrator: :class:`ProjectInitializer`.

Owns the state (project dir + home dir + :class:`InitConfig`) and
the flow (bootstrap → migrate → first-run writes → checksum sync →
hook provisioning). Every free function from the pre-refactor
``core/init.py`` becomes an instance method here — no more threading
``project_dir`` through seven function signatures.

Delegates:

* Home-config bootstrap + migration →
  :class:`igni_core.core.init.home_migrator.HomeConfigMigrator`.
* Hook script + settings.json provisioning →
  :class:`igni_core.core.init.hook_provisioner.HookProvisioner`.
* Checksum-based agents/skills sync →
  :class:`igni_core.core.init.checksum_store.ChecksumStore`.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from igni_core.core.config.secret_scan import scan_project_config
from igni_core.core.dir_migration import migrate_project
from igni_core.core.init.checksum_store import ChecksumStore
from igni_core.core.init.gitignore import IgniGitignore
from igni_core.core.init.home_migrator import HomeConfigMigrator
from igni_core.core.init.hook_provisioner import HookProvisioner
from igni_core.core.init.schemas import (
    InitConfig,
    InitResult,
    SettingsFile,
)
from igni_core.core.init_templates import (
    IGNI_MD_TEMPLATE,
    PROJECT_CONFIG_TEMPLATE,
)
from igni_core.core.paths import CONFIG_DIR, PROJECT_CONTEXT_FILE
from igni_core.core.workspace_trust import WorkspaceTrust

logger = logging.getLogger(__name__)


class ProjectInitializer(BaseModel):
    """Initialise + update a project's ``.igni`` directory.

    Two responsibilities:

    1. **First-run init** — copies built-in agents/skills/hooks
       into ``.igni/`` and creates a starter ``igni.md``. A marker
       file (``.igni/.initialized``) ensures this only runs once.
    2. **Update on every start** — compares package files against
       local copies using SHA-256 checksums. Untouched files are
       overwritten; user-modified files trigger a warning + a
       ``.new`` sidecar.

    :meth:`run` is the entry point. ``trust`` is the workspace-trust
    service the caller already holds: the starter settings and the
    ``.gitignore`` block are written only in a folder it trusts.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    project_dir: Path
    trust: WorkspaceTrust
    config: InitConfig = Field(default_factory=InitConfig)
    home_igni: Path = Field(default_factory=lambda: Path.home() / CONFIG_DIR)

    # ── Public entry point ────────────────────────────────────────

    def run(self) -> InitResult:
        """Run the full init-and-update flow.

        ``InitResult.first_run`` is ``True`` when this run performed
        first-time init on this project.
        """
        # Before the mkdir below, which would otherwise create an empty
        # ``.igni`` beside the project's existing ``.igni`` — and the
        # migration refuses to merge two directories, so that empty one
        # would block the rename permanently.
        migrate_project(self.project_dir)

        self.home_igni.mkdir(parents=True, exist_ok=True)
        (self.project_dir / CONFIG_DIR).mkdir(parents=True, exist_ok=True)

        home_marker = self.home_igni / self.config.marker_file
        project_marker = self.project_dir / CONFIG_DIR / self.config.marker_file

        migrator = HomeConfigMigrator(home_igni=self.home_igni)

        # First-ever run on this machine: write the starter home config.
        if not home_marker.exists():
            migrator.bootstrap_default_config()
            home_marker.touch()

        # Migrate stale defaults from older versions — runs every
        # startup. Cheap and idempotent (byte-stable no-op once
        # migrated).
        migration = migrator.migrate()

        # First-time project init: create starter files.
        first_run = not project_marker.exists()
        if first_run:
            self._write_ember_md()
            self._write_project_config()
            self._write_project_settings()
            project_marker.touch()

        # Every run, so the list stays current as igni learns new runtime
        # files; it rewrites only its own block.
        self._write_gitignore()

        # Sync built-in agents/skills — checksum-based so user edits
        # are preserved.
        warnings = self._update_built_in_files()
        # A credential in the project's shareable config. Reported here
        # because this is the one place that already surfaces warnings to
        # whoever started the session, and the person who has to move a
        # key is often not them — so it needs to be visible rather than
        # only logged at debug.
        warnings.extend(scan_project_config(self.project_dir))
        HookProvisioner(
            project_dir=self.project_dir,
            register_in_settings=not self.config.skip_builtin_hook_registration,
            skip_entirely=self.config.group_ships_hook_scripts,
        ).provision()

        for msg in warnings:
            logger.info(msg)

        return InitResult(
            first_run=first_run,
            warnings=warnings,
            migration=migration,
        )

    # ── Starter-file writers (first-run only) ─────────────────────

    def _write_ember_md(self) -> None:
        """Write a starter ``igni.md`` if one doesn't exist."""
        path = self.project_dir / PROJECT_CONTEXT_FILE
        if not path.exists():
            path.write_text(IGNI_MD_TEMPLATE)

    def _write_project_config(self) -> None:
        """Write a starter ``.igni/config.yaml`` with commented-out options."""
        path = self.project_dir / CONFIG_DIR / "config.yaml"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(PROJECT_CONFIG_TEMPLATE)

    def _write_project_settings(self) -> None:
        """Write a starter ``.igni/settings.local.json`` with default
        permissions.

        Gives users a template they can customise for their project.
        The file is gitignored so each user can have their own
        overrides. Team defaults can go in ``.igni/settings.json``
        (committed).

        Only writes the ``permissions`` block if the file doesn't
        already declare one — respects a user who has pre-seeded
        their own permissions.
        """
        path = self.project_dir / CONFIG_DIR / "settings.local.json"

        # Not in a folder nobody has trusted. Its permissions are ignored
        # there (``ProjectJsonFragmentTier``), so the starter igni wrote on
        # the first run was the cause of a warning on every run after it —
        # printed twice per ``igni -m``, about a file the person never
        # wrote. Found on a fresh clone; a new user's first folder is an
        # untrusted one.
        if not self.trust.is_trusted(self.project_dir):
            return

        # A file that does not validate is the person's to fix — writing
        # the starter over it would drop everything else it declares.
        settings = SettingsFile.load_if_valid(path)
        if settings is None or settings.permissions is not None:
            return

        settings.permissions = self.config.default_permissions.model_copy(deep=True)
        settings.save(path)

    def _write_gitignore(self) -> None:
        """Keep ``.igni/.gitignore``'s block of personal and runtime files current.

        Not in a folder nobody has trusted, for the reason
        :meth:`_write_project_settings` gives: igni should not add files to a
        repository the person has not said they work in. Written on the first
        run after the folder is trusted.
        """
        if not self.trust.is_trusted(self.project_dir):
            return
        IgniGitignore(self.project_dir / CONFIG_DIR).ensure()

    # ── Checksum-based update (agents / skills) ───────────────────

    def _update_built_in_files(self) -> list[str]:
        """Sync built-in agents and skills using checksum-based merge.

        Returns a list of warning messages for files that were
        modified by the user and could not be auto-updated.
        """
        store = ChecksumStore.load(self.project_dir, self.config)
        warnings: list[str] = []

        # Update agents — unless the person's group ships its own, in
        # which case it is the source for this directory and ours would
        # scaffold back the very agents an admin removed.
        agents_src = self.config.package_dir / "bundled_agents"
        agents_dst = self.project_dir / CONFIG_DIR / "agents"
        if agents_src.exists() and not self.config.skip_bundled_agents:
            agents_dst.mkdir(parents=True, exist_ok=True)
            for src_file in agents_src.glob("*.md"):
                key = f"agents/{src_file.name}"
                dst_file = agents_dst / src_file.name
                outcome = store.sync_file(src_file, dst_file, key)
                if msg := outcome.warning_message():
                    warnings.append(msg)

        # Update skills
        skills_src = self.config.package_dir / "bundled_skills"
        skills_dst = self.project_dir / CONFIG_DIR / "skills"
        if skills_src.exists():
            for skill_dir in skills_src.iterdir():
                if not skill_dir.is_dir():
                    continue
                src_file = skill_dir / "SKILL.md"
                if not src_file.exists():
                    continue
                key = f"skills/{skill_dir.name}/SKILL.md"
                dst_dir = skills_dst / skill_dir.name
                dst_dir.mkdir(parents=True, exist_ok=True)
                dst_file = dst_dir / "SKILL.md"
                outcome = store.sync_file(src_file, dst_file, key)
                if msg := outcome.warning_message():
                    warnings.append(msg)

        store.save()
        return warnings
