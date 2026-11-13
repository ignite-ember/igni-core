"""``host.app``, ``host.app.state``, ``host.windows``, ``host.chat.post`` and
``host.chat``'s views: what each member sends, answers and refuses.

Driven over a scripted host (see ``Gateway``): the gateway, the backend
and the window pushes record what they are told, so each refusal's code
and words, each signal and each value reaching igni is the check.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from igni_core.core.session.client_state import ClientStateRead, ClientStateResult
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.host_state import HostState
from igni_core.extensions.host.members.app import (
    AppMembers,
    AppStateChanged,
    AppStateMembers,
    WindowMembers,
)
from igni_core.extensions.host.members.chat import ChatWriteMembers
from igni_core.extensions.host.members.chat_views import ChatViewMembers
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.values import UpdateInfo
from igni_core.protocol.rpc import RpcMethod
from tests.test_the_host_members_translate_what_an_extension_asks import Gateway, a_call

SPEC = {"root": "c", "elements": {"c": {"type": "Card", "props": {"title": "Hi"}}}}


class Emitted:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def emit(
        self, wire: str, value: Any, *, content: Any = None, only: Any = None, window: Any = None
    ) -> None:
        self.sent.append({"wire": wire, "value": value, "only": only})


class Store:
    """A client's state store: ``fail`` makes every operation fail."""

    def __init__(self, fail: str = "") -> None:
        self.rows: dict[str, dict[str, str]] = {}
        self.fail = fail

    def get_for_client(self, client_id: str) -> ClientStateRead:
        if self.fail:
            return ClientStateRead(ok=False, error=self.fail)
        return ClientStateRead(values=dict(self.rows.get(client_id, {})))

    def set_value(self, client_id: str, key: str, value: str) -> ClientStateResult:
        if self.fail:
            return ClientStateResult(ok=False, error=self.fail)
        self.rows.setdefault(client_id, {})[key] = value
        return ClientStateResult()

    def delete_value(self, client_id: str, key: str) -> ClientStateResult:
        if self.fail:
            return ClientStateResult(ok=False, error=self.fail)
        self.rows.get(client_id, {}).pop(key, None)
        return ClientStateResult()


class Backend:
    def __init__(self, store: Store | None = None) -> None:
        self.client_state = store or Store()
        self.posted: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self.delivery = SimpleNamespace(ok=True, reason="")
        self.shown: list[dict[str, Any]] = []
        self.shown_views: list[Any] = ["a view"]
        self.the_activity = "idle"

    async def post(self, **kw: Any) -> None:
        self.posted.append(kw)

    async def deliver(self, **kw: Any) -> Any:
        self.delivered.append(kw)
        return self.delivery

    async def show_view(self, **kw: Any) -> None:
        self.shown.append(kw)

    def views(self, session_id: str) -> list[Any]:
        return [session_id, *self.shown_views]

    def activity(self) -> str:
        return self.the_activity


class Pushes:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str, str]] = []

    def open_external(self, window: str, url: str, *, source: str) -> None:
        self.opened.append((window, url, source))


class Registry:
    def describe_all(self) -> list[str]:
        return ["w-1", "w-2"]

    def focused(self) -> str | None:
        return None


class Rig:
    def __init__(self, gateway: Gateway | None = None, backend: Backend | None = None) -> None:
        self.gateway = gateway or Gateway(
            {
                RpcMethod.GET_DISPLAY_CONFIG: {"show_routing": False},
                RpcMethod.GET_OUTPUT_STYLES: {"active": "terse", "styles": [{"name": "terse"}]},
            }
        )
        self.backend = backend or Backend()
        self.bus = Emitted()
        self.pushes = Pushes()
        self.host: Any = SimpleNamespace(
            gateway=self.gateway,
            backend=self.backend,
            bus=self.bus,
            pushes=self.pushes,
            state=HostState(),
            windows=SimpleNamespace(registry=Registry()),
        )


