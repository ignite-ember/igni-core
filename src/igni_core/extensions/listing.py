"""What igni server's listing carried: the priority order and the group.

The super admin's order of extensions settles every collision between
them, and the signed-in person's group says which extensions igni
listed to them. Both arrive with the listing, and both are kept so
the next start can apply them before igni server has been asked
anything: a machine that cannot reach igni still settles collisions
the way the super admin ordered.

Where the layer keeps no files of it (the cloud's) the listing is
kept in this store's memory only: it is igni server's, read-only in
the VM, and a VM starts by asking again. What it keeps in memory
belongs to the store that received it, so two stores over one root
do not see each other's listing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from igni_core.core.layer import layers

logger = logging.getLogger(__name__)

#: The super admin's priority order of extensions, as the last
#: distribution listing carried it.
PRIORITY_FILE = ".priority.json"

#: The signed-in person's group, as the last distribution listing
#: carried it: who this machine's extensions were installed for.
GROUP_FILE = ".group.json"


class StoredPriority(BaseModel):
    #: The super admin's order of extensions, highest first. It settles
    #: every collision, and is the order edits apply and extensions' hooks run in.
    priority: list[str] = Field(default_factory=list)


class StoredGroup(BaseModel):
    #: The one group the signed-in person belongs to in their
    #: organisation, read by igni server when it listed what they get
    #: (its §8.3); ``None`` when they are in none.
    group: str | None = None
    #: Its name, to show a person; ``None`` from an igni server that did
    #: not send one.
    name: str | None = None

    @property
    def label(self) -> str | None:
        """What to call the group to a person: its name, else its id."""
        return self.name or self.group


class ListingState:
    """The listing igni server last sent, as this machine knows it.

    On the desktop it is written to files beside the bundles, so the
    next start applies it before igni server has been asked anything.
    In the cloud a producer's VM is a cache and igni server's listing
    is read-only there, so it is kept in this instance's memory
    instead, and a VM starts by asking again.

    What it holds in memory is this instance's alone: two stores over
    one root do not see each other's listing.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._priority = StoredPriority()
        self._group: StoredGroup | None = None

    @property
    def priority_path(self) -> Path:
        """Where the priority order is written."""
        return self._root / PRIORITY_FILE

    @property
    def group_path(self) -> Path:
        """Where the person's group is written."""
        return self._root / GROUP_FILE

    def record_priority(self, priority: list[str]) -> bool:
        """Keep the priority order the listing carried. ``True`` if it changed."""
        stored = StoredPriority(priority=priority)
        if self._stored_priority() == stored:
            return False
        if not self._in_files():
            self._priority = stored
            return True
        self._root.mkdir(parents=True, exist_ok=True)
        self.priority_path.write_text(stored.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return True

    def priority(self) -> list[str]:
        """The priority order as last received, highest first. Empty, if nothing was."""
        return self._stored_priority().priority

    def record_group(self, group: str | None, *, name: str | None = None) -> None:
        """Keep the person's group the listing carried, for the next start before igni answers."""
        stored = StoredGroup(group=group, name=name if group is not None else None)
        if not self._in_files():
            self._group = stored
            return
        self._root.mkdir(parents=True, exist_ok=True)
        self.group_path.write_text(stored.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def group(self) -> StoredGroup | None:
        """The person's group as last received; ``None`` if no listing ever arrived."""
        if not self._in_files():
            return self._group
        path = self.group_path
        if not path.is_file():
            return None
        try:
            return StoredGroup.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return None

    def _in_files(self) -> bool:
        """Whether the layer keeps what the listing carried in files."""
        return layers.current().keeps_listing_in_files()

    def _stored_priority(self) -> StoredPriority:
        if not self._in_files():
            return self._priority
        path = self.priority_path
        if not path.is_file():
            return StoredPriority()
        try:
            return StoredPriority.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            logger.warning("ignoring %s: %s", path, exc)
            return StoredPriority()


__all__ = ["GROUP_FILE", "PRIORITY_FILE", "ListingState", "StoredGroup", "StoredPriority"]
