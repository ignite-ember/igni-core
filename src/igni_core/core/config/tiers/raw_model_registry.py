"""``RawModelRegistry`` — ``models.registry`` of a settings payload before validation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class RawModelRegistry(BaseModel):
    """The ``models.registry`` block of a raw settings payload.

    The tiers see settings before ``Settings`` validates them, so an entry is
    whatever the file said. An entry that is a mapping is a *row*; anything
    else is carried through untouched, for ``Settings`` to refuse.
    """

    model_config = ConfigDict(frozen=True)

    entries: dict[str, Any]

    @classmethod
    def of(cls, payload: Mapping[str, Any]) -> RawModelRegistry | None:
        """``payload``'s registry, or ``None`` when it has no mapping there."""
        models = payload.get("models")
        registry = models.get("registry") if isinstance(models, dict) else None
        return cls(entries=registry) if isinstance(registry, dict) else None

    def rows(self) -> dict[str, dict[str, Any]]:
        """The entries that are mappings, by model name."""
        return {name: entry for name, entry in self.entries.items() if isinstance(entry, dict)}

    def row(self, name: str) -> dict[str, Any] | None:
        """Entry ``name`` when it is a mapping."""
        entry = self.entries.get(name)
        return entry if isinstance(entry, dict) else None

    def rows_setting(self, fields: Iterable[str]) -> dict[str, list[str]]:
        """Each row that sets any of ``fields``, with the ones it sets, sorted."""
        wanted = tuple(fields)
        return {
            name: sorted(f for f in wanted if f in row)
            for name, row in self.rows().items()
            if any(f in row for f in wanted)
        }

    def without(self, fields: Mapping[str, Iterable[str]]) -> RawModelRegistry:
        """This registry with ``fields[name]`` taken out of row ``name``."""
        entries = dict(self.entries)
        for name, dropped in fields.items():
            row = self.row(name)
            if row is not None:
                gone = set(dropped)
                entries[name] = {k: v for k, v in row.items() if k not in gone}
        return RawModelRegistry(entries=entries)

    def into(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """``payload`` with this registry in place of its own.

        ``payload`` must already hold a ``models`` mapping — the one this
        registry was read from with :meth:`of`.
        """
        return {**payload, "models": {**payload["models"], "registry": dict(self.entries)}}
