"""Whether this process may run agent sessions."""

from __future__ import annotations


class GroupPolicyGate:
    """Whether this process may run agent sessions.

    Closed by the backend when ``group_policy.required`` is set and no
    verified pack could be applied at start-up. The backend still boots —
    the person has to be able to sign in, which is how a valid pack is
    fetched — but every place that starts an agent run asks here first and
    refuses with :meth:`reason`. Stays closed until the process restarts:
    the settings this process loaded were built without the policy, so a
    pack that arrives later cannot make *them* compliant.

    TODO(pass 2): a class-level singleton; becomes an injected ``RunGate``
    instance with a typed phase.
    """

    _reason: str | None = None

    @classmethod
    def close(cls, reason: str) -> None:
        cls._reason = reason

    @classmethod
    def reason(cls) -> str | None:
        return cls._reason

    @classmethod
    def reset(cls) -> None:
        cls._reason = None
