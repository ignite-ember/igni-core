"""``SessionTargets``: ``host.sessions.chat(id)`` reaches another session only as far as it is allowed.

The on-screen session is ``host.chat`` and nothing else. Another session of
this project may be read, and acted on only by a method that names the
grant that takes it, held by the extension. One that is not this project's
is not found, and asking never creates it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.session_target import SessionTarget, SessionTargets
from igni_core.extensions.host.spec import ErrorCode, ObjectSpec, method, prop, signal
from igni_core.extensions.host.wire import Op, Request
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest

CHAT = ObjectSpec(
    path="chat",
    doc="",
    permission="chat",
    per_session=True,
    properties=(prop("processing", "bool", "", writable=True),),
    signals=(signal("run_started", "bool", ""),),
    methods=(
        method("post", "", elsewhere="chat.write"),
        method("cancel", ""),
    ),
)
NOT_PER_SESSION = ObjectSpec(path="app", doc="", permission=None)

PROCESSING = CHAT.member("processing")
POST = CHAT.member("post")
CANCEL = CHAT.member("cancel")
RUN_STARTED = CHAT.member("run_started")


class Backend:
    def __init__(self, live: list[str], listed: Any, fails: bool = False) -> None:
        self.live = live
        self.listed = listed
        self.fails = fails
        self.rpcs: list[tuple[RpcMethod, dict[str, Any], str]] = []

    def live_sessions(self) -> list[str]:
        return list(self.live)

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        self.rpcs.append((method, args, session_id))
        if self.fails:
            raise HostModelError(ErrorCode.REFUSED, "cannot list")
        return self.listed


class Gateway:
    def __init__(self) -> None:
        self.attached: list[tuple[str, str | None]] = []

    async def attach(self, session_id: str, project_dir: str | None) -> Any:
        self.attached.append((session_id, project_dir))
        return SimpleNamespace(session_id=session_id)


class Rig:
    def __init__(self, live: list[str] | None = None, listed: Any = None, fails: bool = False):
        self.backend = Backend(live or [], {"sessions": []} if listed is None else listed, fails)
        self.gateway = Gateway()
        self.targets = SessionTargets(SimpleNamespace(backend=self.backend, gateway=self.gateway))  # type: ignore[arg-type]

    async def resolve(
        self,
        member: Any,
        *,
        session: str | None,
        op: Op = Op.CALL,
        spec: ObjectSpec = CHAT,
        on_screen: str = "s-main",
        **permissions: Any,
    ) -> SessionTarget:
        return await self.targets.resolve(
            grants=HostGrants(manifest("acme.one", **permissions)),
            spec=spec,
            member=member,
            request=Request(id=1, op=op, path=f"chat.{member.name}", session=session),
            on_screen=on_screen,
        )


async def refused(rig: Rig, member: Any, **kwargs: Any) -> HostModelError:
    with pytest.raises(HostModelError) as caught:
        await rig.resolve(member, **kwargs)
    return caught.value


@pytest.mark.parametrize("session", [None, "s-main"])
async def test_no_session_or_the_one_on_screen_is_the_one_on_screen(session: str | None) -> None:
    rig = Rig()

    target = await rig.resolve(CANCEL, session=session)

    assert target == SessionTarget(session_id="s-main", on_screen=True)
    assert rig.backend.rpcs == [] and rig.gateway.attached == []


async def test_the_session_on_screen_named_by_its_id_needs_no_elsewhere_grant() -> None:
    target = await Rig().resolve(CANCEL, session="s-main", op=Op.CALL)

    assert target.on_screen is True
    assert (await Rig().resolve(PROCESSING, session="s-main", op=Op.SET)).on_screen is True


async def test_a_session_named_on_an_object_no_session_owns_is_invalid() -> None:
    error = await refused(
        Rig(), CANCEL, session="s-other", spec=NOT_PER_SESSION, **{"host": ["chat.*"]}
    )

    assert error.code is ErrorCode.INVALID
    assert error.message == "'app' does not belong to a session"


async def test_naming_the_on_screen_session_is_still_refused_on_an_object_no_session_owns() -> None:
    error = await refused(Rig(), CANCEL, session="s-main", spec=NOT_PER_SESSION)

    assert error.code is ErrorCode.INVALID


async def test_an_empty_session_id_is_invalid() -> None:
    error = await refused(Rig(), CANCEL, session="")

    assert error.code is ErrorCode.INVALID
    assert error.message == "a session id is required"


async def test_another_session_s_property_may_be_read() -> None:
    rig = Rig(live=["s-other"])

    target = await rig.resolve(PROCESSING, session="s-other", op=Op.GET)

    assert target == SessionTarget(session_id="s-other", on_screen=False)


async def test_another_session_s_property_may_never_be_set() -> None:
    rig = Rig(live=["s-other"])

    error = await refused(rig, PROCESSING, session="s-other", op=Op.SET, host=["chat.*"])

    assert error.code is ErrorCode.REFUSED
    assert error.message == "'chat.processing' is set only in the session on screen"
    assert rig.gateway.attached == []


async def test_a_method_without_an_elsewhere_grant_acts_only_on_the_session_on_screen() -> None:
    rig = Rig(live=["s-other"])

    error = await refused(rig, CANCEL, session="s-other", host=["chat.*"])

    assert error.code is ErrorCode.REFUSED
    assert error.message.startswith("'chat.cancel' acts only on the session on screen")
    assert rig.gateway.attached == []


async def test_an_extension_without_the_elsewhere_grant_is_not_granted() -> None:
    rig = Rig(live=["s-other"])

    error = await refused(rig, POST, session="s-other", host=["chat.read"])

    assert error.code is ErrorCode.NOT_GRANTED
    assert "'chat.write'" in error.message
    assert "'chat.post' in a session that is not on screen" in error.message
    assert rig.gateway.attached == []


async def test_an_extension_with_the_elsewhere_grant_reaches_a_live_session() -> None:
    rig = Rig(live=["s-other"])

    target = await rig.resolve(POST, session="s-other", host=["chat.write"])

    assert target == SessionTarget(session_id="s-other", on_screen=False)
    assert rig.gateway.attached == [("s-other", None)]
    assert rig.backend.rpcs == [], "a live session needs no listing"


async def test_a_signal_passes_the_elsewhere_check() -> None:
    rig = Rig(live=["s-other"])

    target = await rig.resolve(RUN_STARTED, session="s-other", op=Op.SUBSCRIBE)

    assert target.on_screen is False


async def test_a_session_that_is_not_live_but_is_in_the_project_list_is_reached() -> None:
    rig = Rig(listed={"sessions": [{"session_id": "s-old"}, {"session_id": "s-other"}]})

    target = await rig.resolve(POST, session="s-old", host=["chat.write"])

    assert target == SessionTarget(session_id="s-old", on_screen=False)
    assert rig.gateway.attached == [("s-old", None)]


async def test_the_list_is_asked_of_the_session_on_screen_never_of_the_one_named() -> None:
    rig = Rig(listed={"sessions": [{"session_id": "s-old"}]})

    await rig.resolve(POST, session="s-old", host=["chat.write"])

    assert rig.backend.rpcs == [(RpcMethod.LIST_SESSIONS, {}, "s-main")]


async def test_a_bare_list_of_sessions_is_read_too() -> None:
    rig = Rig(listed=[{"session_id": "s-old"}])

    assert (await rig.resolve(POST, session="s-old", host=["chat.write"])).on_screen is False


@pytest.mark.parametrize(
    "listed",
    [
        {"sessions": [{"session_id": "s-else"}]},
        {"sessions": []},
        {"sessions": None},
        {},
        [],
        None,
        "nonsense",
        {"sessions": ["s-old", 3, None, {"title": "no id"}]},
    ],
)
async def test_a_session_that_is_not_in_the_project_is_not_found_and_not_created(
    listed: Any,
) -> None:
    rig = Rig(listed=listed)
    rig.backend.listed = listed

    error = await refused(rig, POST, session="s-old", host=["chat.write"])

    assert error.code is ErrorCode.NOT_FOUND
    assert error.message == "no session 's-old' in this project"
    assert rig.gateway.attached == []


async def test_a_listing_that_fails_means_the_session_is_not_known() -> None:
    rig = Rig(fails=True)

    error = await refused(rig, POST, session="s-old", host=["chat.write"])

    assert error.code is ErrorCode.NOT_FOUND
    assert rig.gateway.attached == []


async def test_a_session_that_is_not_known_is_refused_for_the_grant_first() -> None:
    error = await refused(Rig(), POST, session="s-old", host=["chat.read"])

    assert error.code is ErrorCode.NOT_GRANTED


async def test_a_refused_attach_is_the_gateways_refusal() -> None:
    rig = Rig(live=["s-other"])

    async def attach(session_id: str, project_dir: str | None) -> Any:
        raise HostModelError(ErrorCode.REFUSED, "another project")

    rig.gateway.attach = attach  # type: ignore[method-assign]

    error = await refused(rig, POST, session="s-other", host=["chat.write"])

    assert error.code is ErrorCode.REFUSED and error.message == "another project"
