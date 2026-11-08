"""The command line's tier, and what it set that the tiers above may not undo."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.schemas.orchestration import GroupHookMode
from igni_core.core.config.tiers.base import Tier

if TYPE_CHECKING:
    from igni_core.core.config.models import CliOverrides


class CliRatchet(BaseModel):
    """What the command line tightened, which a tier above it may not loosen.

    Read by :class:`~igni_core.core.config.tiers.group_policy.GroupPolicyTier`
    (a group can tighten a permission but never loosen one the operator denied
    on the command line) and by
    :class:`~igni_core.core.config.tiers.developer.DeveloperOnlyTier`
    (``hooks.group``). Built from the overrides themselves rather than
    inferred from the merged payload, because "the CLI denied this" and
    "something upstream happened to deny this" are different facts, and only
    the first is a ratchet.
    """

    model_config = ConfigDict(frozen=True)

    #: A permission field's tightest value, where it is not ``"deny"``.
    #: ``--read-only`` sets ``read_only: true``, and a group's
    #: ``read_only: false`` would otherwise have switched it off.
    TIGHTEST: ClassVar[Mapping[str, object]] = MappingProxyType({"read_only": True})

    #: Permission fields the command line set to their tightest value.
    denied: frozenset[str] = frozenset()
    #: ``hooks.group`` as the command line set it (``--strict`` sets ``ask``).
    hooks_group: GroupHookMode | None = None

    @classmethod
    def tightest(cls, field: str) -> object:
        """``field``'s tightest value."""
        return cls.TIGHTEST.get(field, "deny")

    @classmethod
    def of(cls, overrides: CliOverrides | None) -> CliRatchet:
        """What ``overrides`` tightened; nothing when there are none."""
        if overrides is None:
            return cls()
        permissions = overrides.permissions.to_payload()
        return cls(
            denied=frozenset(f for f, v in permissions.items() if v == cls.tightest(f)),
            hooks_group=overrides.hooks_group,
        )

    def refusals(self, permissions: Mapping[str, Any]) -> dict[str, str]:
        """The fields of ``permissions`` that would loosen a CLI deny, with the value tried."""
        return {
            field: str(permissions[field])
            for field in sorted(self.denied)
            if permissions.get(field) is not None and permissions[field] != self.tightest(field)
        }


class CliTier(Tier):
    """In-memory CLI overrides merged into the accumulator. ``None`` is a no-op."""

    def __init__(self, overrides: CliOverrides | None) -> None:
        self._overrides = overrides

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        if self._overrides is None:
            return accumulator
        return accumulator.merge(self._overrides.as_merge_dict())
