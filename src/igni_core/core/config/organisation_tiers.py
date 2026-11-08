"""``OrganisationTiers`` — the managed file and group pack, read on their own.

The merge folds every tier into one ``Settings``, which cannot say where a
value came from. Three decisions need the organisation's tiers alone: the
workspace-trust policy (read *before* the merge, since it decides whether
the folder's config may set ``permissions`` and ``safety``), the mode policy,
and which ``permissions`` fields are final. This class reads both tiers once
per load and is handed to the merge plan as its managed-path provider and
group-policy fetcher, so the decisions see the very file and pack the merge
used rather than a second fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from igni_core.core.config.config_io import DictMerger, YamlSource
from igni_core.core.config.permission_eval.mode_policy import ModePolicy
from igni_core.core.config.schemas.workspace_trust import WorkspaceTrustConfig

if TYPE_CHECKING:
    from igni_core.core.config.group_policy.pack import GroupPolicyPack


logger = logging.getLogger(__name__)


class OrganisationTiers:
    """The managed-settings file and the group pack of one settings load."""

    def __init__(
        self,
        *,
        managed_path_provider: Callable[[], Path | None],
        group_policy_fetcher: Callable[[], GroupPolicyPack | None] | None,
    ) -> None:
        self._managed_path_provider = managed_path_provider
        self._group_policy_fetcher = group_policy_fetcher
        self._managed_path_read = False
        self._managed_path: Path | None = None
        self._managed_payload: dict[str, Any] | None = None
        self._pack_fetched = False
        self._pack: GroupPolicyPack | None = None

    def managed_path(self) -> Path | None:
        """Where the managed-settings file is, asked of the provider once."""
        if not self._managed_path_read:
            self._managed_path = self._managed_path_provider()
            self._managed_path_read = True
        return self._managed_path

    def group_pack(self) -> GroupPolicyPack | None:
        """The organisation's group pack, fetched once; ``None`` without a fetcher."""
        if self._group_policy_fetcher is None:
            return None
        if not self._pack_fetched:
            self._pack = self._group_policy_fetcher()
            self._pack_fetched = True
        return self._pack

    @property
    def group_policy_fetcher(self) -> Callable[[], GroupPolicyPack | None] | None:
        """What the merge plan fetches the pack with: :meth:`group_pack`, when there is a fetcher."""
        return self.group_pack if self._group_policy_fetcher is not None else None

    def managed_settings(self) -> dict[str, Any]:
        """The managed file's settings; empty when none is deployed."""
        if self._managed_payload is None:
            path = self.managed_path()
            payload = YamlSource(path).load() if path is not None else {}
            self._managed_payload = payload if isinstance(payload, dict) else {}
        return self._managed_payload

    def group_settings(self) -> dict[str, Any]:
        """The group pack's settings; empty when there is no pack."""
        pack = self.group_pack()
        return pack.to_settings_dict() if pack is not None else {}

    def workspace_trust_policy(self) -> WorkspaceTrustConfig:
        """``workspace_trust`` from the group pack, overridden by the managed file.

        A malformed policy fails closed: every folder is untrusted.
        """
        block: dict[str, Any] = {}
        for payload in (self.group_settings(), self.managed_settings()):
            tier_block = payload.get("workspace_trust")
            if isinstance(tier_block, dict):
                block = DictMerger.deep(block, tier_block)
        try:
            return WorkspaceTrustConfig.model_validate(block)
        except Exception as exc:  # noqa: BLE001 — a malformed policy fails closed
            logger.warning(
                "workspace_trust in the organisation's settings is invalid (%s); "
                "treating every folder as untrusted.",
                exc,
            )
            return WorkspaceTrustConfig(mode="never")

    def mode_policy(self) -> ModePolicy:
        """``allow_bypass`` and the mode floor, from these two tiers alone."""
        return ModePolicy.from_tiers(self.managed_settings(), self.group_settings())

    def final_permission_fields(self) -> frozenset[str]:
        """The ``permissions`` fields either tier sets — final for every lower tier."""
        final: set[str] = set()
        for payload in (self.managed_settings(), self.group_settings()):
            perms = payload.get("permissions")
            if isinstance(perms, dict):
                final.update(str(key) for key in perms)
        return frozenset(final)


__all__ = ["OrganisationTiers"]
