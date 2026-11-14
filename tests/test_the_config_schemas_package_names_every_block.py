"""``igni_core.core.config.schemas`` is the one import path for every config block."""

from __future__ import annotations

import importlib

from pydantic import BaseModel

from igni_core.core.config import schemas
from igni_core.core.config.settings import Settings
from igni_core.core.workspace_trust.policy import WorkspaceTrustConfig

EXPECTED = {
    "AgentsConfig",
    "AuthConfig",
    "ContextConfig",
    "DisplayConfig",
    "EvalsConfig",
    "GroupPolicyConfig",
    "GuardrailsConfig",
    "HooksConfig",
    "LearningConfig",
    "MemoryConfig",
    "ModelsConfig",
    "NetworkConfig",
    "OrchestrationConfig",
    "OtlpConfig",
    "PermissionsConfig",
    "PluginsConfig",
    "ReasoningConfig",
    "RulesConfig",
    "SafetyConfig",
    "SchedulerConfig",
    "SkillsConfig",
    "StorageConfig",
    "TelemetryConfig",
    "WorkspaceTrustConfig",
}


def test_the_package_exports_exactly_the_config_blocks_and_each_name_resolves():
    assert set(schemas.__all__) == EXPECTED
    assert len(schemas.__all__) == len(set(schemas.__all__))
    for name in schemas.__all__:
        assert issubclass(getattr(schemas, name), BaseModel), name


def test_the_newer_blocks_are_the_classes_their_own_modules_define():
    group = importlib.import_module("igni_core.core.config.schemas.storage_and_paths")
    otlp = importlib.import_module("igni_core.core.config.schemas.telemetry")
    assert schemas.GroupPolicyConfig is group.GroupPolicyConfig
    assert schemas.OtlpConfig is otlp.OtlpConfig
    assert schemas.WorkspaceTrustConfig is WorkspaceTrustConfig


def test_settings_holds_a_block_of_the_exported_class_for_each_field_it_names():
    exported = {getattr(schemas, name) for name in schemas.__all__}
    settings = Settings()
    held = {
        type(value)
        for value in (getattr(settings, field) for field in Settings.model_fields)
        if isinstance(value, BaseModel)
    }
    assert held
    assert held <= exported