class TestSetVerbose:
    async def test_it_toggles_only_when_the_display_differs_and_answers_the_new_display(
        self,
    ) -> None:
        rig = Rig()
        shown = {"show_routing": False}
        original = rig.gateway.rpc

        async def rpc(method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
            if method is RpcMethod.TOGGLE_VERBOSE:
                shown["show_routing"] = not shown["show_routing"]
                rig.gateway.calls.append((method, args, session_id))
                return None
            rig.gateway.answers[RpcMethod.GET_DISPLAY_CONFIG] = dict(shown)
            return await original(method, args, session_id=session_id)

        rig.gateway.rpc = rpc  # type: ignore[method-assign]
        members = AppMembers(rig.host)
        on = await members.set_verbose(a_call(on=True))
        assert on.verbose is True
        again = await members.set_verbose(a_call(on=True))
        assert again.verbose is True
        toggles = [m for m, _, _ in rig.gateway.calls if m is RpcMethod.TOGGLE_VERBOSE]
        assert len(toggles) == 1
        off = await members.set_verbose(a_call(on=False))
        assert off.verbose is False
        toggles = [m for m, _, _ in rig.gateway.calls if m is RpcMethod.TOGGLE_VERBOSE]
        assert len(toggles) == 2


class TestSetOutputStyle:
    @pytest.mark.parametrize("name", ["", "   ", "two words", "tab\tname"])
    async def test_a_blank_or_spaced_name_is_invalid_and_runs_nothing(self, name: str) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await AppMembers(rig.host).set_output_style(a_call(name=name))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == f"{name.strip()!r} is not an output style name"
        assert rig.gateway.commands == []

    async def test_an_error_outcome_is_refused_with_its_content(self) -> None:
        rig = Rig()
        rig.gateway.outcome = SimpleNamespace(
            kind="error", content="no such style", display_content=""
        )
        with pytest.raises(HostModelError) as caught:
            await AppMembers(rig.host).set_output_style(a_call(name="loud"))
        assert caught.value.code == ErrorCode.REFUSED
        assert caught.value.message == "no such style"
        assert rig.bus.sent == []

    async def test_an_error_outcome_with_no_content_is_refused_as_refused(self) -> None:
        rig = Rig()
        rig.gateway.outcome = SimpleNamespace(kind="error", content="", display_content="")
        with pytest.raises(HostModelError) as caught:
            await AppMembers(rig.host).set_output_style(a_call(name="loud"))
        assert caught.value.message == "refused"

    async def test_a_style_is_run_trimmed_and_the_display_answered_and_signalled(self) -> None:
        rig = Rig()
        display = await AppMembers(rig.host).set_output_style(a_call(name="  terse "))
        assert rig.gateway.commands == [("/output-style terse", "s1")]
        assert display.output_style == "terse"
        [sent] = rig.bus.sent
        assert sent["wire"] == "app.display_changed"
        assert sent["value"]["output_style"] == "terse"


class TestUpdates:
    async def test_the_recorded_update_is_what_update_available_answers(self) -> None:
        rig = Rig()
        members = AppMembers(rig.host)
        assert await members.update_available(a_call()) is None
        rig.gateway.answers[RpcMethod.CHECK_FOR_UPDATE] = {
            "available": True,
            "current_version": "1.0",
            "latest_version": "1.1",
            "download_url": "https://x.test/d",
        }
        found = await members.check_for_update(a_call())
        assert found == UpdateInfo(
            current_version="1.0", latest_version="1.1", download_url="https://x.test/d"
        )
        assert await members.update_available(a_call()) == found
        [sent] = rig.bus.sent
        assert sent["wire"] == UpdateInfo.CHANGED
        assert sent["value"]["latest_version"] == "1.1"

    async def test_no_newer_release_clears_the_record_and_signals_nothing_newer(self) -> None:
        rig = Rig()
        rig.host.state.record_update(
            UpdateInfo(current_version="1", latest_version="2", download_url="https://x.test")
        )
        rig.gateway.answers[RpcMethod.CHECK_FOR_UPDATE] = None
        assert await AppMembers(rig.host).check_for_update(a_call()) is None
        assert rig.host.state.update is None
        assert [(s["wire"], s["value"]) for s in rig.bus.sent] == [(UpdateInfo.CHANGED, None)]


class TestOpenExternal:
    async def test_a_link_is_pushed_to_the_callers_window_as_the_extension(self) -> None:
        rig = Rig()
        await AppMembers(rig.host).open_external(a_call(window="w-1", url="https://igni.test/x"))
        assert rig.pushes.opened == [("w-1", "https://igni.test/x", "acme.reviewer")]

    @pytest.mark.parametrize("url", ["http://igni.test", "file:///etc/passwd", "https://", "x y"])
    async def test_a_link_that_is_not_https_to_a_host_is_invalid_and_not_pushed(
        self, url: str
    ) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await AppMembers(rig.host).open_external(a_call(window="w-1", url=url))
        assert caught.value.code == ErrorCode.INVALID
        assert rig.pushes.opened == []

    async def test_with_no_window_it_is_not_found_before_the_link_is_read(self) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await AppMembers(rig.host).open_external(a_call(window=None, url="not a link"))
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert caught.value.message == "no igni window is open"


class TestAppState:
    async def test_a_value_is_kept_under_the_extensions_own_client_and_signalled_to_it_only(
        self,
    ) -> None:
        rig = Rig()
        members = AppStateMembers(rig.host)
        await members.set_value(a_call(key="k", value=7))
        assert rig.backend.client_state.rows == {"ext:acme.reviewer": {"k": "7"}}
        assert await members.values(a_call()) == {"k": "7"}
        assert await members.get_value(a_call(key="k")) == "7"
        assert await members.get_value(a_call(key="absent")) is None
        [sent] = rig.bus.sent
        assert sent["wire"] == AppStateChanged.WIRE
        assert sent["value"] == {"key": "k", "value": "7"}
        assert sent["only"] == "acme.reviewer"

    async def test_two_extensions_keep_the_same_key_apart(self) -> None:
        rig = Rig()
        members = AppStateMembers(rig.host)
        other = a_call(key="k", value="theirs").model_copy(update={"extension_id": "other.ext"})
        await members.set_value(a_call(key="k", value="mine"))
        await members.set_value(other)
        assert rig.backend.client_state.rows == {
            "ext:acme.reviewer": {"k": "mine"},
            "ext:other.ext": {"k": "theirs"},
        }

    async def test_an_empty_key_is_invalid_and_stores_nothing(self) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await AppStateMembers(rig.host).set_value(a_call(key="", value="v"))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == "a state key cannot be empty"
        assert rig.backend.client_state.rows == {}
        assert rig.bus.sent == []

    async def test_a_delete_removes_the_key_and_signals_it_as_none(self) -> None:
        rig = Rig()
        members = AppStateMembers(rig.host)
        await members.set_value(a_call(key="k", value="v"))
        await members.delete_value(a_call(key="k"))
        assert await members.values(a_call()) == {}
        assert rig.bus.sent[-1]["value"] == {"key": "k", "value": None}
        assert rig.bus.sent[-1]["only"] == "acme.reviewer"

    async def test_a_store_that_fails_is_a_failure_with_its_sentence_and_no_signal(self) -> None:
        rig = Rig(backend=Backend(Store(fail="the store is closed")))
        members = AppStateMembers(rig.host)
        for attempt in (
            members.values(a_call()),
            members.get_value(a_call(key="k")),
            members.set_value(a_call(key="k", value="v")),
            members.delete_value(a_call(key="k")),
        ):
            with pytest.raises(HostModelError) as caught:
                await attempt
            assert caught.value.code == ErrorCode.FAILED
            assert caught.value.message == "the store is closed"
        assert rig.bus.sent == []


class TestWindowsAndTheRest:
    async def test_the_windows_are_what_the_registry_describes(self) -> None:
        rig = Rig()
        members = WindowMembers(rig.host)
        assert await members.window_list(a_call()) == ["w-1", "w-2"]
        assert await members.focused(a_call()) is None

    async def test_the_activity_is_the_backends(self) -> None:
        rig = Rig()
        rig.backend.the_activity = "busy"
        assert await AppMembers(rig.host).activity(a_call()) == "busy"

    async def test_the_connection_is_read_from_the_status(self) -> None:
        rig = Rig()
        rig.gateway.answers[RpcMethod.GET_STATUS] = {
            "cloud_connected": True,
            "model_configured": False,
        }
        got = await AppMembers(rig.host).connection(a_call())
        assert (got.cloud_connected, got.model_configured) == (True, False)


class TestPostingOffScreen:
    @staticmethod
    def off_screen() -> Any:
        c = a_call(text="Indexed", attachments={"a.txt": "body"})
        return c.model_copy(update={"target": SessionTarget(session_id="s2", on_screen=False)})

    async def test_it_goes_through_the_service_event_path_not_the_chat_post(self) -> None:
        rig = Rig()
        await ChatWriteMembers(rig.host).post(self.off_screen())
        assert rig.backend.posted == []
        [delivery] = rig.backend.delivered
        assert delivery["session_id"] == "s2"
        assert delivery["extension_id"] == "acme.reviewer"
        assert delivery["text"] == "Indexed"
        assert delivery["media"].files == {"a.txt": "body"}

    async def test_a_session_that_does_not_accept_it_refuses_with_the_reason(self) -> None:
        rig = Rig()
        rig.backend.delivery = SimpleNamespace(ok=False, reason="an approval is waiting")
        with pytest.raises(HostModelError) as caught:
            await ChatWriteMembers(rig.host).post(self.off_screen())
        assert caught.value.code == ErrorCode.REFUSED
        assert caught.value.message == "an approval is waiting"

    async def test_on_screen_it_is_posted_unqueued_with_its_attachments(self) -> None:
        rig = Rig()
        await ChatWriteMembers(rig.host).post(a_call(text="Hi", attachments={"a.txt": "b"}))
        assert rig.backend.delivered == []
        [sent] = rig.backend.posted
        assert (sent["session_id"], sent["queued"], sent["text"]) == ("s1", False, "Hi")
        assert sent["media"].files == {"a.txt": "b"}

    @pytest.mark.parametrize("attachments", [None, {}])
    async def test_no_attachments_is_an_empty_set(self, attachments: Any) -> None:
        rig = Rig()
        await ChatWriteMembers(rig.host).post(a_call(text="Hi", attachments=attachments))
        assert rig.backend.posted[0]["media"].files == {}

    async def test_a_queued_message_is_posted_queued_with_no_attachments(self) -> None:
        rig = Rig()
        await ChatWriteMembers(rig.host).queue(a_call(text="Later é"))
        [sent] = rig.backend.posted
        assert (sent["queued"], sent["text"]) == (True, "Later é")
        assert sent["media"].files == {}


class TestViews:
    async def test_an_append_shows_the_card_as_the_log_keeps_it_and_returns_a_fresh_id(
        self,
    ) -> None:
        rig = Rig()
        members = ChatViewMembers(rig.host)
        first = await members.append(a_call(view=SPEC, title="Indexed"))
        second = await members.append(a_call(view=SPEC))
        assert first != second and first.startswith("ext-") and len(first) == len("ext-") + 12
        one, two = rig.backend.shown
        card = one["payload"]
        assert one["session_id"] == "s1"
        assert (card.agent_path, card.spec_id, card.final) == ("ext:acme.reviewer", first, True)
        assert (card.title, card.source) == ("Indexed", "acme.reviewer")
        assert json.loads(card.spec_json) == SPEC
        assert two["payload"].title == ""
        assert rig.host.state.view_owners.owner_of(first) == "acme.reviewer"

    @pytest.mark.parametrize(
        "view", ["text", None, [], {"elements": {}}, {"root": 3}, {"root": None}]
    )
    async def test_a_view_that_is_not_a_spec_with_a_text_root_is_invalid_and_not_shown(
        self, view: Any
    ) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await ChatViewMembers(rig.host).append(a_call(view=view))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message.startswith("a view is a json-render spec: {root: <id>")
        assert rig.backend.shown == []

    async def test_a_replace_shows_the_same_id_again(self) -> None:
        rig = Rig()
        members = ChatViewMembers(rig.host)
        view_id = await members.append(a_call(view=SPEC, title="A"))
        await members.replace(a_call(view_id=view_id, view=SPEC, title="B"))
        assert [s["payload"].spec_id for s in rig.backend.shown] == [view_id, view_id]
        assert rig.backend.shown[1]["payload"].title == "B"

    async def test_a_replace_of_an_unknown_view_is_not_found(self) -> None:
        rig = Rig()
        with pytest.raises(HostModelError) as caught:
            await ChatViewMembers(rig.host).replace(a_call(view_id="ext-nope", view=SPEC))
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert caught.value.message == "no view 'ext-nope' was appended"

    async def test_a_replace_by_another_extension_is_refused_and_shows_nothing(self) -> None:
        rig = Rig()
        members = ChatViewMembers(rig.host)
        view_id = await members.append(a_call(view=SPEC))
        intruder = a_call(view_id=view_id, view=SPEC).model_copy(
            update={"extension_id": "other.ext"}
        )
        with pytest.raises(HostModelError) as caught:
            await members.replace(intruder)
        assert caught.value.code == ErrorCode.REFUSED
        assert caught.value.message == f"view {view_id!r} belongs to another extension"
        assert len(rig.backend.shown) == 1

    async def test_the_views_listed_are_the_backends_for_the_session(self) -> None:
        rig = Rig()
        assert await ChatViewMembers(rig.host).view_list(a_call()) == ["s1", "a view"]

    async def test_a_view_action_is_sent_with_its_params_and_the_view_id_inside_them(self) -> None:
        rig = Rig()
        rig.gateway.answers[RpcMethod.DISPATCH_VISUALIZATION_ACTION] = {"ok": True}
        got = await ChatViewMembers(rig.host).dispatch(
            a_call(action="click", view_id="ext-1", params={"row": 2})
        )
        assert got == {"ok": True}
        [(method, args, session)] = rig.gateway.calls
        assert method is RpcMethod.DISPATCH_VISUALIZATION_ACTION and session == "s1"
        assert args["action"] == "click"
        assert args["params"] == {"row": 2, "view_id": "ext-1"}

    async def test_a_view_action_without_params_sends_only_the_view_id(self) -> None:
        rig = Rig()
        rig.gateway.answers[RpcMethod.DISPATCH_VISUALIZATION_ACTION] = None
        await ChatViewMembers(rig.host).dispatch(a_call(action="open", view_id="ext-1"))
        assert rig.gateway.calls[0][1]["params"] == {"view_id": "ext-1"}
