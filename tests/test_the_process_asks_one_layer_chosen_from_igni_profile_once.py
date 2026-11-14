"""The process's layer is one object, chosen from ``IGNI_PROFILE`` once, and every seam asks it.

``core/layer.py``. Before this the layer's state lived at module level;
now :data:`layers` is one :class:`Layers`, and the core's own seams read
it. Here the real seams are watched changing with the layer, from the
environment variable through the real entry point lookup path.
"""

from __future__ import annotations

import inspect
from importlib import import_module
from types import SimpleNamespace

import pytest

from igni_core.core import layer as layer_module
from igni_core.core.db.locator import StorageLocator
from igni_core.core.layer import BackendHooks, Layer, UnknownLayer, layers


class _NeedsADatabase(Layer):
    name = "shared"

    def requires_database(self) -> bool:
        return True


def _declare_shared(monkeypatch) -> None:
    found = SimpleNamespace(name="shared", value="pkg:Shared", load=lambda: _NeedsADatabase)
    monkeypatch.setattr(layer_module, "entry_points", lambda *, group: [found])


def test_a_profile_from_the_environment_changes_what_the_core_s_seams_answer(monkeypatch, tmp_path):
    _declare_shared(monkeypatch)
    monkeypatch.setenv("IGNI_PROFILE", "shared")
    layers.reset()
    with pytest.raises(RuntimeError, match=r"the shared layer keeps igni's state in a database"):
        StorageLocator(database_url=None, data_dir=tmp_path)


def test_the_desktop_core_opens_its_files_without_a_database_url(tmp_path):
    layers.reset()
    assert StorageLocator(database_url=None, data_dir=tmp_path) is not None


def test_the_environment_is_read_once_so_a_later_change_does_not_switch_layers(
    monkeypatch, tmp_path
):
    _declare_shared(monkeypatch)
    layers.reset()
    StorageLocator(database_url=None, data_dir=tmp_path)  # chosen: the desktop
    monkeypatch.setenv("IGNI_PROFILE", "shared")
    StorageLocator(database_url=None, data_dir=tmp_path)  # still the desktop
    layers.reset()
    with pytest.raises(RuntimeError):
        StorageLocator(database_url=None, data_dir=tmp_path)


def test_a_profile_no_package_declares_stops_the_core_instead_of_running_as_a_desktop(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(layer_module, "entry_points", lambda *, group: [])
    monkeypatch.setenv("IGNI_PROFILE", "typo")
    layers.reset()
    with pytest.raises(UnknownLayer, match="typo"):
        StorageLocator(database_url=None, data_dir=tmp_path)


def test_an_installed_layer_is_what_every_seam_sees(tmp_path):
    layers.install(_NeedsADatabase())
    with pytest.raises(RuntimeError, match="shared layer"):
        StorageLocator(database_url=None, data_dir=tmp_path)


def test_the_backend_hooks_name_the_types_they_are_handed():
    """A layer's author reads these names to know what ``serving`` receives; each is a real class."""
    parameters = inspect.signature(BackendHooks.serving).parameters
    named = {
        "model": ("igni_core.extensions.host.model", "HostModel"),
        "transport": ("igni_core.backend.observed_transport", "ObservedTransport"),
        "orchestrator": ("igni_core.backend.session_orchestrator", "SessionOrchestrator"),
        "notifications": ("igni_core.notifications.center", "NotificationCenter"),
    }
    for parameter, (module, name) in named.items():
        assert parameters[parameter].annotation == name
        assert inspect.isclass(getattr(import_module(module), name))
    assert parameters["on_rebind"].annotation == "Callable[[], None]"
    assert parameters["api_url"].annotation == "Callable[[], str | None]"
    listening = inspect.signature(BackendHooks.listening).parameters["transport"]
    assert listening.annotation == "ObservedTransport"
