"""A spreadsheet, Word or PowerPoint file the person attaches reaches the model as its text."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from igni_core.backend.prompt_builder import PromptBuilder
from igni_core.core.config.model_entry import ModelRegistryEntry
from igni_core.core.config.schemas.models import ModelsConfig
from igni_core.core.config.settings import Settings
from igni_core.core.session.reminders import PendingReminderQueue
from igni_core.core.session.session_run import SessionRun
from igni_core.core.utils.media import MediaResolver
from tests.document_fixtures import DocumentFixtures


def _upload(project: Path, name: str) -> Path:
    """Where the composer stores an upload: under the project's attachments directory."""
    folder = project / ".igni" / "attachments" / "6fa3e09e"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


class _Session:
    def __init__(self, root: Path, *, vision: bool) -> None:
        self.project_dir = root
        self.settings = SimpleNamespace(
            storage=SimpleNamespace(database_url=None, data_dir=root),
            models=ModelsConfig(
                default="m", registry={"m": ModelRegistryEntry(model_id="m", vision=vision)}
            ),
        )
        self.reminders = PendingReminderQueue()
        self.learning_mgr = SimpleNamespace(inject=AsyncMock())


async def _build(project: Path, text: str, *, vision: bool = True):
    return await PromptBuilder(_Session(project, vision=vision)).build(  # type: ignore[arg-type]
        text, None, interrupted_summary=None
    )


def _block(message: str) -> str:
    match = re.search(r"<attached-files>\n(.*?)\n</attached-files>", message, re.S)
    assert match, message
    return match.group(1)


def _untrusted(text: str) -> str:
    return (
        r"<untrusted_tool_output nonce=([0-9a-f]+)>\n"
        + re.escape(text)
        + r"\n</untrusted_tool_output nonce=\1>"
    )


@pytest.mark.parametrize("vision", [True, False], ids=["vision", "text-only"])
async def test_an_uploaded_workbook_s_text_follows_the_referenced_files_line(tmp_path, vision):
    book = DocumentFixtures.xlsx_two_sheets(_upload(tmp_path, "budget.xlsx"))

    message = (await _build(tmp_path, f"@{book} what is the total?", vision=vision)).message

    expected = (
        f"\\[Referenced files: {re.escape(str(book))} — read before responding\\]\n"
        + re.escape(f"[Text extracted from {book} (2 sheets):]")
        + "\n"
        + _untrusted(
            "--- Sheet: Budget ---\n"
            "| Item | Cost |\n|---|---|\n| Rent | 1200 |\n| Food | 300.5 |\n| Total | 9999 |\n"
            "--- Sheet: Notes ---\n| Reviewed by Anna |\n|---|"
        )
    )
    assert re.fullmatch(expected, _block(message)), message
    assert message.endswith(f"@{book} what is the total?")


async def test_an_uploaded_word_document_and_deck_both_reach_the_model(tmp_path):
    report = DocumentFixtures.docx_report(_upload(tmp_path, "report.docx"))
    deck = DocumentFixtures.pptx_deck(_upload(tmp_path, "deck.pptx"))

    block = _block((await _build(tmp_path, f"@{report} @{deck} compare them")).message)

    assert f"[Text extracted from {report}:]" in block
    assert "# Quarterly report\n# Summary\nRevenue grew." in block
    assert f"[Text extracted from {deck} (2 slides):]" in block
    assert "Speaker notes:\nMention the caps" in block


async def test_a_document_is_never_sent_to_a_vision_model_as_a_file(tmp_path):
    report = DocumentFixtures.docx_report(_upload(tmp_path, "report.docx"))

    result = await _build(tmp_path, f"@{report} summarise", vision=True)

    assert result.media is None
    assert "Revenue grew." in result.message


async def test_a_legacy_upload_gets_the_save_as_note(tmp_path):
    old = DocumentFixtures.legacy(_upload(tmp_path, "old.doc"))

    block = _block((await _build(tmp_path, f"@{old} open this")).message)

    assert block.endswith(
        f"[Could not read {old}: the legacy .doc format is not supported. Save it as .docx. "
        "To read it as it is, propose to the user writing a small reader for its format, with "
        "whatever it needs installed outside igni (in a project virtual environment, for "
        "example). Ask first, and install nothing until the user agrees.]"
    )


async def test_a_broken_upload_gets_its_note_and_the_turn_goes_on(tmp_path):
    broken = DocumentFixtures.corrupt(_upload(tmp_path, "broken.pptx"))

    message = (await _build(tmp_path, f"@{broken} what is this")).message

    assert _block(message).endswith(f"[No text could be extracted from {broken}.]")
    assert "Traceback" not in message


async def test_a_document_outside_the_allowed_places_is_not_read(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    secret = DocumentFixtures.csv_commas(elsewhere / "secret.csv")

    message = (await _build(project, f"@{secret} summarise")).message

    assert "Amsterdam" not in message
    assert "Text extracted" not in message


def test_the_resolver_finds_documents_but_attaches_only_media(tmp_path):
    sheet = DocumentFixtures.csv_commas(tmp_path / "people.csv")
    resolver = MediaResolver(project_dir=tmp_path, search_dirs=())

    resolved = resolver.resolve_text_references("look at people.csv please")

    assert resolved.attachable == [str(sheet.resolve())]
    assert resolved.text == f"look at {sheet.resolve()} please"
    assert resolver.attach_local_paths(resolved.attachable) is None


class _Run(SessionRun):
    async def run(self) -> None:  # pragma: no cover - never entered
        return None


@pytest.mark.parametrize(
    ("name", "write", "expected"),
    [
        ("people.csv", DocumentFixtures.csv_commas, "| Bob | Amsterdam, NL |"),
        ("budget.xlsx", DocumentFixtures.xlsx_two_sheets, "| Total | 9999 |"),
        ("report.docx", DocumentFixtures.docx_report, "| Quarter | Revenue |"),
        ("deck.pptx", DocumentFixtures.pptx_deck, "--- Slide 2 ---\n# Numbers"),
    ],
)
async def test_igni_m_and_pipe_put_the_document_s_text_in_the_message(
    tmp_path, name, write, expected
):
    path = write(tmp_path / name)
    session = MagicMock()
    session.session_id = "s-1"
    session.settings.models = ModelsConfig(
        default="m", registry={"m": {"provider": "openai_like", "model_id": "m"}}
    )
    session.project_dir = tmp_path
    session.handle_message = AsyncMock(return_value="ok")
    session.hook_executor.execute = AsyncMock()
    type(session).last_turn_failure = PropertyMock(return_value="")
    with patch("igni_core.core.session.session_run.Session", return_value=session):
        run = _Run(Settings())

    await run._run_turn(f"summarise {name} please")

    (sent,) = session.handle_message.await_args.args
    assert sent.startswith(f"<attached-files>\n[Text extracted from {path.resolve()}")
    assert expected in _block(sent)
    assert sent.endswith(f"summarise {path.resolve()} please")
