"""Typed wire schemas for the backend RPC surface + entry-point boot.

Every wire shape the backend hands out — an RPC response, an
entry-point boot payload, or a config value — is a Pydantic model
here so mypy + Ruff + Pydantic validation give schema coverage at the
seam.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.protocol.json_value import WireJson
from igni_core.protocol.redaction import url_without_credentials

if TYPE_CHECKING:
    from igni_core.core.auth.schemas import UserInfo
    from igni_core.core.config.schemas.models import ModelsConfig
    from igni_core.core.utils.update_checker_schemas import UpdateInfo


# ── Ad-hoc RPC responses ──────────────────────────────────────────


class DirListResult(BaseModel):
    """The sub-folders of one folder, for browsing to a project folder."""

    path: str = Field(description="The folder that was listed, as an absolute path.")
    parent: str = Field(
        description="The folder above `path`; empty when `path` is the filesystem root."
    )
    dirs: list[str] = Field(
        description="Names of the sub-folders in `path`, sorted case-insensitively. "
        "Hidden folders are left out unless they were asked for. Empty when the folder "
        "could not be read."
    )
    home: str = Field(description="The user's home folder, as an absolute path.")
    error: str = Field(description="Why the folder could not be read; empty on success.")


class PickDirResult(BaseModel):
    """The folder the user chose in the operating system's own folder picker."""

    path: str = Field(description="The chosen folder; empty when none was chosen.")
    cancelled: bool = Field(description="`true` when the user closed the picker without choosing.")
    error: str = Field(
        description="Why the picker could not be shown, e.g. no native dialog is "
        "available; empty otherwise."
    )


class RunShellResult(BaseModel):
    """What a ``$`` command printed and how it ended — or, when it outlived
    the wait, that it went on in the background and under which ``pid``,
    the id its ``process_*`` pushes carry."""

    output: str = Field(description="What the command printed, stdout and stderr together.")
    exit_code: int = Field(
        description="The exit code; `0` for a command still running in the background."
    )
    pid: int | None = Field(
        default=None,
        description="The process's id, once it started; `null` when it could not start.",
    )
    backgrounded: bool = Field(
        default=False, description="`true` when the command is still running, in the background."
    )


class UpdateAvailable(BaseModel):
    """A newer igni release that can be installed. When no update is
    available the call returns ``null`` instead."""

    available: bool = Field(description="Always `true`: an update is available.")
    current_version: str = Field(description="The version running now.")
    latest_version: str = Field(description="The newest released version.")
    download_url: str = Field(description="Where the new version can be downloaded.")
    pkg_name: str = Field(description="The name of the package to upgrade.")

    @classmethod
    def from_update_info(cls, info: UpdateInfo, pkg_name: str) -> UpdateAvailable:
        """Pure wire-mapping from an ``UpdateInfo`` domain
        object (network probe result) to the FE-facing payload.

        Stays pure data-shape — the network fetch + package metadata
        lookup live in the RPC handler so this module remains a leaf
        (no dependency on :mod:`core.utils.update_checker`).
        """
        return cls(
            available=True,
            current_version=info.current_version,
            latest_version=info.latest_version,
            download_url=info.download_url,
            pkg_name=pkg_name,
        )


class LoginStarted(BaseModel):
    """Confirms that a sign-in has started. Its progress and outcome
    arrive later as ``login_status`` and ``login_result`` pushes."""

    started: bool = Field(description="`true` when the sign-in started.")
    flow_id: str = Field(
        default="",
        description="This sign-in's id, which its `login_status` and `login_result` pushes carry.",
    )


class LoginResult(BaseModel):
    """Named-field result for :meth:`AuthController.login`.

    Replaces the previous ``tuple[bool, str]`` where the second slot
    overloaded "email on success" / "error message on failure" —
    callers now read :attr:`ok`, :attr:`email`, :attr:`error`
    directly instead of pattern-matching on a positional pair.
    """

    ok: bool
    email: str | None = None
    error: str | None = None

    def wire_result_string(self) -> str:
        """Collapse to the single-string payload the
        ``on_login_result`` push notification expects: email on
        success, error text on failure, empty string when neither
        was recorded. Keeps the "success carries the email, failure
        carries the reason" wire convention on the model rather than
        at every call site."""
        if self.ok:
            return self.email or ""
        return self.error or ""


