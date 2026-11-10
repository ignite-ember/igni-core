"""Whether to sandbox, and whether running unsandboxed is allowed at all.

Read from the tiers that may set each switch rather than from the merged
:class:`~igni_core.core.config.settings.Settings`, because the merge
cannot say where a value came from, and *where* is the whole rule here
(:mod:`igni_core.core.config.schemas.extensions`):

==================  ============  =====  ============  =========  =======
setting             managed file  env    group policy  user file  project
==================  ============  =====  ============  =========  =======
``sandbox``         yes           —      —             yes        **no**
``require_sandbox`` yes           yes    yes           **no**     **no**
==================  ============  =====  ============  =========  =======

The group policy is the organisation's pack as igni server signed it and
igni verified it (:class:`~igni_core.core.config.group_policy.settings_fetcher.CachedSettingsFetcher`),
never the cached files beside it: a pack that does not verify sets
nothing. It may require the sandbox; it may not turn it off.

A project's value for either is never read: a repository somebody cloned
must not be able to unconfine the extensions that run beside it.

Each tier's block is read as an
:class:`~igni_core.core.config.schemas.extensions.ExtensionsConfig` — the
model the rest of igni validates settings with — so a switch is only ever
the value that model says it is, and a block that does not validate
applies nothing at all rather than half of it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from igni_core.core.config.config_io import YamlSource
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.settings_fetcher import GroupPolicyUnavailable
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.schemas.extensions import ExtensionsConfig
from igni_core.core.layer import layers
from igni_core.core.paths import home_config_dir

logger = logging.getLogger(__name__)

#: ``1`` makes a sandbox mandatory, as ``require_sandbox: true`` in the
#: managed file does.
REQUIRE_SANDBOX_ENV = "IGNI_EXTENSIONS_REQUIRE_SANDBOX"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class SandboxSettings(BaseModel):
    """The two switches, each from where it may come."""

    model_config = ConfigDict(frozen=True)

    #: Confine extension processes. ``False`` only from managed or user.
    enabled: bool = True
    #: Refuse to run one unconfined. Only from managed or the env.
    required: bool = False
    #: Where ``enabled`` was turned off, for the log: ``managed`` or ``user``.
    disabled_by: str = ""

    @classmethod
    def load(
        cls,
        *,
        managed_path: Callable[[], Path | None] = ManagedPolicySource.platform_path,
        user_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        group: Callable[[], GroupPolicyPack | None] | None = None,
    ) -> SandboxSettings:
        """Read the managed file, the env and the user's file.

        Args:
            managed_path: Where the managed policy is (injectable for tests).
            user_file: The user's ``config.yaml``; ``~/.igni/config.yaml``
                by default, and none when this layer reads no local settings.
            group: Returns the verified group-policy pack, or ``None``.
        """
        env = os.environ if environ is None else environ
        if user_file is None and layers.current().reads_local_settings():
            user_file = home_config_dir() / "config.yaml"
        managed = cls._file_block(managed_path())
        user = cls._file_block(user_file)
        by_group = cls._group_block(group)

        required = (
            managed.require_sandbox
            or env.get(REQUIRE_SANDBOX_ENV, "").strip().lower() in _TRUTHY
            or by_group.require_sandbox
        )
        if not by_group.sandbox:
            logger.warning(
                "extensions.sandbox: false in the group policy is ignored: an "
                "organisation's pack may require the sandbox, not turn it off"
            )
        if "require_sandbox" in user.model_fields_set:
            logger.warning(
                "extensions.require_sandbox in %s is ignored: only the managed "
                "settings file or %s may set it",
                user_file,
                REQUIRE_SANDBOX_ENV,
            )

        disabled_by = ""
        if not managed.sandbox:
            disabled_by = "managed"
        elif "sandbox" not in managed.model_fields_set and not user.sandbox:
            disabled_by = "user"
        if disabled_by and required:
            logger.warning(
                "extensions.sandbox: false (%s settings) is ignored because "
                "extensions.require_sandbox is set",
                disabled_by,
            )
            disabled_by = ""
        return cls(enabled=not disabled_by, required=required, disabled_by=disabled_by)

    @staticmethod
    def _file_block(path: Path | None) -> ExtensionsConfig:
        """The ``extensions`` block of one YAML file, validated."""
        if path is None:
            return ExtensionsConfig()
        try:
            raw = YamlSource(path).load().get("extensions")
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            logger.warning("could not read %s: %s", path, exc)
            return ExtensionsConfig()
        return SandboxSettings._validated(raw, str(path))

    @staticmethod
    def _group_block(group: Callable[[], GroupPolicyPack | None] | None) -> ExtensionsConfig:
        """The ``extensions`` block of the verified group-policy pack.

        No pack, or a pack the organisation required and that cannot be
        had, sets nothing — the second says so in a warning. Anything
        else is a bug, and is not swallowed.
        """
        if group is None:
            return ExtensionsConfig()
        try:
            pack = group()
        except (GroupPolicyUnavailable, OSError) as exc:
            logger.warning("group policy not read for extensions: %s", exc)
            return ExtensionsConfig()
        if pack is None:
            return ExtensionsConfig()
        return SandboxSettings._validated(
            pack.to_settings_dict().get("extensions"), "the group policy"
        )

    @staticmethod
    def _validated(raw: object, where: str) -> ExtensionsConfig:
        """One tier's ``extensions`` block, validated.

        A block that is not a mapping, or that does not validate, is
        ignored whole: nothing in it applies.
        """
        if not isinstance(raw, dict):
            return ExtensionsConfig()
        try:
            return ExtensionsConfig.model_validate(raw)
        except ValidationError as exc:
            logger.warning("extensions block in %s is invalid and ignored: %s", where, exc)
            return ExtensionsConfig()


__all__ = ["REQUIRE_SANDBOX_ENV", "SandboxSettings"]
