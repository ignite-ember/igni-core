"""The settings files a person writes for themselves: YAML and ``settings.json``."""

from __future__ import annotations

from pathlib import Path

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.config_io import YamlSource
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.tiers.base import Tier


class YamlTier(Tier):
    """A YAML file merged into the accumulator. No-op when the file
    doesn't exist or contains a non-dict payload."""

    def __init__(self, path: Path) -> None:
        self._source = YamlSource(path)

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        return accumulator.merge(self._source.load())


class JsonFragmentTier(Tier):
    """A CC-style ``settings.json`` file — only whitelisted top-level
    keys are lifted into the accumulator. Silently no-ops on missing
    / malformed files."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def apply(self, accumulator: SettingsAccumulator) -> SettingsAccumulator:
        result = ManagedPolicySource.load_json_fragment(self._path)
        if result.ok and result.data:
            return accumulator.merge(result.data)
        return accumulator
