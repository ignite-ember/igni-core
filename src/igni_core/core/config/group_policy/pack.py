"""The group pack, and the signed ``policy`` claim it is built from."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator

from igni_core.core.config.config_io import DictMerger
from igni_core.core.config.group_policy.entry import GroupPolicyEntry

logger = logging.getLogger(__name__)


class PackTheme(BaseModel):
    """Brand overrides for a group — accent colours, a wordmark, a logo.

    A pass-through, not a schema: nothing here interprets it. The backend
    hands it to the webview, which validates every value against its own
    allowlist before letting any of it near a stylesheet, so a typed field
    per token would be a second place to update each time the server learns
    one — and not the place doing the checking.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


class SignedPolicy(BaseModel):
    """The verified ``policy`` claim of a pack, as igni server signed it.

    Lenient on purpose: absent or falsy names read as ``""``, numbers as
    strings, a missing time as now and ``entries: null`` as none. A stricter
    reading would refuse packs that apply today.
    """

    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    group_id: str = ""
    group_name: str = ""
    fetched_at: datetime | None = None
    entries: list[GroupPolicyEntry] = []
    default_model: str | None = None
    theme: PackTheme | None = None

    @field_validator("group_id", "group_name", mode="before")
    @classmethod
    def _absent_is_empty(cls, value: object) -> object:
        return value or ""

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _absent_is_unknown(cls, value: object) -> object:
        return value or None

    @field_validator("entries", mode="before")
    @classmethod
    def _absent_is_no_entries(cls, value: object) -> object:
        return value or []


class GroupPolicyPack(BaseModel):
    """Everything a group gives its people — fetched from igni server."""

    group_id: str
    group_name: str
    fetched_at: datetime
    entries: list[GroupPolicyEntry] = []

    # What this group's people get when nothing names a model. Routing
    # is resolved server-side, so this is carried for display and for
    # the frontmatter fallback rather than acted on here.
    default_model: str | None = None

    #: The server's tag for this pack's content. Sent back on the next
    #: poll so an unchanged pack costs a 304 and no payload.
    etag: str | None = None

    #: Group-level UI config for the frontend, not an entry: entries are
    #: materialised as files for agents to read. See :class:`PackTheme`.
    theme: PackTheme | None = None

    #: The same pack as igni server signed it — a compact JWS, kept exactly
    #: as received. Only a pack that arrived with one, and whose signature
    #: verified, is ever applied (``group_policy_signature.py``).
    signed_pack: str | None = None

    @classmethod
    def from_signed(
        cls,
        policy: SignedPolicy,
        *,
        etag: str | None = None,
        signed_pack: str | None = None,
    ) -> GroupPolicyPack:
        """A pack built from the *signed* content, not the plain fields.

        The response carries both; once the signature has verified, this is
        what is materialised and merged, so nothing that was not signed can
        ride along beside it.
        """
        return cls(
            group_id=policy.group_id,
            group_name=policy.group_name,
            fetched_at=policy.fetched_at or datetime.now(timezone.utc),
            entries=policy.entries,
            default_model=policy.default_model,
            theme=policy.theme,
            etag=etag,
            signed_pack=signed_pack,
        )

    def to_settings_dict(self) -> dict:
        """The enabled ``settings`` entries, deep-merged, for the accumulator.

        A dict on purpose: it feeds the settings merge, which takes
        arbitrary settings JSON. Every other kind is materialised to disk by
        ``GroupPolicyCache`` and picked up by its own loader.
        """
        result: dict = {}
        for entry in self.entries:
            if entry.kind != "settings" or not entry.enabled:
                continue
            try:
                parsed = json.loads(entry.content) if entry.content_type == "json" else {}
            except ValueError as exc:
                # An organisation's setting that did not apply must not go
                # unnoticed.
                logger.warning("group policy: a settings entry was not applied: %s", exc)
                continue
            if isinstance(parsed, dict):
                result = DictMerger.deep(result, parsed)
        return result
