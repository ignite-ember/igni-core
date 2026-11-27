"""Every way a turn starts tells the model what it can see of the media files, and guards the rest.

Three paths build the ``<attached-files>`` block: the app's backend
(``PromptBuilder``), ``igni -m`` and pipe mode (both ``SingleMessageRun``,
through the turn pipeline the interactive loop shares). Each is run here
with the same message — a Markdown file, a PNG and an MP4 — for a model
without vision and for one with it, and each is checked for the block the
model was sent, the line the person was shown, and the session's guard.

A vision model is told the media files it was sent are attached and
visible, never to read them. A media file it was not sent — outside the
allowed roots, or one the resolver attached nothing for — is described
by its metadata under a header saying it could not be attached, never
that vision is disabled.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from igni_core.backend.prompt_builder import PromptBuilder
from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.options import CliOptions
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.hooks.text_only_media_guard import TextOnlyMediaGuard
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.utils.media import MediaResolver
from tests.session_factory import STUB_MODEL, build_session

PNG = (Path(__file__).parent / "fixtures" / "media" / "seven-by-three.png").read_bytes()
MESSAGE = "compare @shot.png @clip.mp4 @notes.md"
HEADER = (
    "[Vision is disabled for this model. Do not open, read, decode, OCR or otherwise "
    "extract the content of these media files; only their metadata is available. "
    "Tell the user you cannot see them and suggest switching to a vision-enabled model.]"
)
ATTACHED = (
    "[These media files are attached to this message and already visible to you. "
    "Do not open, read, decode, OCR or otherwise extract their content with tools.]"
)
UNATTACHED = (
    "[These media files could not be attached, so you cannot see them. Do not open, read, "
    "decode, OCR or otherwise extract their content; only their metadata is available. "
    "Tell the user they were not attached.]"
)
REFUSAL = "Not run: vision is disabled for this model; only the metadata below is available."


def _files(root: Path) -> tuple[str, str]:
    """Write the three files; the PNG's and the MP4's resolved paths."""
    (root / "notes.md").write_text("# notes\n")
    (root / "shot.png").write_bytes(PNG)
    (root / "clip.mp4").write_bytes(b"\x00" * 6)
    return str((root / "shot.png").resolve()), str((root / "clip.mp4").resolve())


def _text_only_block(shot: str, clip: str) -> str:
    """The block a text-only model is sent for :data:`MESSAGE`, body included."""
    return (
        "<attached-files>\n"
        "[Referenced files: notes.md — read before responding]\n"
        f"{HEADER}\n"
        f"- shot.png (path: {shot}, type: image/png, size: 74 bytes, dimensions: 7x3 px)\n"
        f"- clip.mp4 (path: {clip}, type: video/mp4, size: 6 bytes)\n"
        "</attached-files>\n"
        f"compare @{shot} @{clip} @notes.md"
    )


def _vision_block(shot: str, clip: str) -> str:
    """The block a vision model is sent for :data:`MESSAGE`: the media attached, the Markdown to read."""
    return (
        "<attached-files>\n"
        "[Referenced files: notes.md — read before responding]\n"
        f"{ATTACHED}\n"
        f"- shot.png (path: {shot}, type: image/png, size: 74 bytes, dimensions: 7x3 px)\n"
        f"- clip.mp4 (path: {clip}, type: video/mp4, size: 6 bytes)\n"
        "</attached-files>\n"
        f"compare @{shot} @{clip} @notes.md"
    )


_UNATTACHED_LINE = "{names} could not be attached, so only their metadata was sent."


def _flat(out: str) -> str:
    """Printed output with the console's line wrapping undone."""
    return out.replace("\n", " ").replace("  ", " ")


def _notice(model: str) -> str:
    return (
        f"Vision is disabled for {model}: only the metadata of shot.png, clip.mp4 was sent. "
        "Switch to a vision-enabled model to let it see the content."
    )


# ── the app's backend ─────────────────────────────────────────────


class _BackendSession:
    def __init__(self, root: Path, *, vision: bool) -> None:
        self.project_dir = root
        self.settings = SimpleNamespace(
            storage=SimpleNamespace(database_url=None, data_dir=root),
            models=ModelsConfig(
                default="minimax",
                registry={"minimax": ModelRegistryEntry(model_id="m", vision=vision)},
            ),
        )
        self.reminders = PendingReminderQueue()
        self.learning_mgr = SimpleNamespace(inject=self._inject)
        self.media_guard = TextOnlyMediaGuard()

    async def _inject(self) -> None:
        return None


