"""The signed pack on disk: ``pack.jws``, the keys it verified against, its version."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from igni_core.core.config.group_policy.jwks import Jwks
from igni_core.core.config.group_policy.jws import CompactPack
from igni_core.core.config.group_policy.layout import GroupPolicyLayout
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.group_policy.verifier import AcceptedPack


class PackState(BaseModel):
    """``pack_state.json`` — the newest pack version this install has accepted."""

    model_config = ConfigDict(frozen=True)

    version: StrictInt


class PluginSourceClaim(BaseModel):
    """A plugin entry as the stored pack names it, read *without* verifying."""

    model_config = ConfigDict(frozen=True, extra="ignore", coerce_numbers_to_str=True)

    kind: str = ""
    entry_name: str = ""
    source_url: str | None = None


class StoredPackRead(BaseModel):
    """What the stored pack says, once verified.

    * ``missing``  — no signed pack at all: somebody in no organisation.
    * ``absent``   — igni server's signed "no pack for this person".
    * ``applied``  — a pack that verified; ``pack`` holds it.
    * ``rejected`` — a pack this install may not apply; ``reason`` says why.
    """

    model_config = ConfigDict(frozen=True)

    status: Literal["missing", "absent", "applied", "rejected"]
    pack: GroupPolicyPack | None = None
    reason: str = ""


class SignedPackStore:
    """Owns the signed pack, its JWKS and its version under one layout."""

    def __init__(self, layout: GroupPolicyLayout) -> None:
        self._layout = layout

    def store(self, accepted: AcceptedPack) -> None:
        """Keep a verified pack as received, the keys it verified against,
        and its version as the newest one accepted.

        The version is the live pack's, not the larger of it and the last:
        a pack fetched over TLS from igni server is the authority on what is
        current, so a server whose clock went backwards does not lock its
        clients out. The rollback check is for the copy on disk.
        """
        self._layout.root.mkdir(parents=True, exist_ok=True)
        self._layout.file(GroupPolicyLayout.SIGNED_PACK_FILE).write_text(
            accepted.compact, encoding="utf-8"
        )
        self._layout.file(GroupPolicyLayout.JWKS_FILE).write_text(
            json.dumps(accepted.jwks.as_mapping()), encoding="utf-8"
        )
        self._layout.file(GroupPolicyLayout.STATE_FILE).write_text(
            json.dumps(PackState(version=accepted.version).model_dump()), encoding="utf-8"
        )
        self.drop_legacy_settings()

    def signed_text(self) -> str | None:
        """The stored JWS, or ``None`` when there is none to read."""
        try:
            return (
                self._layout.file(GroupPolicyLayout.SIGNED_PACK_FILE)
                .read_text(encoding="utf-8")
                .strip()
            )
        except (OSError, ValueError):  # a file that is not UTF-8 is not a pack
            return None

    def jwks(self) -> Jwks | None:
        try:
            raw = json.loads(
                self._layout.file(GroupPolicyLayout.JWKS_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None
        return Jwks.parse(raw)

    def applied_version(self) -> int | None:
        try:
            text = self._layout.file(GroupPolicyLayout.STATE_FILE).read_text(encoding="utf-8")
            return PackState.model_validate_json(text).version
        except (OSError, ValueError):  # a ValidationError is a ValueError
            return None

    def read(self, trust: PackTrust, *, etag: str | None = None) -> StoredPackRead:
        """The stored pack, verified against ``trust`` and the keys stored beside it."""
        compact = self.signed_text()
        if not compact:
            return StoredPackRead(
                status="missing", reason=f"no signed group pack in {self._layout.root}"
            )
        verdict = trust.verify(compact, self.jwks(), min_version=self.applied_version())
        if verdict.verified is None:
            return StoredPackRead(status="rejected", reason=verdict.reason)
        if verdict.verified.absent or verdict.verified.policy is None:
            return StoredPackRead(status="absent")
        pack = GroupPolicyPack.from_signed(verdict.verified.policy, etag=etag, signed_pack=compact)
        return StoredPackRead(status="applied", pack=pack)

    def unverified_plugin_sources(self) -> list[PluginSourceClaim]:
        """The plugin entries the stored pack names, read without verifying.

        The only thing this may decide is whether the next fetch asks for the
        whole pack instead of a 304, so a forged file can cost a download and
        nothing else.
        """
        compact = self.signed_text()
        pack_claims = CompactPack.unverified_claims(compact) if compact else None
        policy = pack_claims.policy if pack_claims is not None else None
        entries = policy.get("entries") if policy is not None else None
        claims: list[PluginSourceClaim] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                claims.append(PluginSourceClaim.model_validate(entry))
            except ValidationError:
                continue
        return claims

    def drop_legacy_settings(self) -> None:
        self._layout.file(GroupPolicyLayout.LEGACY_SETTINGS_FILE).unlink(missing_ok=True)
