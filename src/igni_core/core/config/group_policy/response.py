"""The body of ``GET /v1/portal/me/group/pack``, and what it answers."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from igni_core.core.config.group_policy.answers import SignedAbsence
from igni_core.core.config.group_policy.entry import GroupPolicyEntry
from igni_core.core.config.group_policy.pack import GroupPolicyPack


class PackResponse(BaseModel):
    """igni server's pack response: the plain fields and the same pack signed.

    Either a pack (``group_id`` … ``entries`` plus ``signed_pack``) or, with
    ``no_pack`` set, a signed "this person has no pack". Only ``signed_pack``
    is ever applied once ``PackVerifier`` has checked it; the plain fields
    are what the cache materialises when no verifier is configured.
    """

    model_config = ConfigDict(extra="ignore")

    group_id: str = ""
    group_name: str = ""
    fetched_at: datetime | None = None
    default_model: str | None = None
    entries: list[GroupPolicyEntry] = []
    signed_pack: str | None = None
    no_pack: bool = False

    def answer(self, etag: str | None) -> GroupPolicyPack | SignedAbsence:
        """The pack this response carries, or its signed absence.

        Raises :class:`ValueError` (pydantic's errors are one) when it is a
        pack without the fields a pack needs.
        """
        if self.no_pack and self.signed_pack:
            # Verified by ``PackVerifier`` before it clears anything.
            return SignedAbsence(signed_pack=self.signed_pack, etag=etag)
        if self.fetched_at is None:
            raise ValueError("the pack has no fetched_at")
        return GroupPolicyPack(
            group_id=self.group_id,
            group_name=self.group_name,
            fetched_at=self.fetched_at,
            default_model=self.default_model,
            entries=self.entries,
            etag=etag,
            signed_pack=self.signed_pack,
        )
