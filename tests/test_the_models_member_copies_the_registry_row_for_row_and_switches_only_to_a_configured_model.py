"""``host.models``: the registry an extension may read, and the model it may switch to.

A row is copied field for field from what igni answered, carrying
whether the entry has a key and never the key. A switch goes to a model
the registry has, by the name it has it under, and to no other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from igni_core.backend.schemas_rpc import ModelRegistryEntryView, ModelRegistryResult
from igni_core.extensions.host.members.models import ModelEntry, ModelRegistry
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import HostRig
from tests.scripted_backend import script

ROW: dict[str, Any] = {
    "model_id": "claude-x",
    "provider": "anthropic",
    "url": "https://models.acme.test/v1",
    "has_api_key": True,
    "temperature": 0.25,
    "max_tokens": 4096,
    "timeout": 77,
    "vision": True,
    "context_window": 150_000,
    "thinking": {"type": "adaptive", "budget": 10},
    "effort": "high",
    "prompt_caching": True,
    "source": "cloud",
}


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


class TestTheRowAnExtensionSees:
    def test_every_field_of_the_row_is_copied_as_it_was_answered(self) -> None:
        view = ModelRegistryEntryView.model_validate(ROW)
        entry = ModelEntry.of(view)
        assert entry.model_dump() == ROW

    def test_the_row_has_exactly_the_fields_the_view_has(self) -> None:
        """No field renamed, none added, none left out."""
        assert set(ModelEntry.model_fields) == set(ModelRegistryEntryView.model_fields)

    def test_a_row_with_nothing_set_is_copied_with_its_nulls(self) -> None:
        view = ModelRegistryEntryView(
            model_id="m", provider="p", has_api_key=False, timeout=60, vision=False,
            prompt_caching=False,
        )  # fmt: skip
        entry = ModelEntry.of(view)
        assert entry.url is None
        assert entry.thinking is None
        assert entry.temperature is None
        assert entry.max_tokens is None
        assert entry.context_window is None
        assert entry.effort is None
        assert entry.source == ""
        assert entry.has_api_key is False

    def test_the_registry_is_the_default_the_ceiling_and_each_row_by_name(self) -> None:
        found = ModelRegistryResult.model_validate(
            {
                "default": "big",
                "max_context_window": 200_000,
                "registry": {"big": ROW, "small": ROW | {"model_id": "claude-s", "timeout": 5}},
            }
        )
        registry = ModelRegistry.of(found)
        assert registry.default == "big"
        assert registry.max_context_window == 200_000
        assert list(registry.registry) == ["big", "small"]
        assert registry.registry["small"].model_id == "claude-s"
        assert registry.registry["small"].timeout == 5
        assert registry.registry["big"].model_dump() == ROW

    def test_a_registry_with_no_rows_is_empty(self) -> None:
        found = ModelRegistryResult(default="", max_context_window=0, registry={})
        registry = ModelRegistry.of(found)
        assert registry.registry == {}
        assert registry.default == ""


class TestTheRegistryAsTheMemberAnswersIt:
    async def test_a_key_in_the_answer_never_reaches_the_extension(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        script(
            rig,
            {
                RpcMethod.GET_MODEL_REGISTRY: {
                    "default": "big",
                    "max_context_window": 1000,
                    "registry": {
                        "big": ROW | {"api_key": "sk-leak", "api_key_cmd": "pass show", "x": 1}
                    },
                }
            },
        )
        registry = ok(await rig.ask(ext, "get", "models.registry"))
        assert registry["registry"]["big"] == ROW
        assert "sk-leak" not in str(registry)
        assert "pass show" not in str(registry)

    async def test_no_models_is_an_empty_registry(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        script(
            rig,
            {
                RpcMethod.GET_MODEL_REGISTRY: {
                    "default": "",
                    "max_context_window": 8000,
                    "registry": {},
                }
            },
        )
        assert ok(await rig.ask(ext, "get", "models.registry")) == {
            "default": "",
            "max_context_window": 8000,
            "registry": {},
        }

    async def test_a_registry_that_is_not_one_is_refused(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        script(rig, {RpcMethod.GET_MODEL_REGISTRY: {"default": "x"}})
        message = refused(await rig.ask(ext, "get", "models.registry"), "failed")
        assert "models.registry" in message

    async def test_the_model_in_use_is_the_one_the_status_names(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        script(rig, {RpcMethod.GET_STATUS: {"model": "large"}})
        assert ok(await rig.ask(ext, "get", "models.current")) == "large"


class TestSwitching:
    async def test_the_switch_asks_for_the_model_by_its_registry_name(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        backend = script(rig, {RpcMethod.SWITCH_MODEL: {"type": "info", "text": "ok"}})
        ok(await rig.ask(ext, "set", "models.current", to="large"))
        assert backend.asked(RpcMethod.SWITCH_MODEL) == [{"model_name": "large"}]

    @pytest.mark.parametrize("name", ["", "Large", "gigantic", " small"])
    async def test_a_name_the_registry_does_not_have_is_refused_and_not_asked(
        self, rig: HostRig, name: str
    ) -> None:
        ext = rig.install(host=["models"])
        backend = script(rig, {RpcMethod.SWITCH_MODEL: {"type": "info", "text": "ok"}})
        message = refused(await rig.ask(ext, "set", "models.current", to=name), "refused")
        assert repr(name) in message
        assert backend.asked(RpcMethod.SWITCH_MODEL) == []

    async def test_a_switch_igni_refuses_is_refused_in_its_words(self, rig: HostRig) -> None:
        ext = rig.install(host=["models"])
        script(rig, {RpcMethod.SWITCH_MODEL: {"type": "error", "text": "the model is managed"}})
        message = refused(await rig.ask(ext, "set", "models.current", to="large"), "refused")
        assert message == "the model is managed"
        assert ok(await rig.ask(ext, "get", "models.current")) == "small"
