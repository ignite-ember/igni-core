"""``host.self`` and ``host.extensions``: an extension's settings, its lease on igni's wakefulness.

Driven through the real host model: what an extension asks, what it
is answered, what it is refused, and what is left behind.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.host_model_helpers import HostRig


@pytest.fixture
async def rig(tmp_path: Path):
    rig = await HostRig(tmp_path).start()
    try:
        yield rig
    finally:
        assert rig.model is not None
        # The activity registry is shared by the process: let go of what a test took.
        for extension_id in ("acme.reviewer", "acme.indexer", "acme.other"):
            rig.model.services.busy.release_all(extension_id)
        await rig.stop()


def ok(reply: Any) -> Any:
    assert reply.ok, f"refused: {reply.error}"
    return reply.result


def refused(reply: Any, code: str) -> str:
    assert not reply.ok, f"answered {reply.result!r}"
    assert reply.error.code == code, reply.error
    return reply.error.message


class TestTheBusyLease:
    async def test_a_lease_needs_a_reason(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        message = refused(await rig.ask(ext, "call", "self.busy", reason=""), "invalid")
        assert "reason" in message
        activity = ok(await rig.ask(ext, "get", "app.activity"))
        assert [b for b in activity["busy"] if b["holder"] == ext] == []

    async def test_a_lease_without_a_ttl_still_ends_on_its_own(self, rig: HostRig) -> None:
        """An extension that forgets ``done`` does not keep igni awake for ever."""
        ext = rig.install(host=["app"])
        lease = ok(await rig.ask(ext, "call", "self.busy", reason="indexing"))
        assert lease["holder"] == ext
        assert lease["expires_at"] is not None

    async def test_a_lease_with_a_ttl_expires_after_it(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        lease = ok(await rig.ask(ext, "call", "self.busy", reason="indexing", ttl_seconds=30))
        since = datetime.fromisoformat(lease["since"])
        expires = datetime.fromisoformat(lease["expires_at"])
        assert (expires - since).total_seconds() == pytest.approx(30, abs=1)

    async def test_done_lets_go_of_the_lease_it_names_and_no_other(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        ok(await rig.ask(ext, "call", "self.busy", reason="indexing"))
        ok(await rig.ask(ext, "call", "self.busy", reason="syncing"))
        ok(await rig.ask(ext, "call", "self.done", reason="indexing"))
        activity = ok(await rig.ask(ext, "get", "app.activity"))
        assert [b["reason"] for b in activity["busy"]] == ["syncing"]

    async def test_done_lets_go_of_its_own_lease_and_no_one_elses(self, rig: HostRig) -> None:
        mine = rig.install("acme.indexer", host=["app"])
        other = rig.install("acme.other", host=["app"])
        ok(await rig.ask(mine, "call", "self.busy", reason="indexing"))
        ok(await rig.ask(other, "call", "self.busy", reason="indexing"))
        ok(await rig.ask(mine, "call", "self.done", reason="indexing"))
        activity = ok(await rig.ask(mine, "get", "app.activity"))
        assert [b["holder"] for b in activity["busy"]] == ["acme.other"]

    async def test_done_without_a_lease_is_nothing_to_let_go_of(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        assert ok(await rig.ask(ext, "call", "self.done", reason="never held")) is None

    async def test_done_needs_its_reason(self, rig: HostRig) -> None:
        ext = rig.install(host=["app"])
        refused(await rig.ask(ext, "call", "self.done"), "invalid")


class TestItsOwnSettings:
    async def test_settings_are_an_object(self, rig: HostRig) -> None:
        ext = rig.install()
        for value in ([1], "text", None, 3):
            refused(await rig.ask(ext, "set", "self.settings", to=value), "invalid")
        assert ok(await rig.ask(ext, "get", "self.settings")).get("serviceUrl") in (None, "")

    async def test_settings_written_are_the_settings_read(self, rig: HostRig) -> None:
        ext = rig.install()
        ok(await rig.ask(ext, "set", "self.settings", to={"serviceUrl": "https://b.test"}))
        assert ok(await rig.ask(ext, "get", "self.settings")) == {"serviceUrl": "https://b.test"}

    async def test_its_id_and_state_are_its_own(self, rig: HostRig) -> None:
        ext = rig.install("acme.reviewer")
        rig.install("acme.other")
        assert ok(await rig.ask(ext, "get", "self.id")) == "acme.reviewer"
        state = ok(await rig.ask(ext, "get", "self.state"))
        assert state["id"] == "acme.reviewer"


class TestTheSettingsOfTheInstalledExtensions:
    async def test_settings_are_saved_and_the_saved_values_answered(self, rig: HostRig) -> None:
        ext = rig.install(host=["extensions"])
        saved = ok(
            await rig.ask(
                ext, "call", "extensions.set_settings", values={"serviceUrl": "https://a.test"}
            )
        )
        assert saved == {"values": {"serviceUrl": "https://a.test"}}
        assert ok(await rig.ask(ext, "get", "self.settings"))["serviceUrl"] == "https://a.test"

    async def test_a_setting_the_extension_did_not_declare_is_refused_and_not_saved(
        self, rig: HostRig
    ) -> None:
        ext = rig.install(host=["extensions"])
        message = refused(
            await rig.ask(ext, "call", "extensions.set_settings", values={"backdoor": "1"}),
            "refused",
        )
        assert "backdoor" in message
        assert "backdoor" not in ok(await rig.ask(ext, "get", "self.settings"))

    async def test_the_values_are_an_object(self, rig: HostRig) -> None:
        ext = rig.install(host=["extensions"])
        refused(await rig.ask(ext, "call", "extensions.set_settings", values="x"), "invalid")
