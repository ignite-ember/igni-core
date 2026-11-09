"""The store's refusals: the error it raises, and the names it will not make.

The store builds directory and file names out of an extension's id,
which arrives from outside — a command line, an RPC, a listing from
igni server. A name that is not a name would put a bundle somewhere
the store did not mean to put it, so the bundle's own directory and
the record beside it go through one rule.
"""

from __future__ import annotations


class StoreError(Exception):
    """The bundle will not be stored, and why."""


class StoreName:
    """An extension id as the name of a file in the store.

    The id is reverse-DNS and validated by the manifest, so this
    cannot happen through the normal path — but it is a directory
    name built from a string, and the cost of being sure is one line.
    """

    def __init__(self, extension_id: str) -> None:
        if "/" in extension_id or "\\" in extension_id or extension_id in {".", ".."}:
            raise StoreError(f"{extension_id!r} is not a usable directory name")
        self._id = extension_id

    @property
    def id(self) -> str:
        """The extension's id, as the store names it."""
        return self._id

    def with_suffix(self, suffix: str) -> str:
        """A file beside the bundle: the id and a suffix."""
        return f"{self._id}{suffix}"


__all__ = ["StoreError", "StoreName"]
