"""The window compaction is measured against is the model's, capped by the setting."""

from __future__ import annotations

from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.settings import Settings
from igni_core.core.session.compaction import ContextBudget


def _settings(*, window: int, cap: int, default: str = "m") -> Settings:
    s = Settings()
    s.models.registry = {default: ModelRegistryEntry(model_id=default, context_window=window)}
    s.models.default = default
    s.models.max_context_window = cap
    return s


def test_the_window_is_the_models_when_the_cap_is_larger() -> None:
    budget = ContextBudget.for_settings(_settings(window=50_000, cap=200_000))

    assert budget.window == 50_000
    assert budget.last_input_tokens == 0


def test_the_cap_wins_when_the_model_window_is_larger() -> None:
    assert ContextBudget.for_settings(_settings(window=1_000_000, cap=128_000)).window == 128_000


def test_a_resize_follows_a_model_switch_and_keeps_the_latched_count() -> None:
    settings = _settings(window=200_000, cap=1_000_000)
    settings.models.registry["small"] = ModelRegistryEntry(model_id="small", context_window=8_000)
    budget = ContextBudget.for_settings(settings)
    budget.latch(1234)

    settings.models.default = "small"
    budget.resize(settings)

    assert budget.window == 8_000
    assert budget.last_input_tokens == 1234


def test_latch_records_the_latest_count_and_zero_resets_it() -> None:
    budget = ContextBudget(window=10)

    budget.latch(500)
    assert budget.last_input_tokens == 500
    budget.latch(0)
    assert budget.last_input_tokens == 0


def test_the_package_exports_what_it_lists() -> None:
    from igni_core.core.session import compaction

    assert sorted(compaction.__all__) == [
        "CompactionCoordinator",
        "ContextBreakdownReporter",
        "ContextBudget",
        "FallbackSummariser",
        "TeamSessionSource",
        "TranscriptMessage",
    ]
    assert all(hasattr(compaction, name) for name in compaction.__all__)
