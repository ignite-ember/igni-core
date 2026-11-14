"""``models``: the registry queries ``/logout`` and the vision filter rely on."""

from __future__ import annotations

from igni_core.core.config.model_entry import CLOUD_TOKEN_SENTINEL, ModelRegistryEntry
from igni_core.core.config.schemas import ModelsConfig


def _entry(**fields: object) -> ModelRegistryEntry:
    return ModelRegistryEntry(model_id="m", **fields)  # type: ignore[arg-type]


def test_a_fresh_models_block_is_auto_with_the_shipped_limits_and_an_empty_registry():
    models = ModelsConfig()
    assert models.default == ""
    assert models.max_context_window == 200_000
    assert models.max_run_timeout == 300
    assert models.retries == 2
    assert models.registry == {}


def test_yaml_style_rows_are_validated_into_typed_entries_and_a_bad_row_is_refused():
    import pytest
    from pydantic import ValidationError

    models = ModelsConfig(registry={"a": {"model_id": "x", "vision": True}})  # type: ignore[dict-item]
    assert isinstance(models.registry["a"], ModelRegistryEntry)
    assert models.registry["a"].vision is True
    with pytest.raises(ValidationError):
        ModelsConfig(registry={"a": {"provider": "no model id"}})  # type: ignore[dict-item]


def test_entry_returns_the_row_or_none():
    row = _entry()
    models = ModelsConfig(registry={"a": row})
    assert models.entry("a") is row
    assert models.entry("missing") is None


def test_vision_is_true_only_for_a_known_entry_flagged_vision():
    models = ModelsConfig(registry={"eyes": _entry(vision=True), "blind": _entry()})
    assert models.has_vision("eyes") is True
    assert models.has_vision("blind") is False
    assert models.has_vision("unknown") is False


def test_the_default_uses_the_cloud_token_only_when_it_names_an_entry_with_the_sentinel():
    cloud = _entry(api_key=CLOUD_TOKEN_SENTINEL)
    inline = _entry(api_key="sk-own")
    registry = {"cloud": cloud, "own": inline}
    assert ModelsConfig(default="cloud", registry=registry).current_uses_cloud_token() is True
    assert ModelsConfig(default="own", registry=registry).current_uses_cloud_token() is False
    assert ModelsConfig(default="", registry=registry).current_uses_cloud_token() is False
    assert ModelsConfig(default="gone", registry=registry).current_uses_cloud_token() is False


def test_the_non_cloud_fallback_is_the_first_entry_with_a_real_key_in_registry_order():
    registry = {
        "cloud": _entry(api_key=CLOUD_TOKEN_SENTINEL),
        "empty": _entry(api_key=""),
        "none": _entry(),
        "first": _entry(api_key="k1"),
        "second": _entry(api_key="k2"),
    }
    assert ModelsConfig(registry=registry).find_non_cloud_fallback() == "first"


def test_there_is_no_fallback_for_an_empty_registry_or_one_of_cloud_and_keyless_entries():
    assert ModelsConfig().find_non_cloud_fallback() is None
    only_cloud = {"c": _entry(api_key=CLOUD_TOKEN_SENTINEL), "k": _entry(api_key="")}
    assert ModelsConfig(registry=only_cloud).find_non_cloud_fallback() is None
