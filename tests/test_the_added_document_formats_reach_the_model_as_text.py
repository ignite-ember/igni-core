"""OpenDocument, .xls, RTF, EPUB, email, notebooks and iWork previews reach the model as text, cut where they say, refused in one line."""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import subprocess
import sys
import time
import zipfile
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree

from igni_core.core.documents import budget as budget_module
from igni_core.core.documents.attached import AttachedDocumentText
from igni_core.core.documents.eml_reader import EmlDocumentReader
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.registry import DocumentReaders
from igni_core.core.documents.rtf_reader import RtfDocumentReader
from igni_core.core.documents.safe_xml import SafeXml
from igni_core.core.tools.read_document import ReadDocumentTools
from tests.document_fixtures import DocumentFixtures
from tests.format_fixtures import OdfFixtures, TextFormatFixtures, XlsFixtures

UNTRUSTED = re.compile(
    r"<untrusted_tool_output nonce=([0-9a-f]+)>\n(.*)\n</untrusted_tool_output nonce=\1>", re.S
)
PROPOSE = (
    "propose to the user writing a small reader for its format, with whatever it needs "
    "installed outside igni (in a project virtual environment, for example). Ask first, and "
    "install nothing until the user agrees."
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path.resolve()


def _read(project: Path, path: str | Path, part: str = "") -> str:
    tool = ReadDocumentTools(base_dir=str(project))
    return asyncio.run(tool.read_document(str(path), part))


def _text(result: str, path: Path, count: str) -> str:
    """The text inside the untrusted block, after checking the line above it."""
    head, _, rest = result.partition("\n")
    assert head == (
        f"[Text extracted from {path} ({count}):]" if count else f"[Text extracted from {path}:]"
    ), result
    body = UNTRUSTED.fullmatch(rest)
    assert body, result
    return body.group(2)


def _hint(part: str) -> str:
    return f' To read on, call read_document with part="{part}".]'


def _walk(project: Path, name: str) -> list[str]:
    """Every body read by following each result's first read-on hint from the start."""
    bodies: list[str] = []
    part = ""
    for _ in range(100):
        body = UNTRUSTED.search(_read(project, name, part))
        assert body
        bodies.append(body.group(2))
        hints = re.findall(r'call read_document with part="([^"]+)"', body.group(2))
        if not hints:
            return bodies
        part = hints[0]
    raise AssertionError("the hints never ended")


def _rewrite(path: Path, changes: dict[str, bytes]) -> None:
    """The zip at ``path`` with ``changes``' members replaced, every other member kept."""
    with zipfile.ZipFile(path) as archive:
        items = {info.filename: archive.read(info) for info in archive.infolist()}
    items.update(changes)
    with zipfile.ZipFile(path, "w") as archive:
        for filename, data in items.items():
            archive.writestr(filename, data)


def _secret(tmp_path: Path) -> Path:
    path = tmp_path / "secret.txt"
    path.write_text("TOPSECRET-42", encoding="utf-8")
    return path


# ── OpenDocument ────────────────────────────────────────────────────


def test_an_odt_gives_headings_paragraphs_lists_and_tables_in_order(project):
    path = OdfFixtures.odt_report(project / "report.odt")

    assert _text(_read(project, "report.odt"), path, "") == (
        "# Quarterly report\n# Summary\nRevenue  grew\tfast.\nFirst point\nSecond point\n"
        "| Quarter | Revenue |\n|---|---|\n| Q1 | 10 \\| 12 |\n## Details\nAfter\nthe table."
    )


def test_an_odt_leaves_out_footnotes_comments_and_macros(project):
    OdfFixtures.odt_report(project / "report.odt")

    result = _read(project, "report.odt")

    assert "A footnote." not in result
    assert "A reviewer comment." not in result
    assert "rm -rf" not in result


def test_an_ods_gives_each_sheet_as_a_table_with_saved_values(project):
    path = OdfFixtures.ods_budget(project / "budget.ods")

    assert _text(_read(project, "budget.ods"), path, "2 sheets") == (
        "--- Sheet: Budget ---\n| Item | Cost |\n|---|---|\n| Rent | 1200 |\n"
        "| Food | 300.5 |\n| Food | 300.5 |\n| Total | 9999 |\n"
        "--- Sheet: Notes ---\n| Reviewed by Anna |  | Done |\n|---|---|---|"
    )


def test_an_odp_gives_each_slide_with_its_title_table_and_notes(project):
    path = OdfFixtures.odp_deck(project / "deck.odp")

    assert _text(_read(project, "deck.odp"), path, "2 slides") == (
        "--- Slide 1 ---\n# Roadmap\nShip the readers\nSpeaker notes:\nMention the caps\n"
        "--- Slide 2 ---\n# Numbers\n| Metric | Value |\n|---|---|\n| Users | 42 |"
    )


@pytest.mark.parametrize(
    ("name", "kind"), [("a.odt", "text"), ("a.ods", "spreadsheet"), ("a.odp", "presentation")]
)
def test_an_encrypted_opendocument_file_says_it_is_password_protected(project, name, kind):
    path = OdfFixtures.package(project / name, kind, "<text:p>hidden</text:p>", encrypted=True)

    assert _read(project, name) == f"[Could not read {path}: it is password-protected.]"


@pytest.mark.parametrize(
    ("name", "kind"), [("a.odt", "text"), ("a.ods", "spreadsheet"), ("a.odp", "presentation")]
)
def test_an_opendocument_file_of_another_kind_gives_no_text(project, name, kind):
    other = {"text": "presentation", "spreadsheet": "text", "presentation": "spreadsheet"}[kind]
    path = OdfFixtures.package(project / name, other, "<text:p>x</text:p>")

    assert _read(project, name) == f"[No text could be extracted from {path}.]"


def test_an_odt_part_counts_its_paragraphs(project):
    path = OdfFixtures.odt_paragraphs(project / "doc.odt", ["one", "two", "three"])

    assert _text(_read(project, "doc.odt", "2-"), path, "") == "two\nthree"
    assert _read(project, "doc.odt", "4") == (
        f'[Could not read {path}: part="4" is past its end; it has 3 paragraphs.]'
    )


def test_an_odt_cut_at_the_character_cap_names_the_paragraph(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 12)
    path = OdfFixtures.odt_paragraphs(project / "doc.odt", ["a" * 5, "b" * 5, "c" * 5])

    assert _text(_read(project, "doc.odt"), path, "") == (
        "a" * 5 + "\n" + "b" * 5 + "\ncc\n[Text truncated at 12 characters." + _hint("3-")
    )


def test_following_the_hints_reads_every_odt_paragraph(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 20)
    OdfFixtures.odt_paragraphs(project / "doc.odt", [f"para {n}" for n in range(1, 10)])

    seen = {
        int(n)
        for body in _walk(project, "doc.odt")
        for n in re.findall(r"para (\d+)\n", body + "\n")
    }

    assert seen == set(range(1, 10))


def test_an_ods_named_sheet_part_is_its_rows_and_a_long_sheet_names_the_next(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_ROWS", 2)
    path = OdfFixtures.ods_sheets(project / "book.ods", {"Sales": 4, "Costs": 2})

    first = _text(_read(project, "book.ods"), path, "2 sheets")
    rest = _text(_read(project, "book.ods", "sales!3-"), path, "2 sheets")

    assert first == (
        "--- Sheet: Sales ---\n| n | Sales |\n|---|---|\n| 1 | Sales-1 |\n"
        "[Rows truncated at 2 of 4." + _hint("Sales!3-") + "\n"
        "--- Sheet: Costs ---\n| n | Costs |\n|---|---|\n| 1 | Costs-1 |"
    )
    assert (
        rest == "--- Sheet: Sales ---\n| n | Sales |\n|---|---|\n| 2 | Sales-2 |\n| 3 | Sales-3 |"
    )


def test_an_ods_past_the_sheet_cap_names_the_sheets_to_read_next(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_SHEETS", 1)
    path = OdfFixtures.ods_sheets(project / "book.ods", {"A": 2, "B": 2})

    assert _text(_read(project, "book.ods"), path, "2 sheets") == (
        "--- Sheet: A ---\n| n | A |\n|---|---|\n| 1 | A-1 |\n[Sheets truncated at 1 of 2."
        + _hint("2-")
    )
    assert (
        _read(project, "book.ods", "C!1-") == f'[Could not read {path}: it has no sheet named "C".]'
    )


def test_an_ods_row_repeated_a_billion_times_costs_a_moment_not_the_time_cap(project):
    path = OdfFixtures.ods_repeat_bomb(project / "bomb.ods")

    started = time.monotonic()
    text = _text(_read(project, "bomb.ods"), path, "1 sheet")

    assert time.monotonic() - started < 5
    assert text.startswith("--- Sheet: Bomb ---\n| x | x |")
    assert "y" not in text
    assert text.endswith("[Text truncated at 100000 characters." + _hint("Bomb!2-"))


def test_an_odp_part_is_its_slides_and_a_long_deck_names_the_next(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_SLIDES", 2)
    path = OdfFixtures.odp_titles(project / "deck.odp", 5)

    first = _text(_read(project, "deck.odp"), path, "5 slides")
    last = _text(_read(project, "deck.odp", "5-"), path, "5 slides")

    assert first == (
        "--- Slide 1 ---\n# Slide 1\n--- Slide 2 ---\n# Slide 2\n[Slides truncated at 2 of 5."
        + _hint("3-")
    )
    assert last == "--- Slide 5 ---\n# Slide 5"
    assert _read(project, "deck.odp", "6-") == (
        f'[Could not read {path}: part="6-" is past its end; it has 5 slides.]'
    )


def test_following_the_hints_reads_every_odp_slide_once(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_SLIDES", 3)
    OdfFixtures.odp_titles(project / "deck.odp", 8)

    slides = [
        int(n)
        for body in _walk(project, "deck.odp")
        for n in re.findall(r"--- Slide (\d+) ---", body)
    ]

    assert slides == list(range(1, 9))


def test_an_odp_cut_at_the_character_cap_names_the_slide(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 12)
    path = OdfFixtures.odp_titles(project / "deck.odp", 3)

    assert _text(_read(project, "deck.odp"), path, "3 slides") == (
        "--- Slide 1 ---\n# Slide 1\n--- Slide 2 ---\n# S\n[Text truncated at 12 characters."
        + _hint("2-")
    )


# ── legacy Excel ────────────────────────────────────────────────────


def test_an_xls_gives_each_sheet_as_a_table_with_dates_and_booleans(project):
    path = XlsFixtures.budget(project / "budget.xls")

    assert _text(_read(project, "budget.xls"), path, "3 sheets") == (
        "--- Sheet: Budget ---\n| Item | Cost | Paid | Due |\n|---|---|---|---|\n"
        "| Rent | 1200 | True | 2025-12-09 |\n| Food | 300.5 | False |  |\n"
        "--- Sheet: Notes ---\n| Reviewed by Anna |\n|---|\n"
        "--- Sheet: Odd ---\n| Error | Noon | Far future | Gap | End |\n|---|---|---|---|---|\n"
        "| #DIV/0! | 2025-12-09T12:00:00 | 1000000000000 |  | x |"
    )


def test_an_xls_saved_with_a_password_says_so(project):
    path = XlsFixtures.encrypted(project / "locked.xls")

    assert _read(project, "locked.xls") == f"[Could not read {path}: it is password-protected.]"


def test_an_xls_that_is_not_a_workbook_gives_no_text(project):
    path = DocumentFixtures.legacy(project / "old.xls")

    assert _read(project, "old.xls") == f"[No text could be extracted from {path}.]"


def test_an_xls_named_sheet_and_row_cap_name_the_rows_to_read_next(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_ROWS", 2)
    path = XlsFixtures.sheets(project / "book.xls", {"Sales": 4, "Costs": 2})

    first = _text(_read(project, "book.xls"), path, "2 sheets")
    rest = _text(_read(project, "book.xls", "Sales!3-"), path, "2 sheets")

    assert first.startswith(
        "--- Sheet: Sales ---\n| n | Sales |\n|---|---|\n| 1 | Sales-1 |\n"
        "[Rows truncated at 2 of 4." + _hint("Sales!3-")
    )
    assert (
        rest == "--- Sheet: Sales ---\n| n | Sales |\n|---|---|\n| 2 | Sales-2 |\n| 3 | Sales-3 |"
    )


def test_following_the_hints_reaches_every_xls_sheet_and_row(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_SHEETS", 2)
    monkeypatch.setattr(DocumentReader, "MAX_ROWS", 3)
    XlsFixtures.sheets(project / "book.xls", {"A": 7, "B": 2, "C": 4})
    seen: set[str] = set()
    pending, done = [""], set()
    while pending:
        part = pending.pop()
        if part in done:
            continue
        done.add(part)
        body = UNTRUSTED.search(_read(project, "book.xls", part))
        assert body
        seen.update(re.findall(r"\| \d+ \| ([A-C]-\d+) \|", body.group(2)))
        pending.extend(re.findall(r'part="([^"]+)"', body.group(2)))

    assert seen == {f"A-{i}" for i in range(1, 7)} | {"B-1"} | {f"C-{i}" for i in range(1, 4)}


def test_an_xls_never_writes_to_the_console(project, capsys):
    DocumentFixtures.legacy(project / "old.xls")
    XlsFixtures.budget(project / "budget.xls")

    _read(project, "old.xls")
    _read(project, "budget.xls")

    assert capsys.readouterr() == ("", "")


# ── RTF ─────────────────────────────────────────────────────────────


def test_an_rtf_gives_its_paragraphs_without_pictures_fields_or_objects(project):
    path = TextFormatFixtures.rtf(project / "memo.rtf")

    result = _read(project, "memo.rtf")

    assert _text(result, path, "") == "Quarterly report\nRené paid €5."
    assert "evil.example" not in result
    assert "89504e47" not in result


def test_an_rtf_part_counts_paragraphs_and_a_cut_names_the_next(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 30)
    path = TextFormatFixtures.rtf(project / "memo.rtf", "third one", "fourth one")

    assert _text(_read(project, "memo.rtf", "3-"), path, "") == "third one\nfourth one"
    assert _text(_read(project, "memo.rtf"), path, "") == (
        "Quarterly report\nRené paid €5.\nt\n[Text truncated at 30 characters." + _hint("3-")
    )
    assert _read(project, "memo.rtf", "5") == (
        f'[Could not read {path}: part="5" is past its end; it has 4 paragraphs.]'
    )


def test_an_rtf_over_its_own_size_cap_is_not_handed_to_the_parser(project, monkeypatch):
    monkeypatch.setattr(RtfDocumentReader, "MAX_RTF_BYTES", 100)
    path = TextFormatFixtures.rtf(project / "memo.rtf")

    assert _read(project, "memo.rtf") == f"[Could not read {path}: it is too large to read.]"


# ── EPUB ────────────────────────────────────────────────────────────


def test_an_epub_gives_its_chapters_in_spine_order(project):
    path = TextFormatFixtures.epub(project / "book.epub")

    result = _read(project, "book.epub")

    assert _text(result, path, "2 chapters") == (
        "--- Chapter 1 ---\n# Part one\n## Opening\nIt began here.\nNext line.\nOne\nTwo\n"
        "  code\n    block\n"
        "--- Chapter 2 ---\n# Part two\nThe second chapter.\n| Name | Age |\n|---|---|\n| Anna | 30 |"
    )
    assert "alert" not in result
    assert "color: red" not in result


def test_an_epub_part_is_its_chapters(project):
    path = TextFormatFixtures.epub(project / "book.epub")

    assert _text(_read(project, "book.epub", "2"), path, "2 chapters").startswith(
        "--- Chapter 2 ---\n# Part two"
    )
    assert _read(project, "book.epub", "3-") == (
        f'[Could not read {path}: part="3-" is past its end; it has 2 chapters.]'
    )
    assert _read(project, "book.epub", "Ch!1").startswith(
        f'[Could not read {path}: part="Ch!1" is not a range'
    )


def test_an_epub_cut_at_the_character_cap_names_the_chapter(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 20)
    chapters = {f"c{n}.xhtml": f"<p>{chr(96 + n) * 15}</p>" for n in range(1, 4)}
    path = TextFormatFixtures.epub(project / "book.epub", chapters)

    assert _text(_read(project, "book.epub"), path, "3 chapters") == (
        "--- Chapter 1 ---\n" + "c" * 15 + "\n--- Chapter 2 ---\n" + "b" * 5 + "\n"
        "[Text truncated at 20 characters." + _hint("2-")
    )


def test_following_the_hints_reads_every_epub_chapter(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 40)
    chapters = {f"c{n:02}.xhtml": f"<h1>Chapter {n}</h1><p>{'x' * 20}</p>" for n in range(1, 8)}
    TextFormatFixtures.epub(project / "book.epub", chapters)

    seen = {
        int(n) for body in _walk(project, "book.epub") for n in re.findall(r"# Chapter (\d+)", body)
    }

    assert seen == set(range(1, 8))


def test_an_epub_that_runs_out_of_time_starts_again_at_the_unread_chapter(project, monkeypatch):
    path = TextFormatFixtures.epub(project / "book.epub")
    ticks = itertools.chain([0] * 8, itertools.repeat(99))
    monkeypatch.setattr(budget_module, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    text = _text(_read(project, "book.epub"), path, "2 chapters")

    assert text.endswith("[Text truncated after 15 seconds of reading." + _hint("2-"))
    assert "--- Chapter 2 ---" not in text


def test_an_epub_leaves_out_drm_encrypted_chapters(project):
    path = TextFormatFixtures.epub(project / "book.epub", encrypted=("ch1.xhtml",))

    text = _text(_read(project, "book.epub"), path, "1 chapter")

    assert "Part two" not in text
    assert text.startswith("--- Chapter 1 ---\n# Part one")


def test_an_epub_spine_cannot_reach_outside_the_archive(project, tmp_path):
    secret = _secret(tmp_path)
    escape = os.path.relpath(secret, "/").replace(os.sep, "/")
    path = TextFormatFixtures.epub(project / "book.epub", {"ok.xhtml": "<p>Inside</p>"})
    with zipfile.ZipFile(path) as archive:
        opf = archive.read("OEBPS/content.opf").decode()
    opf = opf.replace(
        "</manifest>",
        f'<item id="x" href="../../../../../../{escape}" media-type="application/xhtml+xml"/>'
        f'<item id="y" href="/{escape}" media-type="application/xhtml+xml"/></manifest>',
    )
    opf = opf.replace("<spine>", '<spine><itemref idref="x"/><itemref idref="y"/>')
    _rewrite(path, {"OEBPS/content.opf": opf.encode()})

    result = _read(project, "book.epub")

    assert "TOPSECRET" not in result
    assert "Inside" in result


# ── email ───────────────────────────────────────────────────────────


def test_an_email_gives_its_headers_attachments_and_text_body(project):
    path = TextFormatFixtures.email(project / "mail.eml")

    result = _read(project, "mail.eml")

    assert _text(result, path, "") == (
        "From: Anna Kovalenko <anna@example.com>\n"
        "To: Bob <bob@example.com>, Carol <carol@example.com>\n"
        "Cc: René <rene@example.com>\n"
        "Date: Thu, 26 Nov 2026 10:00:00 +0100\n"
        "Subject: Q3 numbers\n\n"
        "Attachments:\n- report-1.pdf (application/pdf, 960 bytes)\n\n"
        "Hi Bob,\n\nThe numbers are attached.\nSee page 2.\n\nAnna"
    )
    assert "secret payload" not in result


def test_an_html_only_email_body_becomes_text(project):
    path = TextFormatFixtures.email(project / "mail.eml", html_only=True, attachments=0)

    result = _read(project, "mail.eml")

    assert _text(result, path, "").endswith(
        "Subject: Q3 numbers\n\n# Q3\n\nThe numbers are attached.\n\n| Revenue | 10 |\n|---|---|"
    )
    assert "steal" not in result


def test_an_email_never_decodes_its_attachments(project, monkeypatch):
    TextFormatFixtures.email(project / "mail.eml", attachments=3)
    decoded: list[object] = []
    real = EmlDocumentReader._size

    def spy(self, part):
        original = part.get_payload

        def payload(*args, **kwargs):
            decoded.append(kwargs.get("decode") or (args[1] if len(args) > 1 else False))
            return original(*args, **kwargs)

        part.get_payload = payload
        return real(self, part)

    monkeypatch.setattr(EmlDocumentReader, "_size", spy)
    files_before = set(project.iterdir())

    result = _read(project, "mail.eml")

    assert "- report-3.pdf (application/pdf, 960 bytes)" in result
    assert decoded and not any(decoded)
    assert set(project.iterdir()) == files_before


def test_an_email_lists_at_most_a_hundred_attachments(project):
    path = TextFormatFixtures.email(project / "mail.eml", attachments=102)

    text = _text(_read(project, "mail.eml"), path, "")

    assert "- report-100.pdf" in text
    assert "- report-101.pdf" not in text
    assert "[Attachments truncated at 100 of 102.]" in text


def test_an_attached_email_inside_an_email_is_one_attachment(project):
    inner = TextFormatFixtures.email(project / "inner.eml", attachments=2)
    outer = project / "outer.eml"
    message = EmailMessage()
    message["Subject"] = "Fwd"
    message.set_content("See below.")
    message.add_attachment(
        inner.read_bytes(), maintype="message", subtype="rfc822", filename="fwd.eml"
    )
    outer.write_bytes(message.as_bytes())

    text = _text(_read(project, "outer.eml"), outer, "")

    assert "- fwd.eml (message/rfc822, " in text
    assert "report-1.pdf" not in text


def test_an_email_part_counts_its_body_paragraphs_and_keeps_the_headers(project, monkeypatch):
    body = "\n\n".join(f"Paragraph {n}." for n in range(1, 6))
    path = TextFormatFixtures.email(project / "mail.eml", attachments=0, body=body)

    text = _text(_read(project, "mail.eml", "4-"), path, "")

    assert text.startswith("From: Anna Kovalenko")
    assert text.endswith("Subject: Q3 numbers\n\nParagraph 4.\n\nParagraph 5.")
    assert _read(project, "mail.eml", "6") == (
        f'[Could not read {path}: part="6" is past its end; it has 5 paragraphs.]'
    )


def test_following_the_hints_reads_every_email_paragraph(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 300)
    body = "\n\n".join(f"Paragraph {n} " + "x" * 40 for n in range(1, 12))
    TextFormatFixtures.email(project / "mail.eml", attachments=0, body=body)

    seen = {
        int(n) for b in _walk(project, "mail.eml") for n in re.findall(r"Paragraph (\d+) x{40}", b)
    }

    assert seen == set(range(1, 12))


def test_a_huge_header_is_cut_so_the_body_still_fits(project, monkeypatch):
    path = TextFormatFixtures.email(project / "mail.eml", attachments=0)
    raw = path.read_bytes().replace(b"Subject: Q3 numbers", b"Subject: " + b"S" * 50_000)
    path.write_bytes(raw)

    text = _text(_read(project, "mail.eml"), path, "")

    assert f"Subject: {'S' * EmlDocumentReader.MAX_HEADER_CHARS}\n" in text
    assert text.endswith("Anna")


def test_an_email_over_its_own_size_cap_is_not_parsed(project, monkeypatch):
    monkeypatch.setattr(EmlDocumentReader, "MAX_EML_BYTES", 100)
    path = TextFormatFixtures.email(project / "mail.eml")

    assert _read(project, "mail.eml") == f"[Could not read {path}: it is too large to read.]"


def test_an_email_in_an_unknown_charset_is_still_read(project):
    path = project / "odd.eml"
    path.write_bytes(
        b"From: a@example.com\r\nSubject: odd\r\nContent-Type: text/plain; charset=x-unknown\r\n\r\nHello there\r\n"
    )

    assert _text(_read(project, "odd.eml"), path, "").endswith("Hello there")


# ── notebooks ───────────────────────────────────────────────────────


def test_a_notebook_gives_cells_in_order_and_says_non_text_outputs_are_left_out(project):
    path = TextFormatFixtures.notebook(project / "analysis.ipynb")

    result = _read(project, "analysis.ipynb")

    assert _text(result, path, "5 cells") == (
        "--- Cell 1 (markdown) ---\n# Analysis\nLoad the data.\n"
        "--- Cell 2 (code) ---\nx = 41\nprint(x + 1)\nOutput:\n42\n"
        "--- Cell 3 (code) ---\nplot(x)\nOutput:\n<Figure size 640x480>\n"
        "--- Cell 4 (code) ---\n1/0\nOutput:\nZeroDivisionError: division by zero\n"
        "--- Cell 5 (raw) ---\nraw text\n"
        "[Images, HTML and other non-text outputs are left out.]"
    )
    assert "iVBORw0KGgo" not in result


def test_a_notebook_with_only_text_has_no_left_out_note(project):
    path = TextFormatFixtures.notebook(
        project / "plain.ipynb",
        [
            {
                "cell_type": "code",
                "source": "1+1",
                "outputs": [{"output_type": "execute_result", "data": {"text/plain": "2"}}],
            }
        ],
    )

    assert (
        _text(_read(project, "plain.ipynb"), path, "1 cell")
        == "--- Cell 1 (code) ---\n1+1\nOutput:\n2"
    )


def test_a_markdown_cell_s_embedded_image_is_left_out_and_said_to_be(project):
    path = TextFormatFixtures.notebook(
        project / "img.ipynb",
        [
            {
                "cell_type": "markdown",
                "source": "![x](attachment:a.png)",
                "attachments": {"a.png": {"image/png": "AAAA"}},
            }
        ],
    )

    assert _text(_read(project, "img.ipynb"), path, "1 cell") == (
        "--- Cell 1 (markdown) ---\n![x](attachment:a.png)\n[Images, HTML and other non-text outputs are left out.]"
    )


def test_a_version_3_notebook_is_read_from_its_worksheets(project):
    path = project / "old.ipynb"
    path.write_text(
        '{"nbformat": 3, "worksheets": [{"cells": [{"cell_type": "code", "input": "x = 1", '
        '"outputs": [{"output_type": "pyout", "text": "1"}]}, "junk"]}]}',
        encoding="utf-8",
    )

    assert (
        _text(_read(project, "old.ipynb"), path, "1 cell")
        == "--- Cell 1 (code) ---\nx = 1\nOutput:\n1"
    )


def test_a_notebook_part_is_its_cells_and_a_cut_names_the_next(project, monkeypatch):
    path = TextFormatFixtures.notebook(project / "analysis.ipynb")

    assert (
        _text(_read(project, "analysis.ipynb", "5"), path, "5 cells")
        == "--- Cell 5 (raw) ---\nraw text"
    )
    assert _read(project, "analysis.ipynb", "6") == (
        f'[Could not read {path}: part="6" is past its end; it has 5 cells.]'
    )
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 60)
    assert _text(_read(project, "analysis.ipynb"), path, "5 cells").endswith(
        "[Text truncated at 60 characters." + _hint("2-")
    )


def test_following_the_hints_reads_every_notebook_cell(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_CHARS", 80)
    cells = [
        {"cell_type": "code", "source": f"cell_{n} = {'0' * 20}", "outputs": []}
        for n in range(1, 10)
    ]
    TextFormatFixtures.notebook(project / "nb.ipynb", cells)

    seen = {
        int(n)
        for body in _walk(project, "nb.ipynb")
        for n in re.findall(r"cell_(\d+) = 0{20}", body)
    }

    assert seen == set(range(1, 10))


@pytest.mark.parametrize("content", ["[1, 2]", "{not json", '{"cells": 5}'])
def test_a_file_that_is_not_a_notebook_gives_no_text(project, content):
    path = project / "bad.ipynb"
    path.write_text(content, encoding="utf-8")

    assert _read(project, "bad.ipynb") == f"[No text could be extracted from {path}.]"


# ── Apple iWork ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "app", "exports"),
    [
        ("a.numbers", "Numbers", ".xlsx or .csv"),
        ("a.pages", "Pages", ".docx or PDF"),
        ("a.key", "Keynote", ".pptx or PDF"),
    ],
)
def test_an_iwork_file_with_a_preview_is_read_from_it_and_says_so(project, name, app, exports):
    path = TextFormatFixtures.iwork_zip(project / name)

    assert _text(_read(project, name), path, "1 page") == (
        "--- Page 1 ---\nPreview line 1\n"
        f"[This is the document's preview, a PDF saved inside the {app} file; it may not show "
        f"all of the document. For all of it, ask the user to export it from {app} as {exports}.]"
    )


@pytest.mark.parametrize(
    ("name", "app", "exports"),
    [
        ("a.numbers", "Numbers", ".xlsx or .csv"),
        ("a.pages", "Pages", ".docx or PDF"),
        ("a.key", "Keynote", ".pptx or PDF"),
    ],
)
def test_an_iwork_file_without_a_preview_says_to_export_it(project, name, app, exports):
    path = TextFormatFixtures.iwork_zip(project / name, preview=None)

    assert _read(project, name) == (
        f"[Could not read {path}: this {app} file holds no preview PDF to read. "
        f"Ask the user to export it from {app} as {exports}.]"
    )


def test_an_iwork_preview_at_the_top_is_found_in_any_case(project):
    path = TextFormatFixtures.iwork_zip(project / "a.pages", preview="Preview.PDF")

    assert _text(_read(project, "a.pages"), path, "1 page").startswith(
        "--- Page 1 ---\nPreview line 1\n"
    )


def test_an_iwork_preview_part_is_its_pages(project):
    path = TextFormatFixtures.iwork_zip(project / "a.key", pages=3)

    assert _text(_read(project, "a.key", "2-"), path, "3 pages").startswith(
        "--- Page 2 ---\nPreview line 2\n--- Page 3 ---\nPreview line 3\n[This is the document's preview"
    )
    assert (
        _read(project, "a.key", "4")
        == f'[Could not read {path}: part="4" is past its end; it has 3 pages.]'
    )


def test_an_iwork_folder_bundle_is_read_from_its_preview(project):
    path = TextFormatFixtures.iwork_bundle(project / "talk.key")

    assert _text(_read(project, "talk.key"), path, "1 page").startswith(
        "--- Page 1 ---\nBundle preview\n"
    )


def test_an_iwork_folder_bundle_without_a_preview_says_to_export_it(project):
    path = TextFormatFixtures.iwork_bundle(project / "sheet.numbers", preview=False)

    assert _read(project, "sheet.numbers") == (
        f"[Could not read {path}: this Numbers file holds no preview PDF to read. "
        "Ask the user to export it from Numbers as .xlsx or .csv.]"
    )


def test_an_iwork_bundle_preview_linked_outside_the_bundle_is_not_read(project, tmp_path):
    outside = DocumentFixtures.pdf(tmp_path / "outside" / "Preview.pdf", [["TOPSECRET outside"]])
    bundle = project / "talk.key"
    bundle.mkdir()
    (bundle / "QuickLook").symlink_to(outside.parent, target_is_directory=True)

    result = _read(project, "talk.key")

    assert "TOPSECRET" not in result
    assert result.startswith(f"[Could not read {bundle}: this Keynote file holds no preview PDF")


def test_an_iwork_bundle_preview_over_the_size_cap_is_not_read(project, monkeypatch):
    path = TextFormatFixtures.iwork_bundle(project / "talk.key")
    monkeypatch.setattr(DocumentReader, "MAX_BYTES", 300)

    assert _read(project, "talk.key") == f"[Could not read {path}: it is too large to read.]"


def test_a_plain_folder_and_a_folder_named_like_a_zipped_format_are_still_directories(project):
    (project / "notes.odt").mkdir()

    assert (
        _read(project, "notes.odt")
        == f"[Could not read {project / 'notes.odt'}: it is a directory.]"
    )


def test_an_iwork_bundle_outside_the_project_is_refused(project, tmp_path):
    bundle = TextFormatFixtures.iwork_bundle(tmp_path / "elsewhere.key")

    assert _read(project, bundle) == (
        f"[Could not read {bundle}: it is outside the project. Ask the user to attach it.]"
    )


def test_a_pem_key_named_dot_key_shows_none_of_its_content(project):
    path = project / "server.key"
    path.write_text("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n")

    assert _read(project, "server.key") == f"[No text could be extracted from {path}.]"


# ── hostile packages ────────────────────────────────────────────────


def _bomb(path: Path) -> Path:
    """A zip claiming far more unpacked bytes than any reader opens."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/zip")
        archive.writestr("content.xml", b"\0" * (4 * 1024 * 1024))
        archive.writestr("QuickLook/Preview.pdf", b"\0" * (4 * 1024 * 1024))
    return path


@pytest.mark.parametrize("name", ["bomb.odt", "bomb.ods", "bomb.odp", "bomb.epub", "bomb.numbers"])
def test_a_zip_bomb_is_refused_before_anything_unpacks(project, monkeypatch, name):
    monkeypatch.setattr(DocumentReader, "MAX_UNZIPPED_BYTES", 1024 * 1024)
    path = _bomb(project / name)

    assert _read(project, name) == f"[Could not read {path}: it is too large to read.]"


@pytest.mark.parametrize(
    ("name", "write"),
    [
        ("big.odt", lambda p: OdfFixtures.odt_paragraphs(p, ["x" * 5000])),
        (
            "big.epub",
            lambda p: TextFormatFixtures.epub(p, {"c.xhtml": "<p>" + "x" * 5000 + "</p>"}),
        ),
    ],
)
def test_one_xml_part_over_the_part_cap_is_not_parsed(project, monkeypatch, name, write):
    monkeypatch.setattr(DocumentReader, "MAX_PART_BYTES", 2000)
    path = write(project / name)

    assert _read(project, name) == f"[Could not read {path}: it is too large to read.]"


def test_a_zip_member_that_lies_about_its_size_is_never_read_past_the_cap(project, monkeypatch):
    monkeypatch.setattr(DocumentReader, "MAX_PART_BYTES", 2000)
    path = OdfFixtures.odt_paragraphs(project / "liar.odt", ["x" * 5000])
    raw = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("content.xml")
    real = info.file_size.to_bytes(4, "little")
    raw = bytearray(bytes(raw).replace(real, (100).to_bytes(4, "little")))
    path.write_bytes(bytes(raw))

    result = _read(project, "liar.odt")

    assert result in (
        f"[No text could be extracted from {path}.]",
        f"[Could not read {path}: it is too large to read.]",
    )
    assert "x" * 200 not in result


def _xxe(secret: Path) -> str:
    return f'<!DOCTYPE d [<!ENTITY xxe SYSTEM "file://{secret}">]>'


def test_an_opendocument_external_entity_is_never_resolved(project, tmp_path):
    secret = _secret(tmp_path)
    path = OdfFixtures.package(
        project / "x.odt", "text", "<text:p>before &xxe; after</text:p>", prolog=_xxe(secret)
    )

    result = _read(project, "x.odt")

    assert _text(result, path, "") == "before  after"
    assert "TOPSECRET" not in result


def test_an_opendocument_billion_laughs_is_never_expanded(project):
    lol = "".join(f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' for n in range(1, 10))
    prolog = f'<!DOCTYPE d [<!ENTITY l0 "lol">{lol}]>'
    path = OdfFixtures.package(
        project / "laughs.odt", "text", "<text:p>a&l9;b</text:p>", prolog=prolog
    )

    started = time.monotonic()
    result = _read(project, "laughs.odt")

    assert time.monotonic() - started < 5
    assert "lol" not in result
    assert _text(result, path, "") in ("a", "ab")


def test_an_epub_external_entity_is_never_resolved(project, tmp_path):
    secret = _secret(tmp_path)
    path = TextFormatFixtures.epub(
        project / "x.epub", {"c.xhtml": "<p>before &xxe; after&nbsp;!</p>"}
    )
    with zipfile.ZipFile(path) as archive:
        items = {i.filename: archive.read(i) for i in archive.infolist()}
    items["OEBPS/text/c.xhtml"] = items["OEBPS/text/c.xhtml"].replace(
        b"<!DOCTYPE html>", _xxe(secret).encode()
    )
    with zipfile.ZipFile(path, "w") as archive:
        for filename, data in items.items():
            archive.writestr(filename, data)

    result = _read(project, "x.epub")

    assert _text(result, path, "1 chapter") == "--- Chapter 1 ---\nbefore after !"
    assert "TOPSECRET" not in result


def test_an_epub_container_external_entity_is_never_resolved(project, tmp_path):
    secret = _secret(tmp_path)
    path = TextFormatFixtures.epub(project / "x.epub", {"c.xhtml": "<p>fine</p>"})
    with zipfile.ZipFile(path) as archive:
        items = {i.filename: archive.read(i) for i in archive.infolist()}
    items["META-INF/container.xml"] = (
        items["META-INF/container.xml"]
        .replace(b'<?xml version="1.0"?>', b'<?xml version="1.0"?>' + _xxe(secret).encode())
        .replace(b"<rootfiles>", b"<rootfiles>&xxe;")
    )
    with zipfile.ZipFile(path, "w") as archive:
        for filename, data in items.items():
            archive.writestr(filename, data)

    result = _read(project, "x.epub")

    assert "TOPSECRET" not in result
    assert "fine" in result


def test_an_html_email_external_entity_is_never_resolved(project, tmp_path):
    secret = _secret(tmp_path)
    path = project / "x.eml"
    path.write_text(
        "From: a@example.com\nSubject: x\nContent-Type: text/html; charset=utf-8\n\n"
        f'<?xml version="1.0"?>{_xxe(secret)}<html><body><p>hi &xxe; there</p></body></html>\n',
        encoding="utf-8",
    )

    result = _read(project, "x.eml")

    assert "TOPSECRET" not in result
    assert "hi" in result


@pytest.mark.parametrize(
    "name", ["bad.odt", "bad.ods", "bad.odp", "bad.epub", "bad.numbers", "bad.xls", "bad.eml"]
)
def test_a_broken_file_of_each_new_format_gives_no_text(project, name):
    path = DocumentFixtures.corrupt(project / name)
    if name == "bad.eml":
        path.write_bytes(b"")

    assert _read(project, name) == f"[No text could be extracted from {path}.]"


# ── wiring ──────────────────────────────────────────────────────────


def test_the_registry_claims_every_added_extension_and_only_iwork_as_bundles():
    readers = DocumentReaders.default()

    assert {
        ".odt",
        ".ods",
        ".odp",
        ".xls",
        ".rtf",
        ".epub",
        ".eml",
        ".ipynb",
        ".numbers",
        ".pages",
        ".key",
        ".doc",
        ".ppt",
    } <= readers.extensions
    assert readers.bundle_extensions == frozenset({".numbers", ".pages", ".key"})


@pytest.mark.parametrize(
    ("name", "write", "expected"),
    [
        ("report.odt", OdfFixtures.odt_report, "# Quarterly report"),
        ("budget.xls", XlsFixtures.budget, "--- Sheet: Budget ---"),
        ("mail.eml", TextFormatFixtures.email, "Subject: Q3 numbers"),
        ("analysis.ipynb", TextFormatFixtures.notebook, "--- Cell 1 (markdown) ---"),
    ],
)
def test_an_attached_file_of_an_added_format_reaches_the_model_as_text(
    tmp_path, name, write, expected
):
    path = write(tmp_path / name)

    message = AttachedDocumentText.collect([str(path)]).wrap("look")

    assert message.startswith(f"<attached-files>\n[Text extracted from {path}")
    assert expected in message
    assert message.endswith("</attached-files>\nlook")


def test_a_refusal_tells_the_model_to_propose_a_reader_and_ask_first(project):
    (project / "drawing.dwg").write_bytes(b"AC1032\0\0binary")
    DocumentFixtures.legacy(project / "old.ppt")

    for name in ("drawing.dwg", "old.ppt"):
        assert _read(project, name).endswith(PROPOSE + "]")


# ── the edges of each reader ────────────────────────────────────────


def test_markup_inside_headings_cells_and_preformatted_text_is_read_as_text(project):
    chapters = {
        "c.xhtml": (
            "<h2>Big <em>news</em>&amp;more<script>x()</script></h2>"
            "<table><tr><td>a<br/>b <!-- note --></td><td>&eacute;t&eacute;<style>p{}</style></td>"
            "</tr></table><pre>keep <b>bold</b>&nbsp;</pre><p>&unknownentity;ok</p>"
        )
    }
    path = TextFormatFixtures.epub(project / "b.epub", chapters)

    assert _text(_read(project, "b.epub"), path, "1 chapter") == (
        "--- Chapter 1 ---\n## Big news&more\n| a b | été |\n|---|---|\nkeep bold\xa0\nok"
    )


def test_a_chapter_that_is_plain_html_or_empty_does_not_stop_the_book(project):
    path = TextFormatFixtures.epub(
        project / "b.epub", {"a.xhtml": "<p>A</p>", "b.xhtml": "<p>B</p>"}
    )
    with zipfile.ZipFile(path) as archive:
        items = {i.filename: archive.read(i) for i in archive.infolist()}
    items["OEBPS/text/a.xhtml"] = b""
    items["OEBPS/text/b.xhtml"] = b"<p>Loose</p><p>HTML</p>"
    with zipfile.ZipFile(path, "w") as archive:
        for filename, data in items.items():
            archive.writestr(filename, data)

    assert _text(_read(project, "b.epub"), path, "2 chapters") == (
        "--- Chapter 1 ---\nLoose\nHTML\n--- Chapter 2 ---"
    )


@pytest.mark.parametrize("content", [b"", b"not xml at all"])
def test_an_odt_whose_content_is_not_xml_gives_no_text(project, content):
    path = project / "empty.odt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", content)

    assert _read(project, "empty.odt") == f"[No text could be extracted from {path}.]"


def test_an_email_with_no_text_part_lists_its_attachments(project):
    message = EmailMessage()
    message["Subject"] = "scan"
    message.add_attachment(b"\x89PNG", maintype="image", subtype="png", filename="scan.png")
    message.add_attachment("plain words", filename="notes.txt", cte="7bit")
    path = project / "scan.eml"
    path.write_bytes(message.as_bytes())

    assert _text(_read(project, "scan.eml"), path, "") == (
        "Subject: scan\n\nAttachments:\n- scan.png (image/png, 4 bytes)\n"
        "- notes.txt (text/plain, 12 bytes)"
    )


def test_an_html_email_with_a_blank_body_has_only_its_headers(project):
    path = project / "blank.eml"
    path.write_text("Subject: blank\nContent-Type: text/html\n\n   \n", encoding="utf-8")

    assert _text(_read(project, "blank.eml"), path, "") == "Subject: blank"


def test_a_notebook_output_that_is_not_an_output_is_skipped(project):
    path = TextFormatFixtures.notebook(
        project / "odd.ipynb",
        [
            {
                "cell_type": "code",
                "source": "x",
                "outputs": ["junk", {"output_type": "stream", "text": "ok"}],
            }
        ],
    )

    assert _text(_read(project, "odd.ipynb"), path, "1 cell") == (
        "--- Cell 1 (code) ---\nx\nOutput:\nok"
    )


def test_an_iwork_bundle_whose_quicklook_is_a_file_has_no_preview(project):
    bundle = project / "talk.key"
    bundle.mkdir()
    (bundle / "QuickLook").write_bytes(b"not a folder")

    assert _read(project, "talk.key").startswith(
        f"[Could not read {bundle}: this Keynote file holds no preview PDF"
    )


def test_an_xlsx_saved_with_an_xls_name_gives_no_text(project):
    path = DocumentFixtures.xlsx_sheets(project / "renamed.xls", {"A": 2})

    assert _read(project, "renamed.xls") == f"[No text could be extracted from {path}.]"


class _Recorder(etree.Resolver):
    """Every external DTD or entity the parser asks for."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []

    def resolve(self, system_url, public_id, context):
        self.asked.append(system_url)
        return self.resolve_string("", context)


def test_the_xml_parser_asks_for_no_external_dtd_or_entity(tmp_path):
    secret = _secret(tmp_path)
    parser = SafeXml._xml_parser()  # noqa: SLF001 — the parser's own settings are what is under test
    recorder = _Recorder()
    parser.resolvers.add(recorder)
    doc = (
        f'<!DOCTYPE d SYSTEM "file://{tmp_path}/outer.dtd" [<!ENTITY x SYSTEM "file://{secret}">]>'
        "<d>&x;</d>"
    ).encode()

    root = etree.fromstring(doc, parser)

    assert recorder.asked == []
    assert "TOPSECRET" not in etree.tostring(root).decode()


def test_text_that_is_not_xml_is_refused_not_parsed_into_nothing():
    with pytest.raises(ValueError):
        SafeXml.xml(b"not xml at all")


def test_trailing_empty_cells_do_not_use_up_the_repeat_budget(project):
    body = (
        '<table:table table:name="Wide">'
        '<table:table-row><table:table-cell office:value-type="string"><text:p>h</text:p></table:table-cell></table:table-row>'
        '<table:table-row table:number-rows-repeated="1000">'
        '<table:table-cell office:value-type="string"><text:p>x</text:p></table:table-cell>'
        '<table:table-cell table:number-columns-repeated="16000"/></table:table-row></table:table>'
    )
    path = OdfFixtures.package(project / "wide.ods", "spreadsheet", body)

    assert _text(_read(project, "wide.ods"), path, "1 sheet").endswith(
        "[Rows truncated at 500 of 1001." + _hint("Wide!501-")
    )


def test_an_xls_whose_container_warns_still_writes_nothing_to_the_console(project):
    path = XlsFixtures.budget(project / "budget.xls")
    path.write_bytes(path.read_bytes() + b"\0" * 3)
    script = (
        "import sys; from pathlib import Path; "
        "from igni_core.core.documents.registry import DocumentReaders; "
        "text = DocumentReaders.default().read(Path(sys.argv[1])); "
        "sys.stderr.write(text.body[:21])"
    )

    run = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True
    )

    assert run.stderr == "--- Sheet: Budget ---"
    assert run.stdout == ""


def test_an_rtf_with_raw_latin_1_bytes_keeps_them(project):
    path = project / "raw.rtf"
    path.write_bytes(b"{\\rtf1\\ansi Caf\xe9 au lait\\par}")

    assert _text(_read(project, "raw.rtf"), path, "") == "Café au lait"


def test_an_epub_chapter_whose_name_is_percent_encoded_is_found(project):
    path = TextFormatFixtures.epub(project / "b.epub", {"my chapter.xhtml": "<p>Spaced</p>"})

    assert _text(_read(project, "b.epub"), path, "1 chapter") == "--- Chapter 1 ---\nSpaced"


def test_an_email_body_with_a_file_name_is_the_body_not_an_attachment(project):
    path = project / "named.eml"
    path.write_text(
        "Subject: named\nContent-Type: text/plain\n"
        'Content-Disposition: inline; filename="body.txt"\n\nThe body.\n',
        encoding="utf-8",
    )

    assert _text(_read(project, "named.eml"), path, "") == "Subject: named\n\nThe body."
