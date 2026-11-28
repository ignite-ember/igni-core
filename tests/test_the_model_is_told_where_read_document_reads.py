"""The prompts, ``create_agent``, ``--read-only`` and attachments point documents at ``read_document``."""

from __future__ import annotations

import asyncio
import itertools
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from agno.tools.function import Function

from igni_core.core.agents import AgentPool
from igni_core.core.config.permission_eval import PermissionEvaluator
from igni_core.core.config.permission_eval.schemas import PermissionDecision
from igni_core.core.documents import budget as budget_module
from igni_core.core.documents.attached import AttachedDocumentText
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.tools.orchestrate import OrchestrateTools
from igni_core.core.tools.read_document import ReadDocumentTools
from igni_core.core.tools.tool_spec import ToolSpecCatalog
from tests.document_fixtures import DocumentFixtures

REPO = Path(__file__).resolve().parent.parent
BUNDLED = REPO / "src/igni_core/bundled_agents"
ROOT_AGENTS = REPO / "agents"
MAIN_PROMPT = REPO / "src/igni_core/core/prompts/main_agent.md"
NONCE = re.compile(r"nonce=[0-9a-f]+")

DOCUMENTS = (
    " Read PDF, Word, Excel, PowerPoint, CSV and TSV files with `read_document`; "
    "plain-text and code files are still read with the shell."
)
DEFAULT_TO_SHELL = (
    "- **Default to shell** — `run_shell_command` for searching (`rg`, `grep -r`), finding files "
    "(`find`, `fd`), listing (`ls`), reading (`cat`, `head`, `tail`, `sed -n`), running "
    "tests/builds/git/package managers." + DOCUMENTS
)
DOCS_DEFAULT_TO_SHELL = (
    "- **Default to shell** for all reads, searches, and lookups (`cat`, `rg`, `find`, `ls`, etc.)."
    + DOCUMENTS
)


# ── the main prompt and the agents ──────────────────────────────────


def test_the_main_prompt_s_tool_preferences_name_read_document_after_the_shell():
    lines = MAIN_PROMPT.read_text(encoding="utf-8").splitlines()
    shell = next(i for i, line in enumerate(lines) if line.startswith("- **`run_shell_command`**"))

    assert lines[shell + 1] == (
        "- **`read_document`** — for PDF, Word, Excel, PowerPoint, CSV and TSV files. "
        "Plain-text and code files are still read with the shell."
    )
    assert lines[shell + 2].startswith("- **`edit_file`**")


def _agents(folder: Path) -> list[tuple[Path, list[str], list[str]]]:
    agents = []
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---", 2)[1]) or {}
        raw = front.get("tools", "")
        tools = raw if isinstance(raw, list) else [t.strip() for t in str(raw).split(",")]
        agents.append((path, [t for t in tools if t], text.splitlines()))
    return agents


@pytest.mark.parametrize("folder", [BUNDLED, ROOT_AGENTS], ids=["bundled", "root"])
def test_every_agent_s_default_to_shell_line_names_read_document(folder):
    changed = []
    for path, tools, lines in _agents(folder):
        defaults = [line for line in lines if line.startswith("- **Default to shell**")]
        if not defaults:
            continue
        assert "ReadDocument" in tools, path.name
        assert defaults in ([DEFAULT_TO_SHELL], [DOCS_DEFAULT_TO_SHELL]), path.name
        changed.append(path.stem)

    assert sorted(changed) == ["debugger", "docs", "editor", "explorer", "qa", "simplifier"]


def test_the_docs_agent_has_its_own_default_to_shell_line():
    for folder in (BUNDLED, ROOT_AGENTS):
        assert DOCS_DEFAULT_TO_SHELL in (folder / "docs.md").read_text(encoding="utf-8")


# ── create_agent ────────────────────────────────────────────────────


def test_create_agent_s_tools_argument_names_read_document():
    function = Function.from_callable(OrchestrateTools.create_agent)

    assert function.to_dict()["parameters"]["properties"]["tools"]["description"] == (
        'Comma-separated tool names (e.g. "Bash,Edit,Write").\n'
        "Valid: Bash, BashOutput, Edit, NotebookEdit, ReadDocument, Schedule,\n"
        "WebFetch, WebSearch, Write. Searching and reading files\n"
        "go through Bash (``rg``, ``cat``, ``find``) — there are no\n"
        "separate Read/Grep/Glob/LS toolkits. ReadDocument reads PDF,\n"
        "Word, Excel, PowerPoint, CSV and TSV files."
    )


def test_an_ephemeral_agent_may_be_given_read_document():
    catalog = ToolSpecCatalog.default()

    assert catalog.valid_ephemeral_names == {
        "Bash",
        "BashOutput",
        "Edit",
        "NotebookEdit",
        "ReadDocument",
        "Schedule",
        "WebFetch",
        "WebSearch",
        "Write",
    }
    assert catalog.normalize(["read_document"]).ok


