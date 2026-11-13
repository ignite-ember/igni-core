"""Which notices an extension's connection may post, and the one the host posts for it.

The receiver names the extension by its connection, refuses what is
malformed, over-long or about a page that is not the extension's, and
says so in the log without ending the connection. The fault notice
says an extension stopped responding, bounded, once per fault.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from igni_core.extensions.install_service import SERVER_PREFIX, server_name
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from igni_core.notifications.extension_notices import (
    ExtensionNoticeReceiver,
    FaultNotices,
    NoticeVerdict,
)
from igni_core.notifications.schemas import ExtensionNotice, FloorNotification


def manifest(extension_id: str, *pages: str, name: str | None = None) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": name or extension_id,
            "trust": "unsigned",
            "dev": {"command": ["python", "server.py"]},
            "contributes": {
                "pages": [{"kind": page, "label": page, "sidebar": True} for page in pages]
            },
        }
    )


class Sink:
    def __init__(self) -> None:
        self.kept: list[tuple[str, ExtensionNotice]] = []
        self.seen: set[tuple[str, str]] = set()

    def keep_local(self, source: str, notice: ExtensionNotice) -> FloorNotification | None:
        if (source, notice.event_key) in self.seen:
            return None
        self.seen.add((source, notice.event_key))
        self.kept.append((source, notice))
        return FloorNotification(id="local-1", origin="local", source=source)  # type: ignore[arg-type]


@pytest.fixture
def registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.install(manifest("acme.scanner", "acme.scan"), state=ExtensionState.READY)
    registry.install(manifest("acme.bare"), state=ExtensionState.READY)
    return registry


def params(**changes: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "export_ready",
        "title": "Runbook.pdf",
        "target": {"page": "acme.scan"},
        "event_key": "e1",
    }
    result.update(changes)
    return result


# ── Whose connection it is ──────────────────────────────────────────


def test_a_connection_is_an_extension_only_by_its_name():
    assert ExtensionNoticeReceiver.extension_for(server_name("acme.scanner")) == "acme.scanner"
    assert ExtensionNoticeReceiver.extension_for("github") is None
    assert ExtensionNoticeReceiver.extension_for(SERVER_PREFIX) is None
    assert ExtensionNoticeReceiver.extension_for("") is None


# ── What it refuses, and what it says ───────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        params(title="x" * 201),
        params(kind="x" * 65),
        params(event_key="x" * 201),
        params(body="x" * 1001),
        params(title=""),
        {"title": "no target"},
        None,
        "a string",
        ["a", "list"],
    ],
)
def test_what_is_malformed_or_over_long_is_refused_and_kept_nowhere(registry, bad):
    sink = Sink()

    outcome = ExtensionNoticeReceiver(registry=registry, sink=sink).receive("acme.scanner", bad)

    assert outcome.verdict is NoticeVerdict.REFUSED
    assert outcome.reason.startswith("malformed")
    assert outcome.notification is None
    assert sink.kept == []


def test_the_refusal_says_which_pages_the_extension_has(registry):
    receiver = ExtensionNoticeReceiver(registry=registry, sink=Sink())

    elsewhere = receiver.receive("acme.scanner", params(target={"page": "acme.other"}))
    none = receiver.receive("acme.bare", params(target={"page": "acme.scan"}))

    assert "acme.other" in elsewhere.reason
    assert "acme.scan" in elsewhere.reason
    assert "it declares none" in none.reason


def test_an_uninstalled_extension_is_named_in_the_refusal(registry):
    outcome = ExtensionNoticeReceiver(registry=registry, sink=Sink()).receive("acme.gone", params())

    assert outcome.verdict is NoticeVerdict.REFUSED
    assert outcome.reason == "acme.gone is not installed"


def test_a_repeat_is_a_duplicate_with_nothing_attached(registry):
    receiver = ExtensionNoticeReceiver(registry=registry, sink=Sink())

    first = receiver.receive("acme.scanner", params())
    again = receiver.receive("acme.scanner", params())

    assert first.verdict is NoticeVerdict.STORED
    assert first.notification is not None
    assert again.verdict is NoticeVerdict.DUPLICATE
    assert again.notification is None


def test_a_refusal_is_logged_and_never_raised(registry, caplog):
    sink = Sink()
    receiver = ExtensionNoticeReceiver(registry=registry, sink=sink)

    with caplog.at_level(logging.WARNING):
        receiver(server_name("acme.scanner"), params(target={"page": "acme.other"}))  # type: ignore[arg-type]
        receiver(server_name("acme.scanner"), None)  # type: ignore[arg-type]
        receiver("github", params())

    assert sink.kept == []
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("acme.scanner refused" in m and "acme.other" in m for m in messages)
    assert any("github, which is not an extension: dropped" in m for m in messages)


def test_a_stored_notice_is_not_logged_as_a_warning(registry, caplog):
    sink = Sink()

    with caplog.at_level(logging.WARNING):
        ExtensionNoticeReceiver(registry=registry, sink=sink)(server_name("acme.scanner"), params())

    assert [source for source, _ in sink.kept] == ["acme.scanner"]
    assert caplog.records == []


# ── An extension that stops responding ──────────────────────────────


def test_a_fault_notice_is_cut_to_the_bounds_a_notice_may_have(registry):
    registry.install(
        manifest("acme.verbose", "acme.verbose.home", "acme.verbose.other", name="N" * 500),
        state=ExtensionState.READY,
    )
    sink = Sink()

    FaultNotices(registry, sink).told("acme.verbose", "why " * 1000)

    [(source, notice)] = sink.kept
    assert source == "acme.verbose"
    assert notice.title == ("N" * 500 + " isn't responding")[:200]
    assert len(notice.body) == 1000
    assert notice.kind == FaultNotices.KIND
    assert notice.target.page == "acme.verbose.home"


def test_an_extension_with_no_page_or_not_installed_is_not_told_about(registry, caplog):
    sink = Sink()

    with caplog.at_level(logging.INFO):
        FaultNotices(registry, sink).told("acme.bare", "dead")
        FaultNotices(registry, sink).told("acme.gone", "dead")

    assert sink.kept == []
    assert sum("no page to point a notice at" in r.getMessage() for r in caplog.records) == 2


def test_two_faults_in_one_instant_are_two_notices(registry, monkeypatch):
    monkeypatch.setattr(time, "time_ns", lambda: 1_000)
    sink = Sink()
    faults = FaultNotices(registry, sink)

    faults.told("acme.scanner", "first")
    faults.told("acme.scanner", "second")

    assert [notice.body for _, notice in sink.kept] == ["first", "second"]
    assert len({notice.event_key for _, notice in sink.kept}) == 2


def test_a_fault_notice_names_what_it_is_and_always_fits_a_notice(registry):
    sink = Sink()

    FaultNotices(registry, sink).told("acme.scanner", "")

    [(_, notice)] = sink.kept
    assert notice.event_key.startswith(f"{FaultNotices.KIND}:")
    assert notice.body == ""
    assert notice.title == "acme.scanner isn't responding"
