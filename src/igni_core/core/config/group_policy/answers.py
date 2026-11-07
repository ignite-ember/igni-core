"""What a request for the pack can answer, and the shape of the thing that asks."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.group_policy.pack import GroupPolicyPack


class UnchangedPack(BaseModel):
    """The server's answer when the pack we hold is still current.

    A distinct type rather than ``None`` because the two mean opposite
    things to the caller: ``None`` is "I could not tell you", this is "I
    checked, and you are up to date". Conflating them would make a
    failed request look like a confirmation.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["unchanged"] = "unchanged"


PACK_UNCHANGED = UnchangedPack()


class SignedAbsence(BaseModel):
    """igni server's signed answer "this person has no pack".

    Distinct from ``None`` ("could not tell you; keep what you have") and
    from :class:`UnchangedPack`: once its signature verifies, it means the
    cached policy and everything derived from it must go.
    """

    model_config = ConfigDict(frozen=True)

    signed_pack: str
    etag: str | None = None


#: Everything a pack request can come back with.
PackAnswer = GroupPolicyPack | SignedAbsence | UnchangedPack | None


class PackFetcher(Protocol):
    """Asks igni server for the pack, sending the tag of the one held.

    Passed in (rather than the refresher constructing a ``PortalClient``)
    so the auth package can stay above the cache in the import graph.
    """

    async def __call__(self, token: str, etag: str | None) -> PackAnswer: ...
