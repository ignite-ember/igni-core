"""The failure count belongs to each registry, and what it does at its edges.

:class:`~igni_core.extensions.faults.ExtensionFaults`: three failures in a
row make an extension faulty and tell the watchers once; an answer starts
the count again; a registry never counts another's extensions' failures.
"""

from __future__ import annotations

import logging

import pytest

from igni_core.extensions.faults import ExtensionFaults
from igni_core.extensions.registry import ExtensionRegistry, ExtensionState
from tests.host_model_helpers import manifest

LIMIT = ExtensionFaults.LIMIT


def _registry(*ids: str) -> ExtensionRegistry:
    registry = ExtensionRegistry()
    for extension_id in ids:
        registry.install(manifest(extension_id), state=ExtensionState.READY)
    return registry


def _fail(faults: ExtensionFaults, extension_id: str, times: int, what: str = "a tool") -> None:
    for _ in range(times):
        faults.failed(extension_id, what)


def test_two_registries_count_the_same_extension_id_apart():
    first, second = _registry("acme.scan"), _registry("acme.scan")
    assert first.faults is not second.faults
    _fail(first.faults, "acme.scan", LIMIT - 1)
    _fail(second.faults, "acme.scan", 1)
    assert not first.faults.faulty("acme.scan") and not second.faults.faulty("acme.scan")
    first.faults.failed("acme.scan", "a tool")
    assert first.faults.faulty("acme.scan")
    assert not second.faults.faulty("acme.scan")
    assert second.get("acme.scan").faulty == ""


def test_the_reason_names_the_extension_the_count_and_the_last_call():
    registry = _registry("acme.scan")
    _fail(registry.faults, "acme.scan", LIMIT, what="its scan tool")
    assert registry.get("acme.scan").faulty == (
        "acme.scan isn't responding: 3 calls in a row timed out or crashed "
        "(the last, its scan tool). igni is restarting it."
    )


def test_failures_past_the_limit_tell_the_watchers_once_and_an_answer_arms_them_again():
    registry = _registry("acme.scan")
    told: list[str] = []
    registry.faults.on_faulty(lambda extension_id, reason: told.append(extension_id))
    _fail(registry.faults, "acme.scan", LIMIT + 4)
    assert told == ["acme.scan"]

    registry.faults.answered("acme.scan")
    assert not registry.faults.faulty("acme.scan")
    assert registry.get("acme.scan").faulty == ""
    _fail(registry.faults, "acme.scan", LIMIT - 1)
    assert told == ["acme.scan"], "the count began again from nothing"
    _fail(registry.faults, "acme.scan", 1)
    assert told == ["acme.scan", "acme.scan"]


def test_a_watcher_that_raises_does_not_stop_the_others_and_is_logged(caplog):
    registry = _registry("acme.scan")
    told: list[str] = []

    def broken(extension_id: str, reason: str) -> None:
        raise RuntimeError("watcher bug")

    registry.faults.on_faulty(broken)
    registry.faults.on_faulty(lambda extension_id, reason: told.append(reason))
    with caplog.at_level(logging.ERROR):
        _fail(registry.faults, "acme.scan", LIMIT)
    assert len(told) == 1
    assert registry.faults.faulty("acme.scan")
    assert "a faulty-extension watcher failed for acme.scan" in caplog.text


def test_an_answer_with_nothing_wrong_changes_nothing():
    registry = _registry("acme.scan")
    registry.faults.answered("acme.scan")
    registry.faults.answered("never.heard.of")
    assert not registry.faults.faulty("acme.scan")


def test_an_extension_that_is_not_installed_is_never_faulty_and_is_named_by_its_id():
    registry = _registry()
    _fail(registry.faults, "ghost.ext", LIMIT + 1)
    assert not registry.faults.faulty("ghost.ext")
    assert registry.faults.not_responding("ghost.ext") == "ghost.ext isn't responding."


def test_a_refusal_names_the_extension_by_its_display_name():
    registry = ExtensionRegistry()
    shown = manifest("acme.scan").model_copy(update={"name": "Acme Scanner"})
    registry.install(shown, state=ExtensionState.READY)
    assert registry.faults.not_responding("acme.scan") == "Acme Scanner isn't responding."


@pytest.mark.parametrize("failures", range(LIMIT))
def test_fewer_failures_than_the_limit_are_not_faulty(failures):
    registry = _registry("acme.scan")
    _fail(registry.faults, "acme.scan", failures)
    assert not registry.faults.faulty("acme.scan")
