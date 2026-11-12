"""``PromptBuilder.build`` (the backend's): the message a run is handed and the lines shown beside it.

The uploaded-file path through a vision model has its own file. Here: what the
client's own media does when the text also names a file or a URL, what a model
without vision is told, the ``<system-context>`` header and the interrupted-run
recap, the queued reminders that ride ahead of it, the info lines in order, a
model that is not in the registry, and an upload kept in the database.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from igni_core.backend.prompt_builder import PromptBuilder
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.utils.media import MediaResolver
from igni_core.core.utils.media_schemas import MediaAttachments

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
UPLOAD = "attachment:" + "a" * 32


class _Session:
    def __init__(self, root: Path, *, vision: bool | None) -> None:
        self.project_dir = root
        registry = {} if vision is None else {"m": ModelRegistryEntry(model_id="m", vision=vision)}
        self.settings = SimpleNamespace(
            storage=SimpleNamespace(database_url=None, data_dir=root),
            models=ModelsConfig(default="m", registry=registry),
        )
        self.reminders = PendingReminderQueue()
        self.injected = 0
        self.learning_mgr = SimpleNamespace(inject=self._inject)

    async def _inject(self) -> None:
        self.injected += 1


def _builder(tmp_path: Path, *, vision: bool | None) -> tuple[PromptBuilder, _Session]:
    session = _Session(tmp_path, vision=vision)
    return PromptBuilder(session), session  # type: ignore[arg-type]


def _image(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(PNG)
    return path


def _clients_media(tmp_path: Path, name: str = "pasted.png") -> MediaAttachments:
    """What the front end sent with the message: one pasted image."""
    media = MediaResolver(project_dir=tmp_path).attach_local_paths([str(_image(tmp_path, name))])
    assert media is not None
    return media


def _texts(result) -> list[str]:
    return [i.text for i in result.info_messages]


async def test_plain_text_gets_the_header_and_nothing_else(tmp_path: Path):
    builder, session = _builder(tmp_path, vision=True)

    result = await builder.build("hello there", None, interrupted_summary=None)

    assert re.fullmatch(
        r"<system-context>Current datetime: \d{4}-\d\d-\d\d \d\d:\d\d \S+</system-context>\nhello there",
        result.message,
    )
    assert result.media is None
    assert result.info_messages == []
    assert session.injected == 1, "learnings are injected, and awaited, on every turn"


async def test_the_media_the_client_sent_is_kept_when_the_text_names_a_file(tmp_path: Path):
    _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=True)
    sent = _clients_media(tmp_path)

    result = await builder.build("what is in shot.png", sent, interrupted_summary=None)

    assert result.media is not None
    names = sorted(Path(i.filepath).name for i in result.media.images)
    assert names == ["pasted.png", "shot.png"]
    assert "Attached: 1 file(s)" in _texts(result)


async def test_the_media_the_client_sent_is_kept_when_the_text_names_a_url(tmp_path: Path):
    builder, _ = _builder(tmp_path, vision=True)
    sent = _clients_media(tmp_path)

    result = await builder.build(
        "compare with https://img.example/a.png", sent, interrupted_summary=None
    )

    assert result.media is not None and len(result.media.images) == 2
    assert "Attached 1 URL(s)" in _texts(result)


async def test_a_url_image_is_attached_on_a_vision_model(tmp_path: Path):
    builder, _ = _builder(tmp_path, vision=True)

    result = await builder.build("see https://img.example/a.png", None, interrupted_summary=None)

    assert result.media is not None
    assert [i.url for i in result.media.images] == ["https://img.example/a.png"]
    assert _texts(result) == ["Attached 1 URL(s)"]


async def test_a_model_without_vision_gets_the_resolved_path_as_text_and_no_media(
    tmp_path: Path,
):
    shot = _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build(
        "what is in shot.png and https://img.example/a.png", None, interrupted_summary=None
    )

    assert result.media is None
    assert _texts(result) == [f"Resolved: {shot.resolve()}"]
    assert str(shot.resolve()) in result.message


async def test_a_model_without_vision_keeps_the_media_the_client_sent_untouched(tmp_path: Path):
    _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=False)
    sent = _clients_media(tmp_path)

    result = await builder.build("what is in shot.png", sent, interrupted_summary=None)

    assert result.media is sent and len(sent.images) == 1


async def test_a_vision_model_that_cannot_attach_the_file_is_told_it_was_resolved(
    tmp_path: Path, monkeypatch
):
    shot = _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=True)
    monkeypatch.setattr(MediaResolver, "attach_local_paths", lambda self, paths: None)

    result = await builder.build("what is in shot.png", None, interrupted_summary=None)

    assert result.media is None
    assert _texts(result) == [f"Resolved: {shot.resolve()}"]


async def test_several_resolved_files_are_listed_comma_joined(tmp_path: Path):
    one, two = _image(tmp_path, "one.png"), _image(tmp_path, "two.png")
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build("one.png and two.png", None, interrupted_summary=None)

    assert _texts(result) == [f"Resolved: {one.resolve()}, {two.resolve()}"]


async def test_an_at_mention_is_announced_and_listed_for_the_agent(tmp_path: Path):
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build("read @notes.md and @src/a.py", None, interrupted_summary=None)

    assert _texts(result)[0] == "Referenced: notes.md, src/a.py"
    assert "[Referenced files: notes.md, src/a.py — read before responding]" in result.message


async def test_an_email_address_is_not_a_mention(tmp_path: Path):
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build("mail me@example.com", None, interrupted_summary=None)

    assert result.info_messages == []


async def test_the_info_lines_come_in_the_order_the_steps_ran(tmp_path: Path):
    shot = _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=True)

    result = await builder.build(
        "look at @notes.md and shot.png and https://img.example/a.png",
        None,
        interrupted_summary="Earlier: the run was cut off.",
    )

    assert _texts(result) == [
        "Referenced: notes.md",
        "Attached: 1 file(s)",
        "Attached 1 URL(s)",
        "(continuing from an interrupted previous run)",
    ]
    assert result.media is not None and len(result.media.images) == 2
    assert str(shot.resolve()) in result.message


async def test_an_interrupted_run_is_recapped_in_the_header_and_announced(tmp_path: Path):
    builder, _ = _builder(tmp_path, vision=True)

    result = await builder.build("go on", None, interrupted_summary="You were editing a.py.")

    assert result.message.startswith("<system-context>Current datetime: ")
    assert re.search(r"[A-Z]+ You were editing a\.py\.</system-context>\ngo on$", result.message)
    assert _texts(result) == ["(continuing from an interrupted previous run)"]


@pytest.mark.parametrize("summary", [None, ""])
async def test_no_recap_means_no_notice_and_a_header_of_the_time_alone(tmp_path: Path, summary):
    builder, _ = _builder(tmp_path, vision=True)

    result = await builder.build("go on", None, interrupted_summary=summary)

    assert result.info_messages == []
    assert re.fullmatch(
        r"<system-context>Current datetime: [^<]*[A-Z0-9+:-]</system-context>\ngo on",
        result.message,
    )
    assert "  " not in result.message.split("\n")[0]


async def test_queued_reminders_are_drained_ahead_of_the_header_once(tmp_path: Path):
    builder, session = _builder(tmp_path, vision=True)
    session.reminders.queue("tests failed")
    session.reminders.queue("lint failed")

    first = await builder.build("go on", None, interrupted_summary=None)
    second = await builder.build("go on", None, interrupted_summary=None)

    assert first.message.startswith(
        "<system-reminder>tests failed\nlint failed</system-reminder>\n<system-context>"
    )
    assert first.message.endswith("\ngo on")
    assert "system-reminder" not in second.message
    assert session.reminders.drain() == []


async def test_a_model_missing_from_the_registry_has_no_vision(tmp_path: Path):
    _image(tmp_path, "shot.png")
    builder, _ = _builder(tmp_path, vision=None)

    result = await builder.build(
        "shot.png https://img.example/a.png", None, interrupted_summary=None
    )

    assert result.media is None
    assert _texts(result)[0].startswith("Resolved: ")
    assert len(_texts(result)) == 1


class _Store:
    def local_path(self, ref: str) -> Path | None:
        return Path("/kept/report.pdf") if ref == UPLOAD else None


async def test_an_upload_the_database_holds_is_named_by_its_file_before_the_mentions_run(
    tmp_path: Path, monkeypatch
):
    from igni_core.backend import prompt_builder

    opened: list[Path] = []

    def for_project(project_dir, locator):
        opened.append(Path(project_dir))
        return _Store()

    monkeypatch.setattr(prompt_builder.Attachments, "for_project", staticmethod(for_project))
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build(f"summarise @{UPLOAD} please", None, interrupted_summary=None)

    assert opened == [tmp_path]
    assert UPLOAD not in result.message
    assert "@/kept/report.pdf" in result.message
    assert _texts(result)[0] == "Referenced: /kept/report.pdf"


async def test_an_upload_the_store_does_not_hold_is_left_as_written(tmp_path: Path, monkeypatch):
    from igni_core.backend import prompt_builder

    monkeypatch.setattr(
        prompt_builder.Attachments, "for_project", staticmethod(lambda *_: _Store())
    )
    builder, _ = _builder(tmp_path, vision=False)
    other = "attachment:" + "b" * 32

    result = await builder.build(f"read {other}", None, interrupted_summary=None)

    assert other in result.message


async def test_a_message_without_an_upload_never_opens_the_store(tmp_path: Path, monkeypatch):
    from igni_core.backend import prompt_builder

    def refuse(*_):
        raise AssertionError("the store was opened")

    monkeypatch.setattr(prompt_builder.Attachments, "for_project", staticmethod(refuse))
    builder, _ = _builder(tmp_path, vision=False)

    result = await builder.build("just words", None, interrupted_summary=None)

    assert result.message.endswith("\njust words")
