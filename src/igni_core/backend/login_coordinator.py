"""Login task coordinator.

Owns the "one login attempt at a time" invariant that used to live
as an ad-hoc ``login_state: dict[str, Any] = {"task": None}`` bag
threaded through the RPC dispatch closure and the ``CancelLogin``
message handler.

The class exposes a single-writer interface:

* :meth:`start` — cancel any in-flight login and kick off a new one.
* :meth:`cancel` — cancel the in-flight login (no-op if none).

Both are idempotent so races between an RPC and the ``CancelLogin``
message resolve deterministically.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from typing import Any

from igni_core.backend.push_bridge import PushNotificationBridge
from igni_core.backend.schemas_rpc import LoginResult, LoginStarted
from igni_core.core.activity import background

logger = logging.getLogger(__name__)


class LoginCoordinator:
    """Single-writer manager for the login task.

    Owns the ``asyncio.Task`` handle for the in-flight
    ``backend.login`` call (if any) so the RPC entry point and the
    ``CancelLogin`` message handler can't step on each other.
    """

    def __init__(self, *, backend: Any, push_bridge: PushNotificationBridge) -> None:
        self._backend = backend
        self._push = push_bridge
        self._task: asyncio.Task[None] | None = None
        self._signed_in: list[Callable[[], object]] = []

    def on_signed_in(self, callback: Callable[[], object]) -> None:
        """Call ``callback`` after every successful sign-in.

        For the work that depends on who is signed in and cannot have
        happened before — fetching the extensions that person's igni
        server distributes is the first. A callback that raises is
        logged and does not undo the sign-in.
        """
        self._signed_in.append(callback)

    async def start(self) -> LoginStarted:
        """Cancel any previous login attempt, kick off a new one,
        and ack immediately. The actual login result arrives
        asynchronously via a ``login_result`` push notification."""
        await self.cancel()
        flow_id = f"login-{uuid.uuid4().hex[:12]}"
        # Waits on a person signing in: not busy.
        self._task = background.daemon(self._run_login(flow_id), name="login")
        return LoginStarted(started=True, flow_id=flow_id)

    async def cancel(self) -> None:
        """Cancel the in-flight login task (if any). Awaits the
        task's completion so downstream state is settled before we
        return."""
        old = self._task
        if old is None or old.done():
            return
        old.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await old

    # ── Internals ────────────────────────────────────────────────

    async def _run_login(self, flow_id: str = "") -> None:
        async def on_status(text: str) -> None:
            await self._push.on_login_status(text, flow_id=flow_id)

        try:
            res: LoginResult = await self._backend.controllers.auth.login(on_status=on_status)
            if res.ok:
                self._backend.controllers.auth.reload_cloud_credentials()
                self._notify_signed_in()
            await self._push.on_login_result(
                success=res.ok, result=res.wire_result_string(), flow_id=flow_id
            )
        except asyncio.CancelledError:
            # Cancelled via ``CancelLogin`` — no result to push.
            pass
        except Exception as exc:  # pragma: no cover — defensive
            await self._push.on_login_result(success=False, result=str(exc), flow_id=flow_id)

    def _notify_signed_in(self) -> None:
        for callback in list(self._signed_in):
            try:
                callback()
            except Exception:  # noqa: BLE001 — a listener must not fail the sign-in
                logger.exception("a sign-in listener failed")
