"""``GroupPolicyTier`` — the organisation group's settings, under the CLI deny ratchet."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.tiers.base import Tier
from igni_core.core.config.tiers.cli import CliRatchet

if TYPE_CHECKING:
    from igni_core.core.config.group_policy.pack import GroupPolicyPack

logger = logging.getLogger(__name__)


class GroupPolicyTier(Tier):
    """Org-group policy overrides — fetched from igni server, cached locally.

    Sits ABOVE CliTier (group-defined settings beat user CLI flags) and
    BELOW ManagedTier (sysadmin file still wins over any org policy).

    ``fetcher`` is a zero-argument callable that returns a
    :class:`~igni_core.core.config.group_policy.pack.GroupPolicyPack` or
    ``None`` when the user has no group. It is invoked once per
    :meth:`Tier.apply` call; callers should memoize or cache at the
    fetcher level to avoid repeated network calls.

    ## The deny ratchet

    Sitting above the CLI is deliberate and one-directional in intent:
    the point is that a user cannot escape org policy by adding
    ``--auto-approve``. The implementation was bidirectional, which is
    not the same thing — a group could turn a developer's ``--strict``
    into ``shell_execute: allow`` and nothing said so. A safety flag that
    silently does nothing is worse than no flag.

    So a permission the CLI set to ``deny`` stays denied (``ratchet``, a
    :class:`~igni_core.core.config.tiers.cli.CliRatchet` built from the same
    overrides the CLI tier merges). A group can still *tighten* anything,
    and ``--auto-approve`` still cannot get past a group's ``deny`` — both
    directions of the original intent survive, and only the loosening of an
    explicit local deny does not.

    Everything else a group sets still wins over the CLI. This is a
    ratchet on ``permissions`` denies specifically, not a general
    reversal of precedence.

    ## Where the fetcher comes from

    :meth:`SettingsMergePlan.default` only appends the tier when it is handed
    a ``group_policy_fetcher``, and for a long time no production caller
    passed one — the tier was written, correct, and unreached, so an admin
    publishing ``{"permissions": {"web_fetch": "deny"}}`` believed it was enforced
    and it was not.

    Production now passes
    :class:`~igni_core.core.config.group_policy.settings_fetcher.CachedSettingsFetcher`, from
    the two places that build a real ``Settings``: ``BackendSettingsLoader.load`` (the
    backend process every window talks to) and ``CliSettingsLoader.load``
    (the ``igni -m`` / pipe path, which runs a session in-process). Both build
    exactly one per process.

    That fetcher reads the pack the last refresh left on disk rather than
    calling the portal, because a settings load runs before this process holds
    a bearer token. The fetch lives in ``GroupPolicySync.hydrate``
    — cold start, login, and every ``group_policy.poll_seconds`` — and the read
    lives here; see :class:`CachedSettingsFetcher` for what that split buys and
    what it costs.

    ## Only a pack igni server signed

    The cache is a directory the user can write, so what the fetcher reads is
    trusted only as far as igni server's signature on it: the pack is stored
    exactly as signed (``pack.jws``) and verified — signature, ``iss`` against
    ``api_url``, ``sub`` against the person signed in, no rollback — before it
    reaches this tier. An unusable pack is never merged; with the managed-only
    ``group_policy.required`` the fetcher raises instead. See
    ``core/config/group_policy/signature.py`` and
    :class:`~igni_core.core.config.tiers.managed.ManagedOnlyTier`.

    Tests and one-off migrations still pass their own fetcher, and a plan built
    without one skips the tier entirely.
    """

    def __init__(
        self,
        fetcher: Callable[[], GroupPolicyPack | None],
        ratchet: CliRatchet | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._ratchet = ratchet or CliRatchet()
        #: Fields the group tried to loosen and could not on the last
        #: :meth:`apply`, as ``{field: attempted_value}`` — also said in a
        #: warning, because a policy that quietly does not apply is its own
        #: kind of surprise.
        self.refused_loosening: dict[str, str] = {}

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        pack = self._fetcher()
        if pack is None:
            return accumulator

        payload = pack.to_settings_dict()
        block = payload.get("permissions")
        permissions = block if isinstance(block, dict) else {}
        self.refused_loosening = self._ratchet.refusals(permissions)
        if self.refused_loosening:
            # Drop the keys rather than writing 'deny' back: the accumulator
            # already holds the CLI's deny, and rewriting it would hide which
            # tier decided.
            payload = {
                **payload,
                "permissions": {
                    k: v for k, v in permissions.items() if k not in self.refused_loosening
                },
            }
            # Said out loud, because a policy that quietly does not apply is
            # its own surprise — the admin believes they set something, and
            # nothing anywhere disagrees.
            logger.warning(
                "Group policy tried to relax %s, which you denied on the command "
                "line; the local deny stands. An organisation policy can tighten a "
                "permission but not loosen one you denied yourself.",
                ", ".join(
                    f"{field} to {value!r}"
                    for field, value in sorted(self.refused_loosening.items())
                ),
            )

        return accumulator.merge(payload)
