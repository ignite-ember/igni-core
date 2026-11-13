"""The desktop's choices are the layer's defaults, and the process chooses its layer once.

``core/layer.py``. Every seam in the core asks the current layer; a
cloud layer overrides a handful. Here the defaults are pinned one by one,
and the choosing — ``IGNI_PROFILE`` read once, entry points of the
``igni.layers`` group, refusal of anything that is not a layer.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from igni_core.core import layer as layer_module
from igni_core.core.layer import BackendHooks, Layer, Layers, UnknownLayer, layers


class _Cloudy(Layer):
    name = "cloudy"

    def log_files(self) -> bool:
        return False


def _entry_points(monkeypatch, *found: SimpleNamespace) -> list[str]:
    """Replace the entry point lookup; returns the groups it was asked for."""
    asked: list[str] = []

    def lookup(*, group: str):
        asked.append(group)
        return list(found)

    monkeypatch.setattr(layer_module, "entry_points", lookup)
    return asked


def _declares(name: str, factory, value: str = "pkg.mod:Thing") -> SimpleNamespace:
    return SimpleNamespace(name=name, value=value, load=lambda: factory)


class TestTheDesktopDefaults:
    def test_every_seam_answers_with_the_desktops_own_choice(self):
        desktop = Layer()
        assert desktop.name == "desktop"
        assert desktop.log_files() is True
        assert desktop.credentials() is None
        assert desktop.reads_local_settings() is True
        assert desktop.requires_database() is False
        assert desktop.keeps_listing_in_files() is True
        assert desktop.mcp_approvals() is None
        assert desktop.managed_settings_path() is None
        assert desktop.platform_calls_need_control() is False
        assert desktop.connections_speak_for_extensions() is True
        assert desktop.fetches_own_events() is True

    def test_the_desktop_writes_log_files_so_has_no_log_handler(self):
        with pytest.raises(NotImplementedError, match="log files"):
            Layer().log_handler()

    def test_a_layer_overrides_only_what_it_changes(self):
        cloudy = _Cloudy()
        assert cloudy.log_files() is False
        assert cloudy.reads_local_settings() is True

    def test_every_ask_gets_the_do_nothing_backend_hooks(self):
        assert type(Layer().backend()) is BackendHooks


class TestTheDoNothingBackendHooks:
    async def test_the_boot_and_serving_seams_do_nothing_and_bind_nothing(self):
        hooks = BackendHooks()
        assert hooks.tenant is None
        assert hooks.starting() is None
        assert await hooks.listening(object()) is None  # type: ignore[arg-type]
        assert await hooks.bind(object()) is False  # type: ignore[arg-type]
        assert (
            hooks.serving(
                model=object(),  # type: ignore[arg-type]
                transport=object(),  # type: ignore[arg-type]
                orchestrator=object(),  # type: ignore[arg-type]
                notifications=object(),  # type: ignore[arg-type]
                on_rebind=lambda: None,
                api_url=lambda: None,
            )
            is None
        )
        assert await hooks.stop() is None

    def test_serving_takes_its_parts_by_name_only(self):
        with pytest.raises(TypeError):
            BackendHooks().serving(object())  # type: ignore[call-arg,misc]


class TestChoosingTheLayer:
    @pytest.mark.parametrize("profile", [None, "", "   ", "desktop", "DESKTOP", " Desktop "])
    def test_unset_blank_or_desktop_in_any_spelling_is_the_desktop(self, monkeypatch, profile):
        asked = _entry_points(monkeypatch)
        if profile is None:
            monkeypatch.delenv("IGNI_PROFILE", raising=False)
        else:
            monkeypatch.setenv("IGNI_PROFILE", profile)
        chosen = Layers()
        assert type(chosen.current()) is Layer
        assert chosen.requested() == "desktop"
        assert asked == [], "a desktop never looks for another layer's package"

    def test_the_requested_name_is_trimmed_and_lowercased(self, monkeypatch):
        monkeypatch.setenv("IGNI_PROFILE", "  Cloud ")
        assert Layers().requested() == "cloud"

    def test_a_name_picks_the_entry_point_of_that_name_in_the_igni_layers_group(
        self, monkeypatch, caplog
    ):
        asked = _entry_points(monkeypatch, _declares("other", Layer), _declares("cloudy", _Cloudy))
        monkeypatch.setenv("IGNI_PROFILE", " CLOUDY ")
        with caplog.at_level(logging.INFO, logger="igni_core.core.layer"):
            chosen = Layers().current()
        assert type(chosen) is _Cloudy
        assert asked == ["igni.layers"]
        assert "running with the cloudy layer" in caplog.text

    def test_with_two_entry_points_of_one_name_the_first_wins(self, monkeypatch):
        class Second(Layer):
            pass

        _entry_points(monkeypatch, _declares("cloudy", _Cloudy), _declares("cloudy", Second))
        monkeypatch.setenv("IGNI_PROFILE", "cloudy")
        assert type(Layers().current()) is _Cloudy

    def test_a_name_nothing_declares_is_refused_naming_the_variable_and_group(self, monkeypatch):
        _entry_points(monkeypatch, _declares("other", Layer))
        monkeypatch.setenv("IGNI_PROFILE", "nowhere")
        with pytest.raises(UnknownLayer) as refused:
            Layers().current()
        text = str(refused.value)
        assert "IGNI_PROFILE='nowhere'" in text and "igni.layers" in text
        assert isinstance(refused.value, RuntimeError)

    def test_an_entry_point_that_builds_something_else_is_refused(self, monkeypatch):
        _entry_points(monkeypatch, _declares("odd", lambda: object(), "pkg.mod:odd"))
        monkeypatch.setenv("IGNI_PROFILE", "odd")
        with pytest.raises(UnknownLayer, match=r"pkg\.mod:odd is not a Layer"):
            Layers().current()

    def test_an_entry_point_that_cannot_be_loaded_is_not_swallowed(self, monkeypatch):
        def broken() -> type[Layer]:
            raise ImportError("its dependency is missing")

        _entry_points(
            monkeypatch,
            SimpleNamespace(name="broken", value="pkg:Broken", load=broken),
        )
        monkeypatch.setenv("IGNI_PROFILE", "broken")
        with pytest.raises(ImportError, match="dependency"):
            Layers().current()


class TestTheChoiceIsMadeOnce:
    def test_the_environment_is_read_the_first_time_and_not_again(self, monkeypatch):
        _entry_points(monkeypatch, _declares("cloudy", _Cloudy))
        monkeypatch.setenv("IGNI_PROFILE", "cloudy")
        chosen = Layers()
        first = chosen.current()
        monkeypatch.setenv("IGNI_PROFILE", "desktop")
        assert chosen.current() is first

    def test_reset_chooses_again_from_the_environment_as_it_is_now(self, monkeypatch):
        _entry_points(monkeypatch, _declares("cloudy", _Cloudy))
        monkeypatch.setenv("IGNI_PROFILE", "cloudy")
        chosen = Layers()
        assert type(chosen.current()) is _Cloudy
        monkeypatch.setenv("IGNI_PROFILE", "desktop")
        chosen.reset()
        assert type(chosen.current()) is Layer

    def test_install_replaces_whatever_was_chosen_and_survives_the_environment(self, monkeypatch):
        monkeypatch.setenv("IGNI_PROFILE", "nowhere")
        chosen = Layers()
        mine = _Cloudy()
        chosen.install(mine)
        assert chosen.current() is mine

    def test_concurrent_first_asks_load_the_layer_once_and_agree(self, monkeypatch):
        loads: list[int] = []

        def slow() -> type[Layer]:
            loads.append(1)
            time.sleep(0.2)
            return _Cloudy

        _entry_points(monkeypatch, SimpleNamespace(name="cloudy", value="p:C", load=slow))
        monkeypatch.setenv("IGNI_PROFILE", "cloudy")
        chosen = Layers()
        seen: list[Layer] = []
        threads = [threading.Thread(target=lambda: seen.append(chosen.current())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(loads) == 1
        assert len(seen) == 8 and all(layer is seen[0] for layer in seen)

    def test_the_process_has_one_layers_that_every_seam_asks(self):
        assert isinstance(layers, Layers)
        assert layers.current() is layers.current()
