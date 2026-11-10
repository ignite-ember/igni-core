"""What loading settings changes for the whole process."""

from __future__ import annotations

import os

from igni_core.core import air_gap
from igni_core.core.agno_telemetry import AgnoTelemetry
from igni_core.core.config.settings import Settings
from igni_core.core.db.scope import Tenant, tenancy
from igni_core.core.observability import OtelRuntime
from igni_core.extensions.principal import SignedInPerson


class ProcessEnvironment:
    """The process-global side effects of a backend's settings, in one place."""

    @staticmethod
    def enter(settings: Settings, *, tenant: Tenant | None, person: SignedInPerson) -> None:
        """Apply ``settings`` to the process and say whose rows it keeps.

        ``tenant`` is the one a deployment layer bound (the cloud's: the
        producer its VM is for); without one, the person signed in here.
        """
        # The processes this backend starts (the decisions service above
        # all, which reads no settings) inherit the air-gap decision.
        air_gap.export_to(os.environ, settings.air_gapped)
        # Agno's own run telemetry stays off when air-gapped, whatever the
        # environment said; traces go to the organisation's collector, if
        # it named one (``core/observability``).
        AgnoTelemetry.enforce(os.environ, air_gapped=settings.air_gapped)
        OtelRuntime.start(settings)
        # Asked only when ``IGNI_DATABASE_URL`` names a shared database
        # (docs/CLOUD.md §9); the desktop's files are nobody else's.
        tenancy.use((lambda: tenant) if tenant is not None else person.tenant)
