"""Small real documents for the reader tests, written with the libraries that read them."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import docx
import openpyxl
import pptx
from pptx.util import Inches


class DocumentFixtures:
    """Writers for one small file of each format, and the broken ones."""

    CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    @staticmethod
    def csv_commas(path: Path) -> Path:
        """Two columns, one quoted cell holding a comma."""
        path.write_text('name,city\nAnna,Kyiv\nBob,"Amsterdam, NL"\n', encoding="utf-8")
        return path

    @staticmethod
    def csv_semicolons(path: Path) -> Path:
        """A European export: semicolons, decimal commas, Windows-1252."""
        path.write_bytes("naam;bedrag\nRené;1,50\nBob;2,75\n".encode("cp1252"))
        return path

    @staticmethod
    def csv_rows(path: Path, rows: int) -> Path:
        """A header and ``rows - 1`` numbered rows."""
        lines = ["n,square"] + [f"{i},{i * i}" for i in range(1, rows)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @classmethod
    def xlsx_two_sheets(cls, path: Path) -> Path:
        """A ``Budget`` sheet whose total is a formula saved as 9999, and a ``Notes`` sheet."""
        workbook = openpyxl.Workbook()
        budget = workbook.active
        assert budget is not None
        budget.title = "Budget"
        budget.append(["Item", "Cost"])
        budget.append(["Rent", 1200])
        budget.append(["Food", 300.5])
        budget.append(["Total", "=SUM(B2:B3)"])
        notes = workbook.create_sheet("Notes")
        notes["A1"] = "Reviewed by Anna"
        buffer = io.BytesIO()
        workbook.save(buffer)
        return cls._with_cached_formula(buffer.getvalue(), path)

    @staticmethod
    def _with_cached_formula(raw: bytes, path: Path) -> Path:
        """The workbook with ``9999`` saved as its formula's last value, as Excel would."""
        source = zipfile.ZipFile(io.BytesIO(raw))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
            for item in source.infolist():
                data = source.read(item)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b"<v></v>", b"<v>9999</v>")
                    assert b"<v>9999</v>" in data
                target.writestr(item, data)
        return path

    @staticmethod
    def docx_report(path: Path, *paragraphs: str) -> Path:
        """A title, a heading, a paragraph, a table, a closing paragraph, then ``paragraphs``."""
        document = docx.Document()
        document.add_heading("Quarterly report", 0)
        document.add_heading("Summary", 1)
        document.add_paragraph("Revenue grew.")
        table = document.add_table(rows=2, cols=2)
        for (r, c), text in {
            (0, 0): "Quarter",
            (0, 1): "Revenue",
            (1, 0): "Q1",
            (1, 1): "10",
        }.items():
            table.cell(r, c).text = text
        document.add_heading("Details", 2)
        document.add_paragraph("After the table.")
        for text in paragraphs:
            document.add_paragraph(text)
        document.save(str(path))
        return path

    @staticmethod
    def pptx_deck(path: Path) -> Path:
        """Two slides: a title with text and speaker notes, then a title with a table."""
        deck = pptx.Presentation()
        first = deck.slides.add_slide(deck.slide_layouts[1])
        assert first.shapes.title is not None
        first.shapes.title.text = "Roadmap"
        first.placeholders[1].text = "Ship the readers"
        notes_frame = first.notes_slide.notes_text_frame
        assert notes_frame is not None
        notes_frame.text = "Mention the caps"
        second = deck.slides.add_slide(deck.slide_layouts[5])
        assert second.shapes.title is not None
        second.shapes.title.text = "Numbers"
        table = second.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
        for (r, c), text in {
            (0, 0): "Metric",
            (0, 1): "Value",
            (1, 0): "Users",
            (1, 1): "42",
        }.items():
            table.cell(r, c).text = text
        deck.save(str(path))
        return path

    @classmethod
    def legacy(cls, path: Path) -> Path:
        """A file that starts like an OLE compound document: ``.xls``, ``.doc``, ``.ppt``."""
        path.write_bytes(cls.CFB_MAGIC + b"\0" * 504)
        return path

    @classmethod
    def password_protected(cls, path: Path) -> Path:
        """An encrypted Office file: a compound document carrying an ``EncryptionInfo`` stream."""
        path.write_bytes(cls.CFB_MAGIC + b"\0" * 56 + "EncryptionInfo".encode("utf-16-le"))
        return path

    @staticmethod
    def corrupt(path: Path) -> Path:
        """Bytes no parser accepts."""
        path.write_bytes(b"PK\x03\x04 this is not really a zip archive")
        return path
