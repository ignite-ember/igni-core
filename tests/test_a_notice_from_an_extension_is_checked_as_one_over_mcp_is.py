"""``host.notifications.notify``: a notice from an extension's process.

:class:`LocalNotices` passes it through the same acceptance as a notice
arriving over MCP (:class:`ExtensionNoticeReceiver`): kept when accepted,
refused with the reason when not, and a failure when this igni keeps no
notifications at all.
"""

from __future__ import annotations

import pytest

from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.local_notices import LocalNotices
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.registry import ExtensionRegistry
from igni_core.notifications.extension_notices import ExtensionNoticeReceiver
from igni_core.notifications.schemas import ExtensionNotice, FloorNotification
from tests.host_model_helpers import manifest


class Sink:
    """Where an accepted notice goes: keeps them, as ``(source, notice)``."""

    def __init__(self) -> None:
        self.kept: list[tuple[str, ExtensionNotice]] = []

    def keep_local(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        self.kept.append((source, notice))
        return FloorNotification.model_construct()


def notice(page: str = "acme.one.home", key: str = "k-1") -> dict[str, object]:
    return {
        "kind": "export.ready",
        "title": "Your export is ready",
        "target": {"page": page},
        "event_key": key,
    }


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def notices(sink: Sink) -> LocalNotices:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.one"))
    return LocalNotices(ExtensionNoticeReceiver(registry=registry, sink=sink))


class TestAccepted:
    def test_a_notice_for_its_own_page_is_kept_under_its_extension(
        self, notices: LocalNotices, sink: Sink
    ) -> None:
        assert notices.notify("acme.one", notice()) is None
        [(source, kept)] = sink.kept
        assert source == "acme.one"
        assert (kept.kind, kept.title, kept.target.page) == (
            "export.ready",
            "Your export is ready",
            "acme.one.home",
        )

    def test_a_notice_that_was_already_kept_is_not_an_error(self, sink: Sink) -> None:
        registry = ExtensionRegistry()
        registry.install(manifest("acme.one"))

        class Seen(Sink):
            def keep_local(self, source: str, notice: ExtensionNotice) -> None:  # type: ignore[override]
                return None

        notices = LocalNotices(ExtensionNoticeReceiver(registry=registry, sink=Seen()))
        assert notices.notify("acme.one", notice()) is None


class TestRefused:
    def test_a_page_the_extension_does_not_declare_is_refused_with_the_reason(
        self, notices: LocalNotices, sink: Sink
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            notices.notify("acme.one", notice(page="acme.other.home"))
        assert caught.value.code is ErrorCode.REFUSED
        assert caught.value.message == (
            "target.page 'acme.other.home' is not one of acme.one's pages (acme.one.home)"
        )
        assert sink.kept == []

    def test_a_malformed_notice_is_refused(self, notices: LocalNotices, sink: Sink) -> None:
        with pytest.raises(HostModelError) as caught:
            notices.notify("acme.one", {"kind": "x"})
        assert caught.value.code is ErrorCode.REFUSED
        assert caught.value.message.startswith("malformed: ")
        assert sink.kept == []

    def test_an_extension_that_is_not_installed_is_refused(
        self, notices: LocalNotices, sink: Sink
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            notices.notify("acme.gone", notice())
        assert caught.value.code is ErrorCode.REFUSED
        assert caught.value.message == "acme.gone is not installed"
        assert sink.kept == []


class TestNoReceiver:
    def test_an_igni_that_keeps_no_notifications_says_so_as_a_failure(self) -> None:
        with pytest.raises(HostModelError) as caught:
            LocalNotices(None).notify("acme.one", notice())
        assert caught.value.code is ErrorCode.FAILED
        assert caught.value.message == "this igni keeps no notifications"
