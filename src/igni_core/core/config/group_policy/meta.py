"""``pack_meta.json`` — the pack's identity for the frontend, and its freshness."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, ValidationError, field_serializer, field_validator

from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack, PackTheme

#: Group policies don't change often enough to need sub-minute freshness;
#: a short TTL keeps stale admin pushes from lasting forever.
CACHE_TTL_SECONDS = 300


class PackMeta(BaseModel):
    """What was last fetched, and when it was last confirmed current.

    Every field is optional: the meta igni server's signed "no pack" leaves
    names no group, and files written by older releases must still parse.
    Identity only — never input to the settings merge.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    group_id: str | None = None
    group_name: str | None = None
    fetched_at: datetime | None = None
    entry_count: int = 0
    default_model: str | None = None
    etag: str | None = None
    theme: PackTheme | None = None

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _unreadable_time_is_unknown(cls, value: object) -> object:
        # An unknown time reads as stale; it must not cost the tag beside it.
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return value

    @field_serializer("fetched_at")
    def _isoformat(self, value: datetime | None) -> str | None:
        # ``isoformat`` rather than Pydantic's ``Z``: the bytes on disk stay
        # what every earlier release wrote and read.
        return value.isoformat() if value else None

    @classmethod
    def from_pack(cls, pack: GroupPolicyPack) -> PackMeta:
        return cls(
            group_id=pack.group_id,
            group_name=pack.group_name,
            fetched_at=pack.fetched_at,
            entry_count=len(pack.entries),
            default_model=pack.default_model,
            etag=pack.etag,
            theme=pack.theme,
        )

    @classmethod
    def absence(cls, etag: str | None, now: datetime) -> PackMeta:
        """The only meta kept after a signed "no pack": its tag turns the next poll into a 304."""
        return cls(etag=etag, fetched_at=now, entry_count=0)

    def touched(self, now: datetime) -> PackMeta:
        """The same pack, confirmed current at ``now``."""
        return self.model_copy(update={"fetched_at": now})

    def is_stale(self, now: datetime, ttl_seconds: int = CACHE_TTL_SECONDS) -> bool:
        if self.fetched_at is None:
            return True
        fetched_at = self.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (now - fetched_at).total_seconds() > ttl_seconds


class PackMetaStore:
    """Reads and writes ``pack_meta.json`` under one layout."""

    def __init__(self, layout: GroupPolicyLayout) -> None:
        self._path = layout.file(GroupPolicyLayout.META_FILE)

    def load(self) -> PackMeta | None:
        """The stored meta, or ``None`` when there is none or it does not parse."""
        try:
            return PackMeta.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def save(self, meta: PackMeta) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(meta.model_dump(mode="json")), encoding="utf-8")

    def touch(self) -> None:
        """Mark the stored pack as checked just now."""
        meta = self.load()
        if meta is not None:
            self.save(meta.touched(datetime.now(timezone.utc)))

    def is_stale(self) -> bool:
        """Whether the stored pack is missing or older than the TTL."""
        meta = self.load()
        return meta is None or meta.is_stale(datetime.now(timezone.utc))

    def stored_etag(self) -> str | None:
        """The tag the server gave us for the pack on disk."""
        meta = self.load()
        return meta.etag if meta is not None and meta.etag else None
