"""What the host has observed and the backend does not keep."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from igni_core.extensions.host.broadcasts.approvals import ApprovalMirror
from igni_core.extensions.host.broadcasts.runs import RunLedger
from igni_core.extensions.host.broadcasts.status import StatusMemory
from igni_core.extensions.host.values import UpdateInfo


class ViewOwners:
    """Which extension appended which view — so only it replaces it."""

    def __init__(self) -> None:
        self._by_view: dict[str, str] = {}

    def own(self, view_id: str, extension_id: str) -> None:
        self._by_view[view_id] = extension_id

    def owner_of(self, view_id: str) -> str | None:
        return self._by_view.get(view_id)


class HostState(BaseModel):
    """The host's own memory: the last update check, approvals, runs, views, status."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: The last update check's answer.
    update: UpdateInfo | None = None
    #: Approvals waiting, by session (:class:`ApprovalMirror`).
    approvals: ApprovalMirror = Field(default_factory=ApprovalMirror)
    #: Each session's last top-level run, and the files its current one edited.
    runs: RunLedger = Field(default_factory=RunLedger)
    #: Which extension appended which view.
    view_owners: ViewOwners = Field(default_factory=ViewOwners)
    #: The last status seen, by session.
    status: StatusMemory = Field(default_factory=StatusMemory)

    def record_update(self, update: UpdateInfo | None) -> None:
        """The update check answered."""
        self.update = update


__all__ = ["HostState", "ViewOwners"]