@patch("igni_core.core.agents.builder.AgentBuilder.build")
def test_create_agent_creates_an_agent_with_read_document(mock_build, tmp_path, settings):
    mock_build.return_value = MagicMock()
    pool = AgentPool(settings=settings, project_dir=tmp_path)
    pool.init_ephemeral(tmp_path)
    tools = OrchestrateTools(pool=pool, settings=settings)

    result = tools.create_agent(
        name="reader", description="Reads reports", system_prompt="Read.", tools="Bash,ReadDocument"
    )

    assert result.startswith("Created ephemeral agent 'reader'"), result
    assert "ReadDocument" in pool.get_entry("reader").definition.tools


# ── --read-only ─────────────────────────────────────────────────────


def test_the_read_only_shell_refusal_names_read_document_for_documents():
    evaluator = PermissionEvaluator.from_strings()
    evaluator.read_only = True

    outcome = evaluator.evaluate_outcome("run_shell_command", {"command": "rm report.pdf"})

    assert outcome.decision is PermissionDecision.DENY
    assert outcome.reason == (
        "this session is read-only (--read-only): only read-only shell commands run "
        "(cat, ls, rg, grep, find, head, wc, git log/diff/show/status, …), and this "
        "one could write, delete, run another program or reach the network. Read "
        "files with those commands, documents (PDF, Word, Excel, PowerPoint, CSV, "
        "TSV) with read_document, and the web with web_search/fetch_url; for "
        "anything else, say what you would run and why rather than retrying."
    )


# ── attachments ─────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path.resolve()


def _hint(part: str) -> str:
    return f' To read on, call read_document with part="{part}".]'


def _both(project: Path, path: Path) -> tuple[str, str]:
    """The attachment's block and ``read_document``'s result for ``path``, nonces blanked."""
    attached = AttachedDocumentText.collect([str(path)]).wrap("")
    block = attached.removeprefix("<attached-files>\n").removesuffix("\n</attached-files>")
    tool = asyncio.run(ReadDocumentTools(base_dir=str(project)).read_document(str(path)))
    return NONCE.sub("nonce", block), NONCE.sub("nonce", tool)


def _cut(monkeypatch, cap: str, value: int) -> None:
    monkeypatch.setattr(DocumentReader, cap, value)


CUTS = [
    ("rows", "MAX_ROWS", 3, lambda p: DocumentFixtures.csv_rows(p / "a.csv", 10), "4-"),
    (
        "sheets",
        "MAX_SHEETS",
        1,
        lambda p: DocumentFixtures.xlsx_sheets(p / "b.xlsx", {"A": 2, "B": 2}),
        "2-",
    ),
    (
        "sheet rows",
        "MAX_ROWS",
        2,
        lambda p: DocumentFixtures.xlsx_sheets(p / "c.xlsx", {"S": 4}),
        "S!3-",
    ),
    ("slides", "MAX_SLIDES", 2, lambda p: DocumentFixtures.pptx_titles(p / "d.pptx", 5), "3-"),
    (
        "pdf chars",
        "MAX_CHARS",
        25,
        lambda p: DocumentFixtures.pdf(p / "e.pdf", [["a" * 15], ["b" * 15]]),
        "2-",
    ),
    (
        "docx chars",
        "MAX_CHARS",
        12,
        lambda p: DocumentFixtures.docx_paragraphs(p / "f.docx", ["a" * 5, "b" * 5, "c" * 5]),
        "3-",
    ),
    ("csv chars", "MAX_CHARS", 40, lambda p: DocumentFixtures.csv_rows(p / "g.csv", 10), "3-"),
]


@pytest.mark.parametrize(
    ("cap", "value", "make", "part"), [c[1:] for c in CUTS], ids=[c[0] for c in CUTS]
)
def test_an_attachment_cut_gives_the_hint_read_document_gives(
    project, monkeypatch, cap, value, make, part
):
    _cut(monkeypatch, cap, value)
    path = make(project)

    attached, tool = _both(project, path)

    assert _hint(part) in attached
    assert attached == tool


def test_an_attachment_that_runs_out_of_time_gives_the_hint_read_document_gives(
    project, monkeypatch
):
    path = DocumentFixtures.pptx_titles(project / "deck.pptx", 3)
    clock = itertools.count(0, 10)
    monkeypatch.setattr(budget_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    attached = NONCE.sub("nonce", AttachedDocumentText.collect([str(path)]).wrap(""))
    clock = itertools.count(0, 10)
    tool = NONCE.sub(
        "nonce", asyncio.run(ReadDocumentTools(base_dir=str(project)).read_document(str(path)))
    )

    assert "[Text truncated after 15 seconds of reading." + _hint("2-") in attached
    assert tool in attached


def test_an_attachment_that_is_not_cut_has_no_hint(project):
    path = DocumentFixtures.csv_rows(project / "small.csv", 3)

    attached, tool = _both(project, path)

    assert "To read on" not in attached
    assert attached == tool
