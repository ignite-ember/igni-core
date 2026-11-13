"""``host.notifications``: the floor's notifications, as far as the grant reaches.

Without the ``notifications`` grant an extension sees, counts and marks
its own and nothing else; with it, everyone's. ``notify`` is accepted or
refused as a notice arriving over MCP is. Driven through the real host
model; the floor's answers come from :func:`tests.scripted_backend.script`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.extensions.host.local_notices import LocalNotices
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver
from igni_core.notifications.schemas import ExtensionNotice, FloorNotification
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig
from tests.scripted_backend import ScriptedBackend, script

MINE = "acme.reviewer"
OTHER = "acme.other"

ROWS = [
    {"id": "n1", "source": MINE, "title": "mine", "origin": "local"},
    {"id": "n2", "source": OTHER, "title": "theirs", "origin": "local"},
    {"id": "n3", "source": "server", "title": "from the server", "origin": "server"},
]
COUNTS = {"unread": 6, "by_source": {MINE: 1, OTHER: 2, "server": 3}}


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


def floor(rig: HostRig, **more: Any) -> ScriptedBackend:
    answers: dict[RpcMethod, Any] = {
        RpcMethod.NOTIFICATIONS_LIST: {"notifications": ROWS},
        RpcMethod.NOTIFICATIONS_COUNTS: COUNTS,
        RpcMethod.NOTIFICATIONS_MARK_READ: {"counts": {"unread": 5, "by_source": {OTHER: 2}}},
    }
    return script(rig, answers | {RpcMethod(k): v for k, v in more.items()})


class TestWhatIsListedAndCounted:
    async def test_without_the_grant_the_list_is_its_own(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        floor(rig)
        rows = ok(await rig.ask(ext, "get", "notifications.list"))
        assert [r["id"] for r in rows] == ["n1"]
        assert rows[0]["title"] == "mine"

    async def test_with_the_grant_the_list_is_everyones_in_the_floors_order(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(MINE, host=["notifications"])
        floor(rig)
        rows = ok(await rig.ask(ext, "get", "notifications.list"))
        assert [r["id"] for r in rows] == ["n1", "n2", "n3"]

    async def test_without_the_grant_the_count_is_its_own(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        floor(rig)
        assert ok(await rig.ask(ext, "get", "notifications.unread")) == {
            "unread": 1,
            "by_source": {MINE: 1},
        }

    async def test_without_the_grant_and_nothing_unread_the_count_is_empty(
        self, rig: HostRig
    ) -> None:
        ext = rig.install("acme.quiet")
        floor(rig)
        assert ok(await rig.ask(ext, "get", "notifications.unread")) == {
            "unread": 0,
            "by_source": {},
        }

    async def test_with_the_grant_the_count_is_the_floors(self, rig: HostRig) -> None:
        ext = rig.install(MINE, host=["notifications"])
        floor(rig)
        assert ok(await rig.ask(ext, "get", "notifications.unread")) == COUNTS

    async def test_a_floor_with_no_rows_is_an_empty_list(self, rig: HostRig) -> None:
        ext = rig.install(MINE, host=["notifications"])
        floor(rig, notifications_list={"notifications": []})
        assert ok(await rig.ask(ext, "get", "notifications.list")) == []

    async def test_a_list_that_is_not_an_object_is_an_empty_list(self, rig: HostRig) -> None:
        ext = rig.install(MINE, host=["notifications"])
        floor(rig, notifications_list=None)
        assert ok(await rig.ask(ext, "get", "notifications.list")) == []


class TestMarkingRead:
    async def test_without_the_grant_nothing_named_means_its_own_source(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        ok(await rig.ask(ext, "call", "notifications.mark_read"))
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == [{"ids": None, "source": MINE}]

    async def test_without_the_grant_an_empty_list_of_ids_means_its_own_source_too(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        ok(await rig.ask(ext, "call", "notifications.mark_read", ids=[]))
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == [{"ids": [], "source": MINE}]

    async def test_without_the_grant_its_own_ids_are_marked(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        ok(await rig.ask(ext, "call", "notifications.mark_read", ids=["n1"]))
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == [{"ids": ["n1"], "source": None}]

    async def test_without_the_grant_its_own_source_is_marked(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        ok(await rig.ask(ext, "call", "notifications.mark_read", source=MINE))
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == [{"ids": None, "source": MINE}]

    async def test_without_the_grant_another_source_is_refused_and_nothing_is_marked(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        message = refused(
            await rig.ask(ext, "call", "notifications.mark_read", source=OTHER), "not_granted"
        )
        assert "notifications" in message
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == []

    @pytest.mark.parametrize("ids", [["n2"], ["n1", "n2"], ["n3"], ["nope"]])
    async def test_without_the_grant_ids_that_are_not_its_own_are_refused(
        self, rig: HostRig, ids: list[str]
    ) -> None:
        ext = rig.install(MINE)
        backend = floor(rig)
        refused(await rig.ask(ext, "call", "notifications.mark_read", ids=ids), "not_granted")
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == []

    async def test_with_the_grant_what_is_named_is_marked_as_named(self, rig: HostRig) -> None:
        ext = rig.install(MINE, host=["notifications"])
        backend = floor(rig)
        ok(await rig.ask(ext, "call", "notifications.mark_read", ids=["n2"]))
        ok(await rig.ask(ext, "call", "notifications.mark_read", source=OTHER))
        ok(await rig.ask(ext, "call", "notifications.mark_read"))
        assert backend.asked(RpcMethod.NOTIFICATIONS_MARK_READ) == [
            {"ids": ["n2"], "source": None},
            {"ids": None, "source": OTHER},
            {"ids": None, "source": None},
        ]

    async def test_the_answer_is_the_counts_the_grant_reaches(self, rig: HostRig) -> None:
        mine = rig.install(MINE)
        floor(rig, notifications_mark_read={"counts": COUNTS})
        assert ok(await rig.ask(mine, "call", "notifications.mark_read")) == {
            "unread": 1,
            "by_source": {MINE: 1},
        }
        everyone = rig.install("acme.admin", host=["notifications"])
        assert ok(await rig.ask(everyone, "call", "notifications.mark_read")) == COUNTS

    async def test_an_answer_that_carries_no_counts_is_empty_counts(self, rig: HostRig) -> None:
        ext = rig.install(MINE, host=["notifications"])
        floor(rig, notifications_mark_read=None)
        assert ok(await rig.ask(ext, "call", "notifications.mark_read")) == {
            "unread": 0,
            "by_source": {},
        }


class Sink:
    """Where an accepted notice goes: kept, once per event."""

    def __init__(self) -> None:
        self.kept: list[tuple[str, ExtensionNotice]] = []

    def keep_local(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        if any(n.event_key == notice.event_key for _, n in self.kept):
            return None
        self.kept.append((source, notice))
        return FloorNotification.model_construct()


NOTICE = {
    "kind": "export.ready",
    "title": "Your export is ready",
    "body": "42 files",
    "target": {"page": "acme.reviewer.home", "params": {"export": "42"}},
    "event_key": "export-1",
}


class TestNotify:
    async def test_a_notice_is_kept_as_the_callers_own(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        sink = Sink()
        assert rig.model is not None
        rig.model.services.notices = LocalNotices(
            ExtensionNoticeReceiver(registry=rig.registry, sink=sink)
        )
        assert ok(await rig.ask(ext, "call", "notifications.notify", notification=NOTICE)) is None
        [(source, notice)] = sink.kept
        assert source == MINE
        assert notice.title == "Your export is ready"
        assert notice.body == "42 files"
        assert notice.target.params == {"export": "42"}

    async def test_a_notice_without_its_optional_fields_is_kept_with_their_defaults(
        self, rig: HostRig
    ) -> None:
        """``body`` and the target's ``params`` are optional in the schema: leaving
        them out is not a malformed notice."""
        ext = rig.install(MINE)
        sink = Sink()
        assert rig.model is not None
        rig.model.services.notices = LocalNotices(
            ExtensionNoticeReceiver(registry=rig.registry, sink=sink)
        )
        bare = {k: v for k, v in NOTICE.items() if k != "body"} | {
            "target": {"page": "acme.reviewer.home"}
        }
        ok(await rig.ask(ext, "call", "notifications.notify", notification=bare))
        [(_, notice)] = sink.kept
        assert notice.body == ""
        assert notice.target.params == {}

    async def test_a_notice_pointing_at_a_page_that_is_not_its_own_is_refused(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(MINE)
        sink = Sink()
        assert rig.model is not None
        rig.model.services.notices = LocalNotices(
            ExtensionNoticeReceiver(registry=rig.registry, sink=sink)
        )
        bad = NOTICE | {"target": {"page": "acme.other.home"}}
        message = refused(
            await rig.ask(ext, "call", "notifications.notify", notification=bad), "refused"
        )
        assert "acme.other.home" in message
        assert sink.kept == []

    async def test_an_igni_that_keeps_no_notifications_says_so(self, rig: HostRig) -> None:
        ext = rig.install(MINE)
        message = refused(
            await rig.ask(ext, "call", "notifications.notify", notification=NOTICE), "failed"
        )
        assert "no notifications" in message
