"""The top-level :class:`Settings` model aggregating every sub-config schema.

The model's ``Field`` defaults are the defaults. Loading (the managed > CLI >
project.local > project > user > defaults merge) is :meth:`SettingsLoader.load`
in :mod:`igni_core.core.config.settings_loader`; the sub-config schemas live in
:mod:`igni_core.core.config.schemas`.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from igni_core.core.air_gap import AIR_GAPPED_ENV, air_gapped_by_environment
from igni_core.core.config.schemas import (
    AgentsConfig,
    AuthConfig,
    ContextConfig,
    DisplayConfig,
    EvalsConfig,
    GroupPolicyConfig,
    GuardrailsConfig,
    HooksConfig,
    LearningConfig,
    MemoryConfig,
    ModelsConfig,
    NetworkConfig,
    OrchestrationConfig,
    PermissionsConfig,
    PluginsConfig,
    ReasoningConfig,
    RulesConfig,
    SafetyConfig,
    SchedulerConfig,
    SkillsConfig,
    StorageConfig,
    TelemetryConfig,
    WorkspaceTrustConfig,
)


class Settings(BaseModel):
    """Complete igni settings."""

    #: The igni server. Deliberately empty (igni is self-hosted):
    #: ``core/config/endpoint.py`` refuses an empty value with the setting
    #: name and an example, and local-only paths skip via ``is_configured``.
    api_url: str = ""
    #: The portal a person signs in on. Empty derives it from ``api_url``;
    #: set it when the two differ by port (API on 7777, portal on 3000).
    portal_url: str = ""
    #: Seconds between update checks; ``0`` disables them. Read by both the
    #: PyPI check and the desktop updater (the Rust side reads this key).
    update_check_ttl: int = 86400
    #: Where the desktop app looks for releases; empty means the endpoint in
    #: ``tauri.conf.json``. Read by the Rust side. HTTPS only; the signing
    #: key is not configurable, so a mirror cannot serve a different build.
    update_endpoint: str = ""
    #: The interpreter the desktop app runs its backend with; empty means the
    #: app's managed venv. ``IGNI_RUNTIME_PYTHON`` wins. Read by the Rust side.
    runtime_python: str = ""
    #: No connection igni starts itself leaves the customer's network: the
    #: update checks, non-own plugin clones, WebFetch/WebSearch (denied
    #: below), the decision model download, and URL-reached extensions, MCP
    #: servers and ``http`` hooks outside ``network``. Shell commands, stdio
    #: MCP servers and ``command`` hooks are bounded by the network itself.
    #: Also set by ``IGNI_AIR_GAPPED=1``, managed settings or group policy.
    air_gapped: bool = False
    #: The hosts inside the customer's network, read only under
    #: ``air_gapped``. A project's config cannot set it (``ProjectYamlTier``).
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    #: The variable a deployment turns air-gapped mode on with. It only
    #: turns it on: an unset or false variable leaves the file's choice.
    AIR_GAPPED_ENV: ClassVar[str] = AIR_GAPPED_ENV
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    orchestration: OrchestrationConfig = Field(default_factory=OrchestrationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    group_policy: GroupPolicyConfig = Field(default_factory=GroupPolicyConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    #: Which folders may run their own code. Managed-only (``WorkspaceTrustTier``).
    workspace_trust: WorkspaceTrustConfig = Field(default_factory=WorkspaceTrustConfig)
    #: OpenTelemetry to the customer's own collector; no default endpoint.
    #: Managed-only (``TelemetryTier``).
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)

    @model_validator(mode="after")
    def _carry_confirm_commands_into_permissions(self) -> Settings:
        """Hand ``safety.require_confirmation`` to the block that is read.

        The permission evaluator reads ``settings.permissions`` alone. Copied
        rather than merged into ``permissions.ask`` so the user's own ask list
        stays as written and the two can be told apart. See
        ``PermissionsConfig._confirmation_ask_rules``.
        """
        self.permissions.confirm_commands = list(self.safety.require_confirmation)
        return self

    @model_validator(mode="after")
    def _air_gap_closes_the_web(self) -> Settings:
        """Apply ``IGNI_AIR_GAPPED`` and, when air-gapped, deny the web tools.

        Denied rather than checked at call time because a denied tool is
        never built: ``ToolRegistry.resolve_typed`` skips it, so no agent
        holds a WebFetch or WebSearch toolkit to call. Set on
        ``permissions`` (not a parallel flag) so every reader that already
        honours ``--no-web`` honours this too, and ``igni config`` shows it.
        """
        if air_gapped_by_environment():
            self.air_gapped = True
        if self.air_gapped:
            self.permissions.web_search = "deny"
            self.permissions.web_fetch = "deny"
        return self

    @classmethod
    def defaults(cls) -> Settings:
        """Return a fresh ``Settings`` populated entirely from the
        model's own ``Field`` defaults. Preferred over
        ``default_dict()`` when the caller wants typed access
        (e.g. ``Settings.defaults().models.registry`` is
        ``dict[str, ModelRegistryEntry]`` rather than string-indexed)."""
        return cls()

    @classmethod
    def default_dict(cls) -> dict[str, Any]:
        """Return the model's defaults as a plain dict. Used by
        :class:`SettingsLoader` to seed its accumulator — the merge
        pipeline runs on dicts, then ``finalize()`` validates the
        result back through ``Settings(**config)``."""
        return cls().model_dump()


__all__ = ["Settings"]
