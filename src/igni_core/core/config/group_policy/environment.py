"""The group-policy settings an administrator may set from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class GroupPolicyEnvironment(BaseModel):
    """``IGNI_GROUP_POLICY_TRUSTED_KEYS`` and ``IGNI_GROUP_POLICY_REQUIRED``, read.

    Both are managed-only settings (``ManagedOnlyTier`` in ``merge_plan.py``):
    the environment can pin keys and turn ``required`` on, never off.
    """

    model_config = ConfigDict(frozen=True)

    #: Pins: comma- or whitespace-separated RFC 7638 thumbprints.
    TRUSTED_KEYS_VAR: ClassVar[str] = "IGNI_GROUP_POLICY_TRUSTED_KEYS"
    #: ``1``/``true``/``yes``/``on`` makes a verified pack required.
    REQUIRED_VAR: ClassVar[str] = "IGNI_GROUP_POLICY_REQUIRED"
    _TRUE: ClassVar[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

    pins: tuple[str, ...] = ()
    required: bool = False

    @classmethod
    def read(cls, environ: Mapping[str, str] | None = None) -> GroupPolicyEnvironment:
        """What ``environ`` (the process environment by default) sets."""
        source = os.environ if environ is None else environ
        return cls(
            pins=tuple(source.get(cls.TRUSTED_KEYS_VAR, "").replace(",", " ").split()),
            required=source.get(cls.REQUIRED_VAR, "").strip().lower() in cls._TRUE,
        )