class CloudPlan(BaseModel):
    """The signed-in account's plan and organisation on igni server.
    Any field may be ``null`` when the server did not report it."""

    # email/full_name ride on this payload because the account popover is
    # the one place that asks the cloud anything, and a second round trip
    # for two strings it already fetched is a second thing to fail.
    tier: str | None = Field(description="The plan tier; `null` when not reported.")
    org_name: str | None = Field(
        description="The organisation's display name; `null` when not reported."
    )
    email: str | None = Field(default=None, description="The signed-in user's email address.")
    full_name: str | None = Field(default=None, description="The signed-in user's full name.")

    @classmethod
    def from_user_info(cls, info: UserInfo) -> CloudPlan:
        """Map a ``UserInfo`` (portal ``/me`` response) into the
        client-facing plan payload."""
        return cls(
            tier=info.tier,
            org_name=info.org_display_name,
            email=info.email or None,
            full_name=info.full_name,
        )


class GroupPolicyPackResult(BaseModel):
    """The policy pack of the group the current user belongs to on igni
    server. Every field is empty when the user belongs to no group."""

    group_id: str | None = Field(
        default=None, description="The group's id; `null` when in no group."
    )
    group_name: str | None = Field(
        default=None, description="The group's name; `null` when in no group."
    )
    fetched_at: str | None = Field(
        default=None,
        description="When the pack was last fetched, as an ISO 8601 timestamp; `null` if never.",
    )
    entry_count: int = Field(default=0, description="How many entries the pack holds.")
    default_model: str | None = Field(
        default=None,
        description="The model this group's members get when nothing names one; "
        "`null` when the group sets none.",
    )


class FileCompletion(BaseModel):
    """Project files matching a partial name, for ``@``-mention completion."""

    matches: list[str] = Field(
        description="Matching file paths, relative to the project folder, best match first; "
        "at most the requested limit."
    )
    total: int = Field(
        description="How many files matched in all; more than the length of `matches` "
        "when the list was cut at the limit."
    )


class SkillDefinition(BaseModel):
    """One skill the agent can use."""

    name: str = Field(description="The skill's name, as typed after `/`.")
    description: str = Field(description="What the skill does, in one line.")
    prompt: str = Field(
        description="The skill's instructions; may be empty for a skill that has only a manifest."
    )


class ModelRegistryEntryView(BaseModel):
    """One configured model as a client sees it: what it is and how it
    runs. Its API key is never included, and its URL has any credentials
    removed."""

    # The full registry entry holds the key itself (api_key), the command
    # that prints it (api_key_cmd), and whatever extra keys the
    # configuration carried; none of that is a client's to read.
    model_id: str = Field(description="The model's id as the provider knows it.")
    provider: str = Field(
        description="The provider that serves the model, e.g. `anthropic` or `openai_like`."
    )
    url: str | None = Field(
        default=None,
        description="The endpoint the model is reached at, without credentials; "
        "`null` for the provider's default.",
    )
    has_api_key: bool = Field(
        description="Whether the entry says where its key comes from (a key, an environment "
        "variable or a command). Never the key itself."
    )
    temperature: float | None = Field(
        default=None, description="Sampling temperature; `null` for the provider's default."
    )
    max_tokens: int | None = Field(
        default=None, description="Most tokens a reply may hold; `null` for the default."
    )
    timeout: int = Field(description="How long a request may take, in seconds.")
    vision: bool = Field(description="Whether the model accepts images.")
    context_window: int | None = Field(
        default=None, description="The model's context window, in tokens; `null` when not set."
    )
    thinking: dict[str, WireJson] | None = Field(
        default=None,
        description="The `thinking` object sent to the model unchanged, e.g. "
        '`{"type": "adaptive"}` (Anthropic models only); `null` when not set.',
    )
    effort: str | None = Field(
        default=None,
        description="The reasoning effort asked of the model, `low` to `max` (Anthropic models "
        "only); `null` when not set.",
    )
    prompt_caching: bool = Field(
        description="Whether the system prompt and tool list are sent as cacheable "
        "(Anthropic models only)."
    )
    source: str = Field(
        default="",
        description="Where the entry came from: `cloud` for igni server's models, "
        "empty for the local configuration's own.",
    )

    @classmethod
    def of(cls, entry: ModelRegistryEntry) -> ModelRegistryEntryView:
        extra = entry.model_extra or {}
        return cls(
            model_id=entry.model_id,
            provider=entry.provider,
            url=url_without_credentials(entry.url) if entry.url else entry.url,
            has_api_key=bool(entry.api_key or entry.api_key_env or entry.api_key_cmd),
            temperature=entry.temperature,
            max_tokens=entry.max_tokens,
            timeout=entry.timeout,
            vision=entry.vision,
            context_window=entry.context_window,
            thinking=entry.thinking,
            effort=entry.effort,
            prompt_caching=entry.prompt_caching,
            source=str(extra.get("source") or ""),
        )