def _body(message: str) -> str:
    """The message without its ``<system-context>`` header line."""
    header, body = message.split("\n", 1)
    assert header.startswith("<system-context>")
    return body


async def test_the_backend_sends_a_text_only_model_the_metadata(tmp_path: Path):
    shot, clip = _files(tmp_path)
    session = _BackendSession(tmp_path, vision=False)

    result = await PromptBuilder(session).build(MESSAGE, None, interrupted_summary=None)  # type: ignore[arg-type]

    assert _body(result.message) == _text_only_block(shot, clip)
    assert result.media is None
    assert [i.text for i in result.info_messages] == [
        "Referenced: shot.png, clip.mp4, notes.md",
        f"Resolved: {shot}, {clip}",
        _notice("minimax"),
    ]
    assert [f.path for f in session.media_guard.files] == [shot, clip]


async def test_the_backend_sends_a_vision_model_the_media_and_guards_nothing(tmp_path: Path):
    shot, clip = _files(tmp_path)
    session = _BackendSession(tmp_path, vision=True)
    session.media_guard.begin_turn([object()])  # type: ignore[list-item]

    result = await PromptBuilder(session).build(MESSAGE, None, interrupted_summary=None)  # type: ignore[arg-type]

    assert _body(result.message) == _vision_block(shot, clip)
    assert result.media is not None
    assert [Path(str(i.filepath)).name for i in result.media.images] == ["shot.png"]
    assert [Path(str(v.filepath)).name for v in result.media.videos] == ["clip.mp4"]
    assert not any(i.text.startswith("Vision is disabled") for i in result.info_messages)
    assert session.media_guard.files == [], "the last turn's files are not guarded any more"


async def test_the_backend_leaves_a_message_without_media_as_it_was(tmp_path: Path):
    (tmp_path / "notes.md").write_text("x")
    session = _BackendSession(tmp_path, vision=False)

    result = await PromptBuilder(session).build("read @notes.md", None, interrupted_summary=None)  # type: ignore[arg-type]

    assert _body(result.message) == (
        "<attached-files>\n[Referenced files: notes.md — read before responding]\n"
        "</attached-files>\nread @notes.md"
    )
    assert [i.text for i in result.info_messages] == ["Referenced: notes.md"]
    assert session.media_guard.files == []


async def test_the_backend_describes_a_bare_media_name_it_resolved(tmp_path: Path):
    shot, _ = _files(tmp_path)
    session = _BackendSession(tmp_path, vision=False)

    result = await PromptBuilder(session).build(
        "what is in shot.png", None, interrupted_summary=None
    )  # type: ignore[arg-type]

    assert _body(result.message) == (
        f"<attached-files>\n{HEADER}\n"
        f"- shot.png (path: {shot}, type: image/png, size: 74 bytes, dimensions: 7x3 px)\n"
        f"</attached-files>\nwhat is in {shot}"
    )
    assert [f.path for f in session.media_guard.files] == [shot]


# ── igni -m and pipe mode ─────────────────────────────────────────


class _Headless(HeadlessRun):
    """The real headless runner, without the hard exit past lingering threads."""

    @staticmethod
    def _lingering_threads() -> list[Any]:
        return []


class _Extensions:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture
def cli_session(tmp_path: Path):
    """A real session on a throwaway project whose turn records what the model was sent.

    The turn also makes the call a looping model would — ``cat`` on the
    image through the session's own tool hook — and keeps the answer.
    """
    session = build_session(tmp_path)
    seen: dict[str, Any] = {}

    async def handle_message(message: str, **kwargs: Any) -> str:
        seen["message"], seen["kwargs"] = message, kwargs
        seen["cat"] = await session.tool_hook_factory.create()(
            name="run_shell_command", func=lambda **_: "PNG bytes", args={"command": "cat shot.png"}
        )
        return "I cannot see the image."

    session.handle_message = handle_message  # type: ignore[method-assign]
    session.seen = seen  # type: ignore[attr-defined]
    return session


