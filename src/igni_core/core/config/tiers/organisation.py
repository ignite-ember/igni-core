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

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        defaults = self.defaults()
        payload = dict(accumulator.payload)
        block = payload.get(self.BLOCK)
        if isinstance(block, dict) and any(
            key in defaults and block[key] != defaults[key] for key in block
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
