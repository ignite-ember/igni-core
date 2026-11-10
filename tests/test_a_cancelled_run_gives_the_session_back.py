"""Two ways a run's bookkeeping outlived the run itself.

* **Cancel never ended the run.** ``RunController._run_locked`` sets
  ``RunPhase.streaming`` before ``team.arun`` and only leaves it on one of
  four paths — hook-blocked, stream-failure, natural end, ``except
  Exception``. A user cancel takes none of them: ``asyncio.CancelledError``
  is a ``BaseException``, so ``except Exception`` does not fire, and
  ``_run_locked``'s ``finally`` only tears down the checkpoint task and the
  httpx client. The phase stayed ``streaming`` for the life of the process.
  Everything that reads it then lies: ``is_processing()`` (the FE's
  ``get_processing`` RPC) reports the session permanently busy, and the
  session pool's idle sweep can never reclaim the runtime — so the cost of
  pressing Escape was a session that never came back.

* **A pasted attachment lost to a filename.** ``PromptBuilder``'s vision
  branch returned the filename-resolved media bundle in place of the one the
  caller passed in, rather than merging as the URL branch one method below
  does. No shipping client populates the incoming bundle today, so nothing is
  lost yet; the first FE that wires up paste-an-image would have lost it on
  any message that also named a resolvable file.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace

from agno.media import Image

from igni_core.backend.prompt_builder import PromptBuilder
from igni_core.backend.run_controller import RunController
from igni_core.backend.schemas_run import (
    HookGateResult,
    ModelConfig,
    PromptBuildResult,
    RunPhase,
)
from igni_core.core.config.group_policy.gate import RunGate
from igni_core.core.utils.media_schemas import MediaAttachments, ResolvedText
from igni_core.protocol import messages as msg

# ── Fakes ────────────────────────────────────────────────────────────


class _Journal:
    async def drain_queued_drops(self) -> None:
        return None

    async def record(self, text: str) -> str:
        return "pending-1"

    async def mark_completed(self, pending_id: str) -> None:
        return None

    async def mark_interrupted(
        self, pending_id: str, reason: str, last_error: str | None = None
    ) -> None:
        return None


class _HookGate:
    async def fire_user_prompt_submit(self, text: str) -> HookGateResult:
        return HookGateResult(should_continue=True)

    async def fire_stop(self) -> HookGateResult:
        return HookGateResult(should_continue=True)


class _Dispatcher:
    async def handle(self, proto, team) -> None:
        return None


class _PromptBuilderStub:
    async def build(self, text, media, *, interrupted_summary):
        return PromptBuildResult(message=text, media=None, info_messages=[])


class _FakeSupervisor:
    def cancel_foreground(self) -> bool:
        return False


async def _no_prefix(text: str) -> str:
    return ""


async def _forever(team, *args, **kwargs) -> None:
    await asyncio.sleep(3600)


async def _noop_async(team) -> None:
    return None


def _controller(stream_messages: list[msg.Message] | None) -> RunController:
    """A ``RunController`` on fakes. ``stream_messages=None`` means a stream
    that blocks forever — the state a cancel actually arrives in.

    Built via ``__new__``, the same bypass the crash-survival tests use: a
    real one drags in a Session, an Agno team and a database.
    """

    async def _stream(_team_stream):
        if stream_messages is None:
            await asyncio.sleep(3600)
            return
        for message in stream_messages:
            yield message

    controller = RunController.__new__(RunController)
    controller._phase = RunPhase.idle
    controller._interrupted_summary = None
    controller._pending_id_for_cancel = None
    controller._current_run_task = None
    controller._run_lock = asyncio.Lock()
    controller._pending_journal = _Journal()
    controller._hook_gate = _HookGate()
    controller._dispatcher = _Dispatcher()
    controller._prompt_builder = _PromptBuilderStub()
    controller._session = SimpleNamespace(
        main_team=SimpleNamespace(arun=lambda *a, **k: None, run_id=None),
        guardrail_runner=SimpleNamespace(warning_prefix=_no_prefix),
        memory_mirror=None,
    )
    controller._stream_factory = _stream
    controller._message_queue = None
    controller.periodic_checkpoint = _forever  # type: ignore[method-assign]
    controller._http_client_manager = SimpleNamespace(close_and_replace=_noop_async)
    # The supervisor the controller cancels the foreground slot of.
    # A fake one keeps the test off the real registry.
    controller._supervisor = _FakeSupervisor()
    # The run gate of the backend process — open, so the run starts.
    controller._gate = RunGate()
    return controller


async def _wait_for_streaming(controller: RunController) -> None:
    """Wait until the controller is streaming — or fail instead of hanging.

    A controller that never reaches the streaming phase is a broken
    controller, and a bare spin loop would turn that into a hung test
    rather than a failed one.
    """

    async def _until_streaming() -> None:
        while controller.phase is not RunPhase.streaming:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_until_streaming(), timeout=5)


async def _drain(controller: RunController) -> list[msg.Message]:
    return [proto async for proto in controller.run_message("hello", None)]


# ── Cancel must end the run ──────────────────────────────────────────


class TestACancelledRunIsOver:
    async def test_the_phase_leaves_streaming(self):
        """Without the terminal transition this stays ``streaming`` forever."""
        controller = _controller(None)
        collected: list[msg.Message] = []

        async def _consume() -> None:
            async for proto in controller.run_message("hello", None):
                collected.append(proto)

        task = asyncio.create_task(_consume())
        await _wait_for_streaming(controller)

        controller.cancel_run()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert controller.phase is not RunPhase.streaming
        assert controller.is_processing() is False, (
            "a cancelled session still reports itself busy — the FE keeps the "
            "composer locked and SessionPool.evict_idle can never reclaim it"
        )

    async def test_the_run_task_handle_is_released(self):
        controller = _controller(None)

        async def _consume() -> None:
            async for _ in controller.run_message("hello", None):
                pass

        task = asyncio.create_task(_consume())
        await _wait_for_streaming(controller)

        controller.cancel_run()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert controller.current_run_task is None

    async def test_the_lock_is_free_for_the_next_submit(self):
        """The point of the fix: the session can be used again."""
        controller = _controller(None)

        async def _consume() -> None:
            async for _ in controller.run_message("hello", None):
                pass

        task = asyncio.create_task(_consume())
        await _wait_for_streaming(controller)

        controller.cancel_run()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert not controller.run_lock.locked()


class TestTheTerminalPhasesStillStand:
    """The cancel mapping must not overwrite a phase a real path set."""

    async def test_a_clean_run_ends_done(self):
        controller = _controller([msg.StreamingDone(run_id="r1")])

        await _drain(controller)

        assert controller.phase is RunPhase.done

    async def test_a_failed_run_stays_errored(self):
        controller = _controller([msg.Error(text="model connection reset")])

        await _drain(controller)

        assert controller.phase is RunPhase.errored, (
            "the cancel-path mapping swallowed the failure verdict — 'errored' "
            "is what tells the FE to offer Retry/Discard"
        )


# ── The FE's own attachments survive a filename resolve ──────────────


class _Resolver:
    """Resolves one bare filename to one image, like the real one."""

    def __init__(self, attached: MediaAttachments | None) -> None:
        self._attached = attached

    def resolve_text_references(self, text: str) -> ResolvedText:
        return ResolvedText(
            text=text, paths=["/project/diagram.png"], attachable=["/project/diagram.png"]
        )

    def attach_local_paths(self, paths: list[str]) -> MediaAttachments | None:
        return self._attached


def _builder(attached: MediaAttachments | None) -> PromptBuilder:
    builder = PromptBuilder.__new__(PromptBuilder)
    builder._resolver = _Resolver(attached)
    return builder


def _image(path: str) -> MediaAttachments:
    """A bundle holding one image at ``path`` — the shape the FE's paste
    and the resolver's attach both produce."""
    return MediaAttachments(images=[Image(filepath=Path(path))])