def _use_vision(session: Any, vision: bool) -> None:
    registry = {"stub": {**STUB_MODEL["registry"]["stub"], "vision": vision}}
    session.settings.models = ModelsConfig(default="stub", registry=registry)


def _run_cli(session: Any, run: Any) -> None:
    with (
        patch("igni_core.core.session.session_run.Session", return_value=session),
        patch(
            "igni_core.core.session.single_message_run.HeadlessExtensions",
            lambda _session: _Extensions(),
        ),
    ):
        run()


class _Stdin(io.StringIO):
    def isatty(self) -> bool:
        return False


def _dash_m(session: Any, message: str = MESSAGE) -> None:
    invocation = CliInvocation(CliOptions(message=message), Settings(), _Headless())
    _run_cli(session, lambda: invocation.run_message(message))


def _pipe(session: Any, message: str = MESSAGE) -> None:
    invocation = CliInvocation(CliOptions(pipe=True), Settings(), _Headless())
    with patch.object(sys, "stdin", _Stdin(message + "\n")):
        _run_cli(session, invocation.run_pipe)


ENTRY_POINTS = [pytest.param(_dash_m, id="igni -m"), pytest.param(_pipe, id="pipe mode")]


@pytest.mark.parametrize("enter", ENTRY_POINTS)
def test_a_cli_turn_sends_a_text_only_model_the_metadata_and_refuses_its_cat(
    cli_session, capsys, enter
):
    _use_vision(cli_session, False)
    shot, clip = _files(cli_session.project_dir)

    enter(cli_session)

    seen = cli_session.seen
    assert seen["message"] == _text_only_block(shot, clip)
    assert "media" not in seen["kwargs"]
    assert seen["cat"] == (
        f"{REFUSAL}\n- shot.png (path: {shot}, type: image/png, size: 74 bytes, dimensions: 7x3 px)"
    )
    assert _notice("stub") in capsys.readouterr().out.replace("\n", " ").replace("  ", " ")


@pytest.mark.parametrize("enter", ENTRY_POINTS)
def test_a_cli_turn_sends_a_vision_model_the_media_and_lets_its_cat_run(cli_session, capsys, enter):
    _use_vision(cli_session, True)
    shot, clip = _files(cli_session.project_dir)

    enter(cli_session)

    seen = cli_session.seen
    assert seen["message"] == _vision_block(shot, clip)
    media = seen["kwargs"]["media"]
    assert [Path(str(i.filepath)).name for i in media.images] == ["shot.png"]
    assert [Path(str(v.filepath)).name for v in media.videos] == ["clip.mp4"]
    assert "PNG bytes" in seen["cat"] and not seen["cat"].startswith("Not run:")
    assert "Vision is disabled" not in capsys.readouterr().out
    assert cli_session.media_guard.files == []


async def test_the_backend_describes_an_absolute_media_path_written_without_a_mention(
    tmp_path: Path,
):
    shot, _ = _files(tmp_path)
    session = _BackendSession(tmp_path, vision=False)

    result = await PromptBuilder(session).build(f"what is {shot}", None, interrupted_summary=None)  # type: ignore[arg-type]

    assert _body(result.message).startswith(f"<attached-files>\n{HEADER}\n- shot.png (path: {shot}")
    assert [f.path for f in session.media_guard.files] == [shot]


# ── a vision model: attached, or described when not ───────────────


def _far(root: Path) -> str:
    """A PNG outside every allowed root: found, but never attached."""
    far = root / "elsewhere" / "far.png"
    far.parent.mkdir(parents=True, exist_ok=True)
    far.write_bytes(PNG)
    return str(far.resolve())


def _line(path: str) -> str:
    """The metadata line of one 7x3 PNG."""
    return (
        f"- {Path(path).name} (path: {path}, type: image/png, size: 74 bytes, dimensions: 7x3 px)"
    )


async def _backend(session: Any, message: str) -> Any:
    """The backend's build of ``message`` on ``session``."""
    return await PromptBuilder(session).build(message, None, interrupted_summary=None)


def _real_backend(session: Any, message: str) -> str:
    """The message a real session's backend sends; the media and info land on ``session.seen``."""
    result = asyncio.run(_backend(session, message))
    session.seen.update(
        message=_body(result.message),
        media=result.media,
        info=[i.text for i in result.info_messages],
    )
    return session.seen["message"]


