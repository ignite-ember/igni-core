"""What the host model is built from: the inputs to :meth:`HostModel.build`."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SkipValidation

from igni_core.core.decisions import Laya
from igni_core.extensions.host.backend import HostBackend
from igni_core.extensions.host.definition import HOST_SCHEMA
from igni_core.extensions.host.spec import HostSchema
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.extensions.store import ExtensionStore
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver


class HostModelConfig(BaseModel):
    """The raw inputs the composition root wires into a host model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    backend: SkipValidation[HostBackend]
    registry: SkipValidation[ExtensionRegistry]
    #: The extensions' saved settings; a fresh store when not given.
    store: SkipValidation[ExtensionStore | None] = None
    #: For ``host.notifications.notify`` — the same check a notice
    #: arriving over MCP passes.
    notices: SkipValidation[ExtensionNoticeReceiver | None] = None
    #: Laya, for ``host.decide``; the process's own (``process_laya``) unless a test
    #: brings its own.
    decisions: SkipValidation[Laya | None] = None
    #: The schema every request and signal is checked against.
    host_schema: HostSchema = HOST_SCHEMA
    #: Check every answer against the schema and fail loudly when it does
    #: not fit. For tests: in production an answer is sent as the backend
    #: gave it. A test-only flag on a production config; to be replaced
    #: by an answer checker the test rig injects.
    strict: bool = False


__all__ = ["HostModelConfig"]
