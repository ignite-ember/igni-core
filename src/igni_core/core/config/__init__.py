"""Configuration management for igni."""

from igni_core.core.config.cloud_model_migrator import CloudModelMigrator
from igni_core.core.config.managed_policy import ManagedPolicySource
from igni_core.core.config.settings import Settings
from igni_core.core.config.settings_loader import SettingsLoader
from igni_core.core.config.user_config_store import UserConfigStore

__all__ = [
    "CloudModelMigrator",
    "ManagedPolicySource",
    "ModelRegistry",
    "Settings",
    "SettingsLoader",
    "UserConfigStore",
]


def __getattr__(name: str):
    if name == "ModelRegistry":
        from igni_core.core.config.models import ModelRegistry

        return ModelRegistry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
