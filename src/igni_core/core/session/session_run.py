"""Abstract base class shared by the interactive REPL and single-shot runs.

Both :class:`InteractiveSessionLoop` (in
:mod:`igni_core.core.session.interactive_loop`) and
:class:`SingleMessageRun` (in
:mod:`igni_core.core.session.single_message_run`) subclass
:class:`SessionRun`. The base owns the state and machinery they used
to duplicate:

* :class:`Session` construction (previously done independently in
  ``runner.py`` and ``interactive_loop.py``).
* :meth:`_fire_session_start` / :meth:`_fire_session_end` — the two
  hook-emit sites, backed by
  :class:`SessionLifecyclePayload` (previously three raw
  ``{"session_id": ...}`` dict literals in ``runner.py`` and two
  typed calls in ``interactive_loop.py``).
* :meth:`_run_turn` — the ``@`` mention + :class:`MediaResolver` +
  ``session.handle_message`` + :class:`RunStats` pipeline that was
  byte-for-byte duplicated between the two coordinators. A vision model
  is told which media files it was sent as content; a media file it was
  not sent is described by its metadata (:class:`TurnMediaNotices`), and
  the session's media guard refuses reads of it.

Concrete subclasses define their own ``run(...)`` entry point shape
(``run()`` for the REPL, ``run(message)`` for the single-shot case).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

from igni_core.core.config.settings import Settings
from igni_core.core.hooks.events import HookEvent
from igni_core.core.session.core import Session
from igni_core.core.session.schemas import MessageMedia, SessionLifecyclePayload
from igni_core.core.utils.display_schemas import RunStats
from igni_core.core.utils.media import MediaResolver
from igni_core.core.utils.media_schemas import MediaAttachments
from igni_core.core.utils.mentions import process_file_mentions
from igni_core.core.utils.turn_media import TurnMediaNotices


class SessionRun(ABC):
    """Abstract base holding the machinery shared between the
    interactive REPL loop and the single-shot CLI runner.

    Fields are prefixed with ``_`` because they are protected state
    intended for subclass access only. External callers reach the
    session (when they need it) via the concrete subclass's own
    public methods, not by touching :attr:`_session` directly.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resume_session_id: str | None = None,
        project_dir: Path | None = None,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        self._settings = settings
        self._resume_session_id = resume_session_id
        self._session = Session(
            settings,
            project_dir=project_dir,
            resume_session_id=resume_session_id,
            additional_dirs=additional_dirs,
        )
        #: What a ``SessionStart`` hook said for the agent, until the first turn carries it.
        self._start_context = ""

    # ── Hook emit sites (single source of truth) ──────────────────

    async def _fire_session_start(self) -> None:
        """Emit the ``SessionStart`` hook with a typed payload.

        A hook's ``systemMessage`` is context for the agent (HOOKS.md:
        "context loading", "shown to the agent"); it was discarded, so a
        SessionStart hook could set nothing up for the model (campaign
        R117). It rides on the first turn, as a UserPromptSubmit hook's
        does: a ``<hook-context>`` block after the text.
        """
        result = await self._session.hook_executor.execute(
            event=HookEvent.SESSION_START.value,
            payload=SessionLifecyclePayload(
                session_id=self._session.session_id,
                resumed=bool(self._resume_session_id),
            ),
        )
        message = getattr(result, "message", "")
        if getattr(result, "should_continue", True) is True and isinstance(message, str):
            self._start_context = message

    async def _fire_session_end(self) -> None:
        """Emit the ``SessionEnd`` hook with a typed payload."""
        await self._session.hook_executor.execute(
            event=HookEvent.SESSION_END.value,
            payload=SessionLifecyclePayload(
                session_id=self._session.session_id,
                resumed=bool(self._resume_session_id),
            ),
        )

    # ── Shared turn pipeline ─────────────────────────────────────

    async def _run_turn(self, text: str) -> bool:
        """Resolve file mentions / references, run the model, print
        the response + run stats. ``False`` when the turn failed — the
        failure has been printed as an error, not as a reply.

        Single source of truth for the turn pipeline. Previously
        duplicated verbatim between ``runner.py`` and
        ``interactive_loop.py``.
        """
        session = self._session
        display = session.display
        models = session.settings.models
        vision = models.has_vision(models.default)

        message, mentioned_files = process_file_mentions(text)
        if mentioned_files:
            display.print_info(f"Referenced: {', '.join(mentioned_files)}")

        resolver = MediaResolver(project_dir=session.project_dir)
        resolution = resolver.resolve_text_references(message)
        message, resolved_files = resolution.text, resolution.paths
        if resolved_files:
            display.print_info(f"Resolved: {', '.join(resolved_files)}")

        attachments = self._vision_media(resolver, resolution.attachable)
        named = [resolver.locate(p) or p for p in mentioned_files]
        notices = TurnMediaNotices.collect(
            [*named, *resolved_files, *resolution.attachable],
            attachments.local_paths if attachments is not None else [],
            session.project_dir,
            vision=vision,
        )
        message = notices.wrap(message)
        session.media_guard.begin_turn(notices.unseen.files, notices.unattached.files)
        for line in notices.user_notices(models.default):
            display.print_info(line)

        media: MessageMedia | None = None
        if attachments is not None:
            display.print_info(f"Attached: {len(resolution.attachable)} file(s)")
            media = MessageMedia(
                images=attachments.images or None,
                audio=attachments.audio or None,
                videos=attachments.videos or None,
                files=attachments.files or None,
            )

        context, self._start_context = self._start_context, ""
        # Asked with ``context`` and ``media`` only when there are any: a
        # session double that takes the message alone keeps working.
        extra: dict[str, object] = {}
        if context:
            extra["context"] = context
        if media is not None:
            extra["media"] = media

        start_time = time.monotonic()
        response = await session.handle_message(message, **extra)  # type: ignore[arg-type]
        elapsed = time.monotonic() - start_time
        # A failed turn was printed as an error already; its text is not a
        # reply. Typed check, as in ``_emit_pending_plan``: a loosely held
        # session (a test double) offers a truthy non-string here.
        failure = session.last_turn_failure
        if isinstance(failure, str) and failure:
            display.print_run_stats(
                RunStats(elapsed_seconds=elapsed, model=session.settings.models.default)
            )
            return False
        if response and response.strip():
            display.print_response(response)
        else:
            # A model may return an assistant turn with no content and
            # no tool calls. Captured on the wire against a real
            # provider: `content: ''`, `tool_calls: []`, one round trip,
            # nothing else.
            #
            # `print_response` hands that to `print_markdown`, which
            # renders nothing, so the run printed a timing line and
            # exited 0. To the user that is indistinguishable from a
            # crash, a hang, or a question the agent decided to ignore —
            # and it is the one failure mode with no message anywhere to
            # search for.
            #
            # Said here rather than at the `-m` entry point because this
            # is the shared turn pipeline: the interactive loop renders
            # the same silence.
            display.print_warning(
                "The model returned an empty response — no text and no tool call. "
                "Nothing was run. Try again, or pick another model with /model."
            )
        display.print_run_stats(
            RunStats(
                elapsed_seconds=elapsed,
                model=session.settings.models.default,
            )
        )
        return True

    def _vision_media(self, resolver: MediaResolver, paths: list[str]) -> MediaAttachments | None:
        """The images and files the message names, for a ``vision: true`` model.

        QUICKSTART's File References: a vision model gets them as
        multimodal content, a text-only one the paths to read with its
        tools. Only the app's backend attached them; ``igni -m`` sent a
        vision model the path as text, exactly as a text-only one.
        """
        models = self._session.settings.models
        if not paths or not models.has_vision(models.default):
            return None
        return resolver.attach_local_paths(paths)

    # ── Entry point (subclass-defined shape) ─────────────────────

    @abstractmethod
    async def run(self, *args, **kwargs) -> None:
        """Subclass entry point. Shape differs between the REPL
        (``run()``) and the single-shot case (``run(message)``)."""
