"""``DeveloperOnlyTier`` — settings only the developer may set."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.config_io import YamlSource
from igni_core.core.config.schemas.orchestration import GROUP_HOOK_MODES
from igni_core.core.config.tiers.base import Tier
from igni_core.core.config.tiers.cli import CliRatchet

logger = logging.getLogger(__name__)


class DeveloperOnlyTier(Tier):
    """Settings only the developer may set: their own config and their command line.

    The mirror of
    :class:`~igni_core.core.config.tiers.organisation.OrganisationOnlyTier`.
    ``hooks.group`` decides whether the hooks an organisation's group policy
    ships run on this machine. Those hooks run ``bash -c`` outside the agent's
    permissions, and the person a developer needs protecting from there is
    whoever administers the organisation — who writes the group policy, and
    may well write the managed file too. So the group pack, the managed file
    and a project's config (whoever wrote the repository) cannot set it: a
    value from any of them is discarded, with a warning, and what stands is
    the stricter of ``~/.igni/config.yaml`` and the command line
    (``--strict`` means ``ask``).

    Runs after every tier that could have set it.
    """

    #: ``(block, field)`` for each developer-only setting.
    FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (("hooks", "group"),)

    def __init__(self, user_config: Path | None, ratchet: CliRatchet | None = None) -> None:
        #: ``~/.igni/config.yaml``, or ``None`` where this layer reads no
        #: local settings (the cloud's).
        self._user_config = user_config
        self._ratchet = ratchet or CliRatchet()

    def _user_value(self) -> str | None:
        if self._user_config is None:
            return None
        block = YamlSource(self._user_config).load().get("hooks")
        value = block.get("group") if isinstance(block, dict) else None
        if value is not None and value not in GROUP_HOOK_MODES:
            logger.warning(
                "%s sets hooks.group to %r; it must be one of %s. Using 'ask'.",
                self._user_config,
                value,
                ", ".join(GROUP_HOOK_MODES),
            )
            # A typo in a safety setting is read as the cautious middle,
            # not as the permissive default.
            return "ask"
        return value

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        chosen = [
            v for v in (self._user_value(), self._ratchet.hooks_group) if v in GROUP_HOOK_MODES
        ]
        value = max(chosen, key=GROUP_HOOK_MODES.index, default="run")

        payload = dict(accumulator.payload)
        block = dict(payload.get("hooks") or {})
        if "group" in block and block["group"] != value:
            logger.warning(
                "hooks.group may be set only in your own ~/.igni/config.yaml or on the "
                "command line; %r from a project, the group policy or the managed file "
                "is ignored, and %r stands.",
                block["group"],
                value,
            )
        block["group"] = value
        payload["hooks"] = block
        return SettingsAccumulator(payload=payload)