class ModelRegistryResult(BaseModel):
    """The models a user can pick from, and which one is the default."""

    default: str = Field(
        description="The name of the default model, a key of `registry`; empty means the first "
        "entry of `registry` is used."
    )
    max_context_window: int = Field(
        description="The configured overall context-window limit, in tokens; also "
        "the size used while no model is configured."
    )
    registry: dict[str, ModelRegistryEntryView] = Field(
        description="Every configured model, keyed by the name used to pick it."
    )

    @classmethod
    def from_settings(cls, models: ModelsConfig) -> ModelRegistryResult:
        """Map ``settings.models`` into the wire shape, every registry
        row read as a ``ModelRegistryEntryView``."""
        return cls(
            default=models.default,
            max_context_window=models.max_context_window,
            registry={
                name: ModelRegistryEntryView.of(entry) for name, entry in models.registry.items()
            },
        )


# ── Pool-level RPC responses ───────────────────────────────────────


class AttachSessionResult(BaseModel):
    """The session a connection is now attached to."""

    session_id: str = Field(description="The id of the session that was attached.")
    project_dir: str = Field(description="The session's project folder, as an absolute path.")


# ── Boot-time payloads ────────────────────────────────────────────


class BackendReadyLine(BaseModel):
    """Line one of BE stdout: the "I'm ready, connect here" payload
    the parent process parses to learn transport specifics. The boot
    line is stable wire, not a versioned RPC: the shell's parser is
    byte-oriented, so :meth:`emit` keeps ``json.dumps``'s separators
    (``model_dump_json`` is compact and would change them). Pinned by
    ``tests/test_backend_ready_line.py``."""

    status: str = "ready"
    ws_port: int | None = None
    ws_url: str | None = None
    socket: str | None = None

    def emit(self) -> None:
        """Print the line and flush. Keys nobody set are left out, not null."""
        print(json.dumps(self.model_dump(exclude_none=True)), flush=True)


class SessionPoolConfig(BaseModel):
    """Typed config for :class:`~igni_core.backend.session_pool.SessionPool`.

    ``idle_timeout_seconds`` is how long a session nobody has touched
    stays loaded; ``None`` keeps the pool's default."""

    idle_timeout_seconds: float | None = None

    @classmethod
    def from_env(cls) -> SessionPoolConfig:
        """``IGNI_SESSION_IDLE_TIMEOUT``, when it is a positive number of seconds."""
        raw = os.environ.get("IGNI_SESSION_IDLE_TIMEOUT", "")
        try:
            seconds = float(raw) if raw else 0.0
        except ValueError:
            seconds = 0.0
        return cls(idle_timeout_seconds=seconds if seconds > 0 else None)


# ── Lifecycle ─────────────────────────────────────────────────────


class LifecyclePhase(str, Enum):
    """Single-source-of-truth shutdown state for
    :class:`BackendSupervisor`. Replaces the four independent boolean
    flags the old ``_run`` juggled (parent_watch_task cancelled?
    shutdown_close_task cancelled? evictor_task cancelled? pool
    shutdown?)."""

    BOOTING = "booting"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
