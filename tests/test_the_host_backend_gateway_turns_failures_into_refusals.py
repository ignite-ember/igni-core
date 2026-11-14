"""How the host model reaches igni's backend: failures as refusals, writes told.

:class:`HostBackendGateway` over a backend that answers or fails as a
test says: a handler's own errors become the refusal the extension's
author reads, anything unexpected goes through untouched, and only an RPC
that succeeded is told to those who listen for writes.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.backend_gateway import HostBackendGateway
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import SessionAttached
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


class Backend:
    """A backend that returns ``answer`` or raises ``failure``."""

    def __init__(self) -> None:
        self.answer: Any = {"ok": True}
        self.failure: BaseException | None = None
        self.asked: list[tuple[Any, str]] = []
        self.rpcs: list[tuple[RpcMethod, dict[str, Any], str]] = []
        self.attached: list[tuple[str, str | None]] = []

    def default_session(self) -> str:
        return "boot"

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        self.rpcs.append((method, args, session_id))
        if self.failure is not None:
            raise self.failure
        return self.answer

    async def ask(self, message: Any, *, session_id: str) -> Any:
        self.asked.append((message, session_id))
        return self.answer

    async def attach(self, session_id: str, project_dir: str | None) -> SessionAttached:
        self.attached.append((session_id, project_dir))
        if self.failure is not None:
            raise self.failure
        return self.answer


@pytest.fixture
def backend() -> Backend:
    return Backend()


@pytest.fixture
def gateway(backend: Backend) -> HostBackendGateway:
    return HostBackendGateway(backend)  # type: ignore[arg-type]


async def refusal(gateway: HostBackendGateway, backend: Backend, failure: BaseException):
    backend.failure = failure
    with pytest.raises(HostModelError) as refused:
        await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")
    return refused.value


async def test_a_successful_rpc_returns_the_handlers_answer(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.answer = {"servers": ["a"]}

    result = await gateway.rpc(RpcMethod.GET_STATUS, {"k": 1}, session_id="s1")

    assert result == {"servers": ["a"]}
    assert backend.rpcs == [(RpcMethod.GET_STATUS, {"k": 1}, "s1")]


async def test_every_successful_rpc_is_told_to_each_listener_as_a_write(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    first: list[RpcWrite] = []
    second: list[RpcWrite] = []
    gateway.on_write(first.append)
    gateway.on_write(second.append)

    await gateway.rpc(RpcMethod.GET_STATUS, {"name": "x"}, session_id="s9")

    expected = RpcWrite(method=RpcMethod.GET_STATUS, args={"name": "x"}, session_id="s9")
    assert first == [expected] and second == [expected]


async def test_a_listener_added_while_listeners_are_told_is_not_told_that_write(
    gateway: HostBackendGateway,
) -> None:
    late: list[RpcWrite] = []
    gateway.on_write(lambda _: gateway.on_write(late.append))

    await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")
    assert late == []

    await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")
    assert len(late) == 1


async def test_a_listener_that_raises_surfaces_after_the_rpc_succeeded(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    def broken(_: RpcWrite) -> None:
        raise RuntimeError("listener bug")

    gateway.on_write(broken)

    with pytest.raises(RuntimeError, match="listener bug"):
        await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")

    assert len(backend.rpcs) == 1, "the rpc itself had been made"


async def test_a_missing_argument_is_invalid_and_names_the_method_and_key(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    error = await refusal(gateway, backend, KeyError("server_name"))

    assert error.code is ErrorCode.INVALID
    assert error.message == "get_status: 'server_name' is required"


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("bad value"),
        PermissionError("not allowed"),
        FileNotFoundError("no such file"),
    ],
    ids=lambda f: type(f).__name__,
)
async def test_a_handlers_own_refusals_are_refused_with_their_message(
    gateway: HostBackendGateway, backend: Backend, failure: BaseException
) -> None:
    error = await refusal(gateway, backend, failure)

    assert error.code is ErrorCode.REFUSED
    assert error.message == str(failure)


async def test_a_host_model_error_passes_through_as_it_was_raised(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    original = HostModelError(ErrorCode.NOT_FOUND, "no such session")

    assert await refusal(gateway, backend, original) is original


@pytest.mark.parametrize("failure", [RuntimeError("bug"), TimeoutError("slow")])
async def test_an_unexpected_failure_propagates_unconverted(
    gateway: HostBackendGateway, backend: Backend, failure: BaseException
) -> None:
    backend.failure = failure

    with pytest.raises(type(failure)):
        await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")


async def test_a_failed_rpc_is_not_told_to_the_listeners(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    told: list[RpcWrite] = []
    gateway.on_write(told.append)

    for failure in (KeyError("k"), ValueError("v"), HostModelError(ErrorCode.FAILED, "f")):
        await refusal(gateway, backend, failure)
    backend.failure = RuntimeError("bug")
    with pytest.raises(RuntimeError):
        await gateway.rpc(RpcMethod.GET_STATUS, {}, session_id="s1")

    assert told == []


async def test_a_slash_command_is_sent_as_a_command_and_answered_as_an_outcome(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.answer = msg.CommandResult(kind="error", content="done", display_content="Done.")

    outcome = await gateway.command("/status", session_id="s1")

    [(sent, session)] = backend.asked
    assert isinstance(sent, msg.Command) and sent.text == "/status" and session == "s1"
    assert (outcome.content, outcome.display_content) == ("done", "Done.")
    assert outcome.kind == "error"


async def test_attaching_a_session_returns_what_the_backend_attached(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.answer = SessionAttached(session_id="s1", project_dir="/p", created=True)

    attached = await gateway.attach("s1", None)

    assert attached is backend.answer
    assert backend.attached == [("s1", None)]


async def test_an_attach_the_backend_refuses_is_refused_with_its_message(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.failure = ValueError("that folder is not a project")

    with pytest.raises(HostModelError) as refused:
        await gateway.attach("s1", "/nope")

    assert refused.value.code is ErrorCode.REFUSED
    assert refused.value.message == "that folder is not a project"


async def test_the_account_is_read_from_the_status(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.answer = {"cloud_email": "a@b.c", "cloud_name": "Ann", "cloud_org": "Acme"}

    account = await gateway.account("s1")

    assert backend.rpcs == [(RpcMethod.GET_STATUS, {}, "s1")]
    assert (account.signed_in, account.email, account.name, account.org) == (
        True,
        "a@b.c",
        "Ann",
        "Acme",
    )


async def test_the_account_of_an_empty_status_is_signed_out(
    gateway: HostBackendGateway, backend: Backend
) -> None:
    backend.answer = {}

    account = await gateway.account("s1")

    assert (account.signed_in, account.email, account.name, account.org) == (False, "", "", "")


async def test_the_default_session_is_the_backends(gateway: HostBackendGateway) -> None:
    assert gateway.default_session() == "boot"
