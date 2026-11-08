"""Where a pack is cached for this install, and the files under it."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.paths import home_config_dir

logger = logging.getLogger(__name__)


class EntryPlacement(BaseModel):
    """Where one entry is written, or why it may not be."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    path: Path | None = None
    reason: str = ""


class GroupPolicyLayout(BaseModel):
    """The cache directory, derived once.

    The writer (the backend's hydration) and every reader (settings load,
    the session's loaders, the status panels) have to land on the same
    directory, or a reader finds an empty one and reports "no policy" for a
    group that has one. So the derivation lives here, and nothing else joins
    ``group-policy`` onto a path.

    Pure: nothing here creates a directory except :meth:`ensure_dir`.
    """

    model_config = ConfigDict(frozen=True)

    #: The directory under ``storage.data_dir`` a pack is cached in.
    DIRECTORY: ClassVar[str] = "group-policy"
    #: The pack exactly as igni server signed it (a compact JWS).
    SIGNED_PACK_FILE: ClassVar[str] = "pack.jws"
    #: igni server's JWKS as fetched beside the pack, for offline starts.
    #: Without a pinned key (``group_policy.trusted_keys``) this copy is only
    #: as trustworthy as this directory.
    JWKS_FILE: ClassVar[str] = "jwks.json"
    #: The newest pack version this install has accepted, for refusing a
    #: rollback to an older one.
    STATE_FILE: ClassVar[str] = "pack_state.json"
    #: Identity and freshness of the pack, for the frontend and the TTL.
    META_FILE: ClassVar[str] = "pack_meta.json"
    #: Where older versions persisted the pack's merged ``settings`` entries,
    #: unsigned. Never read any more, and removed whenever a pack is written.
    LEGACY_SETTINGS_FILE: ClassVar[str] = "group_settings.json"
    #: The merged hook declarations, under the ``hooks`` kind's directory.
    HOOKS_FILE: ClassVar[str] = "settings.json"

    root: Path

    @classmethod
    def for_data_dir(cls, data_dir: str | Path | None) -> GroupPolicyLayout:
        """The layout under ``storage.data_dir``.

        A missing or nonsensical ``data_dir`` falls back to
        :func:`~igni_core.core.paths.home_config_dir` — the directory
        ``storage.data_dir`` itself defaults to, and migration-aware, so an
        install still on ``~/.ember`` is not told it has no policy. Anything
        that is not a path is *ignored* rather than coerced: a caller holding
        a stub settings object gets the default, not ``Path('<MagicMock ...>')``.
        """
        if isinstance(data_dir, (str, Path)) and str(data_dir).strip():
            return cls(root=Path(data_dir).expanduser() / cls.DIRECTORY)
        return cls(root=home_config_dir() / cls.DIRECTORY)

    def file(self, name: str) -> Path:
        """One of the files at the root — the ``*_FILE`` names above."""
        return self.root / name

    def dir_for(self, kind: str) -> Path:
        """The directory this kind's entries live in. Not created."""
        return self.root / kind

    def shipped_dir(self, kind: str) -> Path | None:
        """This kind's directory when the pack ships something of it, else ``None``.

        ``None`` rather than a path that may not exist, so a loader can tell
        "the group ships nothing" from "we passed the wrong path".
        """
        return self.dir_for(kind) if self.ships(kind) else None

    def ships(self, kind: str) -> bool:
        """Whether the cached pack carries anything of this kind."""
        directory = self.dir_for(kind)
        try:
            return directory.is_dir() and any(directory.iterdir())
        except OSError as exc:
            logger.debug("Could not inspect the group %s directory: %s", kind, exc)
            return False

    def ensure_dir(self, kind: str) -> Path:
        """The directory this kind's entries live in, created on demand."""
        directory = self.dir_for(kind)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def hooks_file(self) -> Path:
        return self.dir_for("hooks") / self.HOOKS_FILE

    def plugin_dir(self, entry_name: str) -> Path | None:
        """Where a plugin the pack installs lives, or ``None`` for an unsafe name."""
        check = GroupPolicyEntry.check_name(entry_name)
        return self.dir_for("plugins") / check.name if check.ok else None

    def place(self, kind: str, filename_template: str, entry: GroupPolicyEntry) -> EntryPlacement:
        """Where one entry of ``kind`` is written, by its kind's filename template.

        Refused for a name that would leave the cache directory — see
        :meth:`GroupPolicyEntry.check_name`. Then checked again on the
        resolved path: the template may own a separator (a skill is
        ``{name}/SKILL.md``), so the property is "under the kind's
        directory", not "directly in it".
        """
        check = entry.safe_name()
        if not check.ok:
            return EntryPlacement(ok=False, reason=check.reason)
        directory = self.ensure_dir(kind)
        path = directory / filename_template.format(name=check.name)
        root = directory.resolve()
        resolved = path.resolve()
        if root != resolved and root not in resolved.parents:
            return EntryPlacement(
                ok=False, reason=f"entry name {entry.entry_name!r} would write outside {root}"
            )
        return EntryPlacement(ok=True, path=path)
