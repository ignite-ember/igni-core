"""Settings blocks only the organisation may set: ``workspace_trust`` and ``telemetry``."""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.schemas.telemetry import OtlpConfig, TelemetryConfig
from igni_core.core.config.tiers.base import Tier
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig

logger = logging.getLogger(__name__)


class OrganisationOnlyTier(Tier):
    """A settings block that comes from the organisation, never from below it.

    Runs after the CLI tier and before the group and managed tiers: whatever
    the user's config, a project's or the command line said is put back to the
    defaults, and only the group policy pack, the managed file and the
    environment (applied by the block's own model) can set it. Subclasses name
    the block and how to build its defaults.
    """

    #: The top-level settings key.
    BLOCK: ClassVar[str]

    def defaults(self) -> dict[str, Any]:
        """The block's defaults, without its environment."""
        raise NotImplementedError

    def environment_defaults(self) -> dict[str, Any]:
        """The block's defaults with the environment applied.

        The built-in defaults the settings start from already carry the
        environment, so a value equal to one of these came from the
        environment, not from a user, a project or the CLI.
        """
        raise NotImplementedError

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        defaults = self.defaults()
        from_environment = self.environment_defaults()
        payload = dict(accumulator.payload)
        block = payload.get(self.BLOCK)
        if isinstance(block, dict) and any(
            key in defaults
            and block[key] != defaults[key]
            and block[key] != from_environment.get(key)
            for key in block
        ):
            logger.warning(
                "%s may be set only in the managed-settings file, the group policy or "
                "the environment; the value from user, project or CLI settings is ignored.",
                self.BLOCK,
            )
        payload[self.BLOCK] = defaults
        return SettingsAccumulator(payload=payload)


class WorkspaceTrustTier(OrganisationOnlyTier):
    """``workspace_trust`` comes from the organisation, never from below it.

    The environment may only tighten it (``WorkspaceTrustConfig``). A
    repository that could set this could trust itself; a person who could set
    it could opt out of their organisation's rule.
    """

    BLOCK = "workspace_trust"

    def defaults(self) -> dict[str, Any]:
        return WorkspaceTrustConfig.model_construct().model_dump()

    def environment_defaults(self) -> dict[str, Any]:
        return WorkspaceTrustConfig().model_dump()


class WorkspaceTrustFailsClosedTier(Tier):
    """An invalid ``workspace_trust`` from the organisation means ``never``.

    Runs after the group and managed tiers. A policy block the organisation
    got wrong (an unknown mode, a remote list that is not a list) would
    otherwise fail the whole settings load with a validation traceback, and
    no command would start; it fails closed instead, every folder untrusted,
    as ``OrganisationTiers.workspace_trust_policy`` decides for the gate.
    """

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        block = accumulator.payload.get(WorkspaceTrustTier.BLOCK)
        try:
            WorkspaceTrustConfig.model_validate(block)
        except ValueError as exc:  # pydantic's ValidationError is a ValueError
            logger.warning(
                "workspace_trust in the organisation's settings is invalid (%s); "
                "treating every folder as untrusted.",
                exc,
            )
            payload = dict(accumulator.payload)
            payload[WorkspaceTrustTier.BLOCK] = WorkspaceTrustConfig(mode="never").model_dump()
            return SettingsAccumulator(payload=payload)
        return accumulator


class TelemetryTier(OrganisationOnlyTier):
    """``telemetry`` (the OTLP collector) comes from the organisation.

    A repository that could set it could send whoever opens it's traces to a
    host of the repository's choosing, and with ``capture_content`` their
    prompts and code; a person could do the same to their organisation. The
    ``OTEL_*`` environment fills what the organisation left unset
    (``OtlpConfig``).
    """

    BLOCK = "telemetry"

    def defaults(self) -> dict[str, Any]:
        return TelemetryConfig.model_construct(otlp=OtlpConfig.model_construct()).model_dump()

    def environment_defaults(self) -> dict[str, Any]:
        return TelemetryConfig().model_dump()