def _cli_message(enter: Any) -> Any:
    """A CLI entry point that returns the message its turn sent; the media lands on ``session.seen``."""

    def run(session: Any, message: str) -> str:
        enter(session, message)
        session.seen["media"] = session.seen["kwargs"].get("media")
        return session.seen["message"]

    return run


EVERY_ENTRY_POINT = [
    pytest.param(_real_backend, id="backend"),
    pytest.param(_cli_message(_dash_m), id="igni -m"),
    pytest.param(_cli_message(_pipe), id="pipe mode"),
]


def _image_names(media: Any) -> list[str]:
    """The file names of the images sent."""
    return [Path(str(i.filepath)).name for i in (media.images or [])] if media else []


@pytest.mark.parametrize("send", EVERY_ENTRY_POINT)
def test_a_vision_model_is_told_its_media_is_attached_and_the_markdown_to_read(
    cli_session, capsys, send
):
    _use_vision(cli_session, True)
    shot, clip = _files(cli_session.project_dir)

    sent = send(cli_session, MESSAGE)

    assert sent == _vision_block(shot, clip)
    assert "read before responding]" in sent.split(ATTACHED)[0]
    assert shot not in sent.split(ATTACHED)[0], "the PNG is never listed as a file to read"
    assert _image_names(cli_session.seen["media"]) == ["shot.png"]
    assert cli_session.media_guard.files == []
    assert "Vision is disabled" not in capsys.readouterr().out


@pytest.mark.parametrize("send", EVERY_ENTRY_POINT)
def test_a_media_file_a_vision_model_was_not_sent_is_described_by_its_metadata(
    cli_session, capsys, send
):
    _use_vision(cli_session, True)
    shot, _ = _files(cli_session.project_dir)
    far = _far(cli_session.project_dir.parent)

    sent = send(cli_session, f"compare @shot.png @{far}")

    assert sent == (
        f"<attached-files>\n{ATTACHED}\n{_line(shot)}\n{UNATTACHED}\n{_line(far)}\n"
        f"</attached-files>\ncompare @{shot} @{far}"
    )
    assert "Vision is disabled" not in sent
    assert _image_names(cli_session.seen["media"]) == ["shot.png"]
    assert [f.path for f in cli_session.media_guard.files] == [far]
    shown = [*cli_session.seen.get("info", []), _flat(capsys.readouterr().out)]
    assert not any("Vision is disabled" in line for line in shown)
    assert any(_UNATTACHED_LINE.format(names="far.png") in line for line in shown)


@pytest.mark.parametrize("send", EVERY_ENTRY_POINT)
def test_a_vision_model_is_told_nothing_was_attached_when_the_resolver_attached_nothing(
    cli_session, capsys, monkeypatch, send
):
    _use_vision(cli_session, True)
    shot, clip = _files(cli_session.project_dir)
    monkeypatch.setattr(MediaResolver, "attach_local_paths", lambda self, paths: None)

    sent = send(cli_session, MESSAGE)

    assert sent == _text_only_block(shot, clip).replace(HEADER, UNATTACHED)
    assert ATTACHED not in sent and "Vision is disabled" not in sent
    assert cli_session.seen["media"] is None
    assert [f.path for f in cli_session.media_guard.files] == [shot, clip]
    shown = [*cli_session.seen.get("info", []), _flat(capsys.readouterr().out)]
    assert not any("Vision is disabled" in line for line in shown)
    assert any(_UNATTACHED_LINE.format(names="shot.png, clip.mp4") in line for line in shown)


@pytest.mark.parametrize("send", EVERY_ENTRY_POINT)
@pytest.mark.parametrize("vision", [True, False])
def test_a_bare_mention_found_outside_the_project_is_described_once(
    cli_session, monkeypatch, vision, send
):
    _use_vision(cli_session, vision)
    downloads = cli_session.project_dir.parent / "downloads"
    downloads.mkdir()
    (downloads / "shot.png").write_bytes(PNG)
    shot = str((downloads / "shot.png").resolve())
    monkeypatch.setattr(MediaResolver, "_DEFAULT_SEARCH_DIRS", (downloads,))

    sent = send(cli_session, "what is @shot.png")

    header = ATTACHED if vision else HEADER
    assert sent == f"<attached-files>\n{header}\n{_line(shot)}\n</attached-files>\nwhat is @{shot}"
