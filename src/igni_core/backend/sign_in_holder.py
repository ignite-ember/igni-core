"""What signing in and out changes, as the auth controller and the pack sync see it.

A full backend's :class:`~igni_core.core.session.Session` is one: its
credentials, its settings, the agent built on them and the group's agents
it loads. The account-only backend
(:mod:`igni_core.backend.account_backend`) is the other: the same
credentials and settings, and no agent to rebuild.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from igni_core.core.config.settings import Settings
    from igni_core.core.session.cloud_auth import SessionCloudAuth


class SignInHolder(Protocol):
    """Holds who is signed in, and rebuilds what depends on it."""

    @property
    def cloud_auth(self) -> SessionCloudAuth:
        """The credentials igni holds for igni server."""
        ...

    @property
    def settings(self) -> Settings:
        """The settings in force; a sign-out may change the model they name."""
        ...

    def rebuild_main_team(self) -> None:
        """Rebuild the agent on the settings as they are now."""
        ...

    def reload_group_agents(self) -> bool:
        """Load the agents a new group pack ships; ``True`` when any changed."""
        ...


__all__ = ["SignInHolder"]