class TestSuppliedAttachmentsAreKept:
    async def test_the_pasted_image_is_not_replaced_by_the_resolved_one(self):
        """Both reach the model — the branch used to return only the resolved
        bundle, dropping whatever the FE sent with the message."""
        builder = _builder(_image("/project/resolved-diagram.png"))
        supplied = _image("/project/pasted-screenshot.png")

        step = builder._attach_file_references(
            "see diagram.png", supplied, ModelConfig(vision=True)
        )

        assert step.media is not None
        assert [i.filepath for i in step.media.images] == [
            Path("/project/pasted-screenshot.png"),
            Path("/project/resolved-diagram.png"),
        ]

    async def test_a_message_with_no_supplied_media_is_unchanged(self):
        builder = _builder(_image("/project/resolved-diagram.png"))

        step = builder._attach_file_references("see diagram.png", None, ModelConfig(vision=True))

        assert step.media is not None
        assert [i.filepath for i in step.media.images] == [Path("/project/resolved-diagram.png")]

    async def test_nothing_attachable_leaves_the_supplied_bundle_alone(self):
        """``attach_local_paths`` returning ``None`` is the text-only path."""
        builder = _builder(None)
        supplied = _image("/project/pasted-screenshot.png")

        step = builder._attach_file_references("see notes.txt", supplied, ModelConfig(vision=True))

        assert step.media is supplied
