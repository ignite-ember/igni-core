"""What a sign-out does to a session's model: one that needs igni server gives way.

Shared by ``/logout`` and the ``clear_cloud_credentials`` RPC. Not in the
``/logout`` command's module, which an idle backend must not load
(``tests/test_commands_load_on_first_use.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from igni_core.backend.schemas_auth import CloudSwitchOutcome

if TYPE_CHECKING:
    from igni_core.core.session import Session


def switch_off_cloud(session: Session) -> CloudSwitchOutcome:
    """After a sign-out: if the session's model routes through igni
    server, make a non-cloud entry the default and rebuild the main team.
    The returned :class:`CloudSwitchOutcome` names the three possible
    states (`no_switch_needed`, `switched_to`, or
    `cloud_but_no_fallback`) so the invariant lives on the type.
    """
    models = session.settings.models
    if not models.current_uses_cloud_token():
        return CloudSwitchOutcome.no_switch_needed()

    fallback = models.find_non_cloud_fallback()
    if fallback is None:
        return CloudSwitchOutcome.cloud_but_no_fallback(
            "Warning: no models with API keys configured. "
            "Add a model with an api_key or /login again."
        )

    models.default = fallback
    session.rebuild_main_team()
    return CloudSwitchOutcome.switched_to(fallback)


__all__ = ["switch_off_cloud"]
