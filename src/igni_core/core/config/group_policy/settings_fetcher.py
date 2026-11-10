"""The ``group_policy_fetcher`` production hands to ``SettingsLoader.load``."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from igni_core.core.config.group_policy.cache import GroupPolicyCache, RebuildResult
from igni_core.core.config.group_policy.pack import GroupPolicyPack
from igni_core.core.config.group_policy.signed_store import StoredPackRead
from igni_core.core.config.group_policy.transport import SignedPackTransport
from igni_core.core.config.group_policy.trust import PackTrust
from igni_core.core.config.settings import Settings

logger = logging.getLogger(__name__)


class GroupPolicyUnavailable(RuntimeError):
    """``group_policy.required`` is set and no verified pack could be had.

    The message is written for the person in front of igni: what is wrong
    and what to do about it.
    """


class FetcherState(BaseModel):
    """Whether the pack has been read yet, and what came of it."""

    phase: Literal["unread", "read", "unavailable"] = "unread"
    pack: GroupPolicyPack | None = None
    error: str = ""


class CachedSettingsFetcher:
    """Zero-argument and callable — the whole contract ``GroupPolicyTier`` asks for.

    Build it with :meth:`for_settings` from the local-only ``Settings`` of the
    first load pass: that is where the data directory, ``api_url``, the
    credentials and the managed-only ``group_policy.trusted_keys`` /
    ``group_policy.required`` come from.

    ## Why this reads disk instead of the portal

    A settings load runs before the process holds a bearer token, so a
    fetcher that called the portal would make every start-up wait on a round
    trip that can hang. The fetch is ``GroupPolicySync.hydrate``
    (:class:`~.refresher.PackRefresher`) — cold start, login, and every
    ``group_policy.poll_seconds`` — which verifies the pack and stores it as
    igni server signed it. The read is here, and it verifies again: the file
    is the user's to write, so it is trusted only as far as its signature.

    * Offline, portal down, token expired: the last pack that verified still
      applies. A pack has no expiry.
    * A pack that is missing, unsigned, edited, for another person, server or
      organisation, or older than one already applied is **never** merged, and
      what was derived from an earlier pack is removed.
    * A verified pack's files are rebuilt from it here (:meth:`GroupPolicyCache.rebuild`).
    * igni server's signed "no pack for this person" clears the cache.

    ## When there is no usable pack

    ``group_policy.required`` off: a warning (or, for no pack at all, a debug
    line) and the load continues on local configuration. On: one live fetch
    if somebody is signed in (:class:`SignedPackTransport`), and otherwise
    :class:`GroupPolicyUnavailable` — the callers start no agent session.

    Memoized: the read happens once per fetcher.
    """

    def __init__(
        self,
        cache: GroupPolicyCache,
        trust: PackTrust | None = None,
        transport: SignedPackTransport | None = None,
    ) -> None:
        self._cache = cache
        #: Without a trust context nothing can be verified, so nothing applies.
        self._trust = trust or PackTrust()
        self._transport = transport or SignedPackTransport(self._trust)
        self._state = FetcherState()

    @classmethod
    def for_settings(cls, settings: Settings) -> CachedSettingsFetcher:
        """The fetcher for an install configured by ``settings``."""
        trust = PackTrust.from_settings(settings)
        cache = GroupPolicyCache.for_data_dir(settings.storage.data_dir)
        return cls(cache, trust, SignedPackTransport(trust))

    @property
    def required(self) -> bool:
        return self._trust.required

    def __call__(self) -> GroupPolicyPack | None:
        if self._state.phase == "unread":
            try:
                self._state = FetcherState(phase="read", pack=self._read_once())
            except GroupPolicyUnavailable as exc:
                self._state = FetcherState(phase="unavailable", error=str(exc))
        if self._state.phase == "unavailable":
            raise GroupPolicyUnavailable(self._state.error)
        return self._state.pack

    def _read_stored(self) -> StoredPackRead:
        try:
            return self._cache.verified_pack(self._trust)
        except Exception as exc:  # noqa: BLE001 — settings must load whatever is on disk
            return StoredPackRead(status="rejected", reason=str(exc))

    def _read_once(self) -> GroupPolicyPack | None:
        """Verify the stored pack, rebuild what derives from it, and say, once, what was found."""
        root = self._cache.layout.root
        read = self._read_stored()
        if read.status in {"missing", "rejected"}:
            # Nothing that verifies: anything left in the kind directories is
            # nobody's policy, and must not load as the organisation's.
            self._clear_derived()
            if self._trust.required:
                missing = "there is no group policy on this machine yet"
                return self._required_fallback(
                    read.reason if read.status == "rejected" else missing
                )
            if read.status == "missing":
                logger.debug(
                    "no cached org group policy in %s; settings loaded from local "
                    "configuration only",
                    root,
                )
            else:
                logger.warning(
                    "group policy in %s ignored: %s. Settings loaded from local "
                    "configuration only.",
                    root,
                    read.reason,
                )
            return None
        if read.pack is None:
            # Verified "no pack for this person".
            self._clear_derived()
            logger.info("igni server says this person has no group policy; none applied")
            return None

        pack = read.pack
        rebuilt = self._cache.rebuild(pack)
        if not rebuilt.ok:
            if self._trust.required:
                raise self._unavailable(
                    f"its agents, hooks and tools could not be rebuilt: {rebuilt.reason}"
                )
            logger.warning(
                "group policy in %s: its agents, hooks and tools could not be rebuilt (%s); "
                "its settings still apply",
                root,
                rebuilt.reason,
            )
        logger.info(
            "org group policy applied to settings: group %s (%s), settings %s, verified, from %s",
            pack.group_name,
            pack.group_id,
            ", ".join(sorted(pack.to_settings_dict())) or "(none)",
            root,
        )
        return pack

    def _clear_derived(self) -> None:
        try:
            self._cache.clear_derived()
        except OSError as exc:
            logger.warning("group-policy: could not clear %s (%s)", self._cache.layout.root, exc)

    def _required_fallback(self, why: str) -> GroupPolicyPack | None:
        """``required`` and no usable pack: fetch one now, or refuse."""
        live = self._transport.fetch_into(self._cache)
        if live.stored:
            read = self._read_stored()
            if read.status == "absent":
                # igni server, asked just now, says this person has none.
                return None
            rebuilt = self._rebuilt(read)
            if read.pack is not None and rebuilt.ok:
                logger.info(
                    "org group policy fetched from igni server at start-up: group %s (%s)",
                    read.pack.group_name,
                    read.pack.group_id,
                )
                return read.pack
            why = rebuilt.reason
        raise self._unavailable(why)

    def _rebuilt(self, read: StoredPackRead) -> RebuildResult:
        if read.pack is None:
            return RebuildResult(ok=False, reason=read.reason)
        return self._cache.rebuild(read.pack)

    def _unavailable(self, why: str) -> GroupPolicyUnavailable:
        server = self._trust.api_url or "igni server"
        return GroupPolicyUnavailable(
            "Your organisation requires its group policy, and igni has no valid copy "
            f"({why}). Connect to {server} and sign in so igni can fetch it, then "
            "restart igni. No agent session will start until then."
        )
