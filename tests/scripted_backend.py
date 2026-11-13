"""A backend whose chosen RPCs answer from a script, for the host members' tests.

Not a test module: no ``test_`` prefix, so pytest collects nothing.

The host members map a member's parameters onto an RPC's arguments and
read the RPC's answer back. :func:`script` stands between the real
gateway and the rig's backend: the RPCs it was given answer what the
test says (or raise what it says) and are recorded with the arguments
they were called with; every other RPC goes on to the real backend.
"""

from __future__ import annotations

from typing import Any

from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig


class ScriptedBackend:
    """The rig's backend, with some RPCs answered from a script."""

    def __init__(self, inner: Any, answers: dict[RpcMethod, Any]) -> None:
        self._inner = inner
        self.answers = answers
        #: ``(method, args, session_id)`` of every scripted RPC, in order.
        self.calls: list[tuple[RpcMethod, dict[str, Any], str]] = []

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        if method not in self.answers:
            return await self._inner.rpc(method, args, session_id=session_id)
        self.calls.append((method, args, session_id))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def asked(self, method: RpcMethod) -> list[dict[str, Any]]:
        """The arguments each call of ``method`` carried."""
        return [args for called, args, _ in self.calls if called is method]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def script(rig: HostRig, answers: dict[RpcMethod, Any]) -> ScriptedBackend:
    """Answer ``answers`` from a script, on the rig's real gateway."""
    assert rig.model is not None
    gateway = rig.model.services.gateway
    scripted = ScriptedBackend(gateway._backend, answers)  # noqa: SLF001 — the seam under test
    gateway._backend = scripted  # noqa: SLF001
    return scripted
