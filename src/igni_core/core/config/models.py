"""Model registry — maps model names to Agno model instances."""

from __future__ import annotations

import logging

from agno.models.base import Model

from igni_core.core.auth.credentials import CloudCredentials
from igni_core.core.auth.http_auth import CloudTokenAuth
from igni_core.core.config.context_window import ContextWindowResolver
from igni_core.core.config.llm_call_logger import LlmCallLogger
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.null_model import NoModelConfigured
from igni_core.core.config.provider_builders import ProviderClientBuilder
from igni_core.core.config.provider_catalog import ProviderCatalog
from igni_core.core.config.settings import Settings

logger = logging.getLogger(__name__)


class ModelRegistry:
    """Registry that maps model names to Agno model instances.

    All models (including Ember defaults) are defined in the config
    registry (``models.registry``). Built-in defaults ship via the
    ``Settings`` Pydantic model (call ``Settings.defaults()``) and
    can be overridden by user/project config files.

    Resolution order:
    1. Config registry (defaults + user overrides).
    2. ``provider:model_id`` format (e.g., ``openai_like:gpt-4o``).

    Instance collaborators:
    * :class:`ProviderCatalog` — polymorphic dispatch to builders.
    * :class:`LlmCallLogger` — dedicated LLM-call log handler.
    * :class:`ContextWindowResolver` — model-size resolution.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._catalog = ProviderCatalog()
        self._llm_logger = LlmCallLogger()

        # Resolve cloud credentials for inference routing. Cached
        # per-registry-instance rather than per-lookup so a single
        # session doesn't re-read the credentials file for every
        # ``get_model`` call.
        self._credentials = CloudCredentials(settings.auth.credentials_file)
        self._cloud_token = self._credentials.access_token
        # What a model client for igni server's gateway sends: the token
        # at each request, which the one above is not for long.
        self._cloud_auth = CloudTokenAuth(settings.auth.credentials_file)
        self._cloud_server_url = settings.api_url if self._cloud_token else None

        # Context-window resolver reuses the registry's already-derived
        # cloud token so ``/models/{id}`` fetches against the Ember
        # gateway don't re-read credentials from disk.
        self.context_windows = ContextWindowResolver(
            cloud_token=self._cloud_token, server_url=settings.api_url
        )

    # ── Public: model resolution ──────────────────────────────────

    def get_model(self, name: str | None = None) -> Model:
        """Get an Agno model instance by registry name.

        Returns whatever concrete :class:`agno.models.base.Model`
        subclass the resolved provider produces —
        :class:`~igni_core.core.config.logging_model.LoggingModel`
        (an ``OpenAILike`` subclass) for OpenAI-compatible providers,
        :class:`agno.models.google.Gemini` for the Gemini path.

        When no model resolves (registry empty AND no default —
        e.g. brand-new install before ``/login``, or a stale token
        that returned no entries from cloud discovery) we return a
        :class:`NoModelConfigured` placeholder so the session can
        still construct. Real invocation raises a clear error; the
        TUI stays reachable so the user can run ``/login``.

        Raises :class:`ValueError` for user-configuration errors
        (unknown model name, unknown provider).
        """
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            self._warn_no_model()
            return NoModelConfigured.for_login_required()

        entry = self._resolve_entry(resolved_name)
        if entry is None:
            if name:
                # Asked for by name: the caller typed it or passed
                # ``--model``, so tell them it does not exist.
                raise ValueError(
                    f"Unknown model {resolved_name!r}. "
                    "Add an entry to `models.registry` or use a "
                    "`provider:model_id` name."
                )
            # Nobody asked for this one — it is the stored default, and
            # it no longer resolves. Raising here killed the backend
            # during ``Session.__init__``, before it could print its
            # ready line: the desktop app then reported "backend exited
            # before signalling ready" with the reason discarded, and
            # the user had no way to reach the UI that would have fixed
            # it.
            #
            # The empty case two lines up was already handled this way.
            # This is the same situation — no usable default — arrived at
            # by a different route, so it gets the same treatment and a
            # message naming the model that went missing.
            self._warn_stale_default(resolved_name)
            return NoModelConfigured.for_stale_default(resolved_name)

        builder = self._catalog.builder_for(entry.provider)
        if builder is None:
            available = ", ".join(sorted(self._catalog.available_providers())) or "(none)"
            raise ValueError(f"Unknown provider {entry.provider!r}. Available: {available}.")

        # igni server's token goes only to igni server (B28). A model entry
        # names its own ``url``, and a repository's ``.igni/config.yaml``
        # can add one, so ``api_key: cloud_token`` with a ``url`` of the
        # repository's choosing handed the signed-in person's token to that
        # host. The token now travels only to the origin of ``api_url``,
        # which only user, group or managed settings may set — whoever
        # wrote the entry, trusted folder or not: no legitimate entry
        # needs igni server's token anywhere else.
        if ModelRegistryEntry.same_origin(builder.endpoint_for(entry), self.settings.api_url):
            cloud_token, cloud_auth = self._cloud_token, self._cloud_auth
        else:
            cloud_token, cloud_auth = None, None
            if self._cloud_token and entry.needs_cloud_token():
                logger.warning(
                    "Model %r asks for the igni server token, but it sends to %s, "
                    "not to your igni server (api_url %s); the token is not sent.",
                    resolved_name,
                    builder.endpoint_for(entry) or f"{entry.provider}'s default endpoint",
                    self.settings.api_url or "not set",
                )
        model = builder.build(
            entry,
            cloud_token=cloud_token,
            llm_logger=self._llm_logger,
            cloud_auth=cloud_auth,
        )
        # Transient failures are retried here, per model call, where Agno
        # asks whether the error is worth retrying; the agents and the team
        # get ``retries=0`` (B31, ``reply_ending.ReplyCutRetries``).
        model.retries = self.settings.models.retries
        return model

    def get_context_window(self, name: str | None = None) -> int:
        """Get the context window size for a model.

        Bootstrap-safe: when no model is configured (registry empty,
        no default) we return the configured ``max_context_window``
        instead of raising. The session must be able to construct
        even when cloud discovery hasn't populated the registry yet
        — otherwise the user can't reach ``/login`` to fix it.
        """
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(resolved_name)
        model_id = entry.model_id if entry else resolved_name
        return self.context_windows.resolve(model_id, entry)

    async def aget_context_window(self, name: str | None = None) -> int:
        """Get the context window size, with async API fallback."""
        resolved_name = name if name else self._effective_default()
        if not resolved_name:
            return self.settings.models.max_context_window
        entry = self._resolve_entry(resolved_name)
        model_id = entry.model_id if entry else resolved_name
        return await self.context_windows.aresolve(model_id, entry)

    # ── Public: catalog / provider registration ───────────────────

    def register_provider(self, name: str, builder: ProviderClientBuilder) -> None:
        """Register a custom provider builder for this registry
        instance. Kept for parity with the old classvar API — now
        delegates to the per-instance catalog so tests get a fresh
        surface per registry rather than mutating a shared singleton.
        """
        self._catalog.register(name, builder)

    # ── Internals ─────────────────────────────────────────────────

    def _warn_no_model(self) -> None:
        logger.warning(
            "No model configured — returning placeholder. "
            "Run /login or add a model to models.registry."
        )

    def _warn_stale_default(self, name: str) -> None:
        logger.warning(
            "Configured default model %r is not in models.registry — "
            "returning placeholder. Run /login to rediscover models, or "
            "pick one with /model.",
            name,
        )

    def _effective_default(self) -> str:
        """Return the active default model name, or ``""`` when none.

        Resolution order:

        1. ``settings.models.default`` if explicitly set (user override,
           ``/model`` switch, or cloud-discovery auto-assign).
        2. First key in ``settings.models.registry`` — works as soon
           as cloud discovery has merged at least one entry.
        3. ``""`` otherwise — bootstrap-safe. :meth:`get_model` maps
           the empty case to :class:`NoModelConfigured` so the session
           can still construct and the user can reach ``/login`` to
           fix the underlying problem (no cloud token, org-membership
           403, network down, etc.).
        """
        explicit = self.settings.models.default
        if explicit:
            return explicit
        if self.settings.models.registry:
            return next(iter(self.settings.models.registry))
        return ""

    def has_usable_model(self) -> bool:
        """Whether a run would reach a real model rather than the
        :class:`NoModelConfigured` placeholder.

        The same question :meth:`get_model` answers by falling through,
        asked without building anything — the status push wants it on
        every poll and constructing a model client for a yes/no would
        be absurd.

        Public because the frontend needs it and the alternative was
        inferring it: the empty state matched ``status.model`` against
        the placeholder's id string, which meant the sentinel was a
        contract between two files that had no idea they shared one.
        """
        return bool(self._effective_default())

    def _resolve_entry(self, name: str) -> ModelRegistryEntry | None:
        """Resolve a model name to a typed registry entry: a
        ``models.registry`` row, else a ``provider:model_id`` name."""
        entry = self.settings.models.entry(name)
        if entry is not None:
            return entry
        if ":" in name:
            provider, model_id = name.split(":", 1)
            return ModelRegistryEntry(provider=provider, model_id=model_id)
        return None
