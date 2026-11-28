"""Small real files of the formats added after Office: OpenDocument, .xls, RTF, EPUB, email, notebooks, iWork."""

from __future__ import annotations

import json
import math
import struct
import zipfile
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape

from tests.document_fixtures import DocumentFixtures

_ODF_NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"'
)
_MIMES = {
    "text": "application/vnd.oasis.opendocument.text",
    "spreadsheet": "application/vnd.oasis.opendocument.spreadsheet",
    "presentation": "application/vnd.oasis.opendocument.presentation",
}


class OdfFixtures:
    """OpenDocument packages laid out as LibreOffice writes them: ``mimetype`` first and stored."""

    @staticmethod
    def package(
        path: Path, kind: str, body: str, *, encrypted: bool = False, prolog: str = ""
    ) -> Path:
        """An ODF zip whose ``office:<kind>`` holds ``body``."""
        content = (
            f'<?xml version="1.0" encoding="UTF-8"?>{prolog}'
            f'<office:document-content {_ODF_NS} office:version="1.3">'
            f"<office:body><office:{kind}>{body}</office:{kind}></office:body>"
            "</office:document-content>"
        )
        encryption = (
            '<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="x"/>'
            if encrypted
            else ""
        )
        manifest = (
            f'<?xml version="1.0" encoding="UTF-8"?><manifest:manifest {_ODF_NS}>'
            f'<manifest:file-entry manifest:full-path="/" manifest:media-type="{_MIMES[kind]}"/>'
            f'<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml">'
            f"{encryption}</manifest:file-entry></manifest:manifest>"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", _MIMES[kind], compress_type=zipfile.ZIP_STORED)
            archive.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("META-INF/manifest.xml", manifest, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("Basic/Standard/Module1.xml", '<script>Shell("rm -rf /")</script>')
        return path

    @classmethod
    def odt_report(cls, path: Path, *paragraphs: str) -> Path:
        """A title, headings, a paragraph with spaces and a tab, a list, a table with a footnote, then ``paragraphs``."""
        extra = "".join(f"<text:p>{escape(p)}</text:p>" for p in paragraphs)
        body = (
            '<text:sequence-decls><text:sequence-decl text:name="Table"/></text:sequence-decls>'
            '<text:p text:style-name="Title">Quarterly report</text:p>'
            '<text:h text:outline-level="1">Summary</text:h>'
            '<text:p>Revenue<text:s text:c="2"/>grew<text:tab/>fast'
            '<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation>'
            "<text:note-body><text:p>A footnote.</text:p></text:note-body></text:note>.</text:p>"
            "<text:list><text:list-item><text:p>First point</text:p></text:list-item>"
            "<text:list-item><text:p>Second <text:span>point</text:span></text:p></text:list-item></text:list>"
            '<table:table table:name="T1"><table:table-column table:number-columns-repeated="2"/>'
            "<table:table-header-rows><table:table-row>"
            "<table:table-cell><text:p>Quarter</text:p></table:table-cell>"
            "<table:table-cell><text:p>Revenue</text:p></table:table-cell>"
            "</table:table-row></table:table-header-rows>"
            "<table:table-row><table:table-cell><text:p>Q1</text:p></table:table-cell>"
            "<table:table-cell><text:p>10 | 12</text:p></table:table-cell></table:table-row>"
            "</table:table>"
            '<text:h text:outline-level="2">Details</text:h>'
            "<text:p>After<text:line-break/>the table."
            "<office:annotation><text:p>A reviewer comment.</text:p></office:annotation></text:p>"
            f"{extra}"
        )
        return cls.package(path, "text", body)

    @classmethod
    def odt_paragraphs(cls, path: Path, paragraphs: list[str]) -> Path:
        """A text document of ``paragraphs`` and nothing else."""
        return cls.package(
            path, "text", "".join(f"<text:p>{escape(p)}</text:p>" for p in paragraphs)
        )

    @staticmethod
    def _cell(value: str, repeat: int = 1) -> str:
        attr = f' table:number-columns-repeated="{repeat}"' if repeat > 1 else ""
        if not value:
            return f"<table:table-cell{attr}/>"
        return f'<table:table-cell office:value-type="string"{attr}><text:p>{escape(value)}</text:p></table:table-cell>'

    @classmethod
    def ods_budget(cls, path: Path) -> Path:
        """A ``Budget`` sheet whose total is a formula saved as 9999, a merged cell, trailing repeats, and a ``Notes`` sheet."""
        budget = (
            '<table:table table:name="Budget">'
            '<table:table-column table:number-columns-repeated="1024"/>'
            f"<table:table-row>{cls._cell('Item')}{cls._cell('Cost')}{cls._cell('', 1022)}</table:table-row>"
            f"<table:table-row>{cls._cell('Rent')}"
            '<table:table-cell office:value-type="float" office:value="1200"><text:p>1200</text:p></table:table-cell>'
            f"{cls._cell('', 1022)}</table:table-row>"
            '<table:table-row table:number-rows-repeated="2">'
            f"{cls._cell('Food')}{cls._cell('300.5')}</table:table-row>"
            f"<table:table-row>{cls._cell('Total')}"
            '<table:table-cell table:formula="of:=SUM([.B2:.B4])" office:value-type="float" office:value="9999">'
            "<text:p>9999</text:p></table:table-cell></table:table-row>"
            f'<table:table-row table:number-rows-repeated="1048570"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
            "</table:table>"
        )
        notes = (
            '<table:table table:name="Notes"><table:table-row>'
            '<table:table-cell table:number-columns-spanned="2"><text:p>Reviewed by Anna</text:p></table:table-cell>'
            f"<table:covered-table-cell/>{cls._cell('Done')}</table:table-row></table:table>"
        )
        return cls.package(path, "spreadsheet", budget + notes)

    @classmethod
    def ods_sheets(cls, path: Path, sheets: dict[str, int]) -> Path:
        """One sheet per name, each a header and ``rows - 1`` numbered rows."""
        body = ""
        for name, rows in sheets.items():
            lines = [f"<table:table-row>{cls._cell('n')}{cls._cell(name)}</table:table-row>"]
            lines += [
                f"<table:table-row>{cls._cell(str(i))}{cls._cell(f'{name}-{i}')}</table:table-row>"
                for i in range(1, rows)
            ]
            body += f'<table:table table:name="{escape(name)}">{"".join(lines)}</table:table>'
        return cls.package(path, "spreadsheet", body)

    @classmethod
    def ods_repeat_bomb(cls, path: Path) -> Path:
        """A sheet whose one non-empty row claims a billion repeats of a billion columns."""
        body = (
            '<table:table table:name="Bomb"><table:table-row table:number-rows-repeated="1000000000">'
            f"{cls._cell('x', 1000000000)}{cls._cell('y')}</table:table-row></table:table>"
        )
        return cls.package(path, "spreadsheet", body)

    @classmethod
    def odp_deck(cls, path: Path) -> Path:
        """Two slides: a title with text and speaker notes, then a title with a table, plus an image."""

        def frame(cls_: str, text: str) -> str:
            return (
                f'<draw:frame presentation:class="{cls_}"><draw:text-box>'
                f"<text:p>{escape(text)}</text:p></draw:text-box></draw:frame>"
            )

        first = (
            '<draw:page draw:name="page1">'
            f"{frame('title', 'Roadmap')}{frame('outline', 'Ship the readers')}"
            '<draw:frame><draw:image xlink:href="Pictures/1.png" xmlns:xlink="http://www.w3.org/1999/xlink"/></draw:frame>'
            '<presentation:notes><draw:page-thumbnail presentation:class="page"/>'
            f"{frame('notes', 'Mention the caps')}</presentation:notes></draw:page>"
        )
        second = (
            '<draw:page draw:name="page2">'
            f"{frame('title', 'Numbers')}"
            "<draw:g><draw:frame><table:table>"
            f"<table:table-row>{cls._cell('Metric')}{cls._cell('Value')}</table:table-row>"
            f"<table:table-row>{cls._cell('Users')}{cls._cell('42')}</table:table-row>"
            "</table:table></draw:frame></draw:g></draw:page>"
        )
        return cls.package(path, "presentation", first + second)

    @classmethod
    def odp_titles(cls, path: Path, count: int) -> Path:
        """``count`` slides titled ``Slide 1`` to ``Slide count``."""
        pages = "".join(
            f'<draw:page><draw:frame presentation:class="title"><draw:text-box>'
            f"<text:p>Slide {n}</text:p></draw:text-box></draw:frame></draw:page>"
            for n in range(1, count + 1)
        )
        return cls.package(path, "presentation", pages)


class XlsFixtures:
    """Legacy Excel files: a BIFF8 workbook stream inside an OLE compound file, as Excel 97-2003 saves them."""

    _SECTOR = 512
    _FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD

    @staticmethod
    def _record(kind: int, data: bytes = b"") -> bytes:
        return struct.pack("<HH", kind, len(data)) + data

    @classmethod
    def _bof(cls, kind: int) -> bytes:
        return cls._record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 6))

    @staticmethod
    def _unicode(text: str, length_bytes: int) -> bytes:
        size = struct.pack("<B" if length_bytes == 1 else "<H", len(text))
        return size + b"\x01" + text.encode("utf-16-le")

    @classmethod
    def _xf(cls, number_format: int, style: bool) -> bytes:
        kind = 0xFFF5 if style else 0x0000
        return cls._record(
            0x00E0, struct.pack("<HHHBBBBIIH", 0, number_format, kind, 0x20, 0, 0, 0, 0, 0, 0x20C0)
        )

    @classmethod
    def _cells(cls, rows: list[list[object]]) -> bytes:
        out = b""
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if value is None:
                    continue
                if isinstance(value, bool):
                    out += cls._record(0x0205, struct.pack("<HHHBB", r, c, 15, int(value), 0))
                elif isinstance(value, tuple) and value[0] == "error":
                    out += cls._record(0x0205, struct.pack("<HHHBB", r, c, 15, int(value[1]), 1))
                elif isinstance(value, tuple):
                    out += cls._record(0x0203, struct.pack("<HHHd", r, c, 16, float(value[1])))
                elif isinstance(value, int | float):
                    out += cls._record(0x0203, struct.pack("<HHHd", r, c, 15, float(value)))
                else:
                    out += cls._record(
                        0x0204, struct.pack("<HHH", r, c, 15) + cls._unicode(str(value), 2)
                    )
        return out

    @classmethod
    def workbook(cls, sheets: dict[str, list[list[object]]], *, encrypted: bool = False) -> bytes:
        """The BIFF8 ``Workbook`` stream; a ``("date", serial)`` cell is a date, a bool a TRUE/FALSE cell."""
        head = cls._bof(0x0005)
        if encrypted:
            head += cls._record(0x002F, b"\x01\x00" + b"\x00" * 52)
        head += cls._record(0x0042, struct.pack("<H", 1200)) + cls._record(
            0x0022, struct.pack("<H", 0)
        )
        font = cls._record(
            0x0031,
            struct.pack("<HHHHHBBBB", 200, 0, 0x7FFF, 400, 0, 0, 0, 0, 0)
            + cls._unicode("Arial", 1),
        )
        head += font * 5
        head += (
            b"".join(cls._xf(0, True) for _ in range(15)) + cls._xf(0, False) + cls._xf(14, False)
        )
        bodies = [
            cls._bof(0x0010)
            + cls._record(
                0x0200,
                struct.pack("<IIHHH", 0, len(rows), 0, max((len(r) for r in rows), default=0), 0),
            )
            + cls._cells(rows)
            + cls._record(0x000A)
            for rows in sheets.values()
        ]
        names = [
            cls._record(0x0085, struct.pack("<IBB", 0, 0, 0) + cls._unicode(n, 1)) for n in sheets
        ]
        offset = len(head) + sum(len(n) for n in names) + 4
        sheet_records = b""
        for name, body in zip(sheets, bodies, strict=True):
            sheet_records += cls._record(
                0x0085, struct.pack("<IBB", offset, 0, 0) + cls._unicode(name, 1)
            )
            offset += len(body)
        return head + sheet_records + cls._record(0x000A) + b"".join(bodies)

    @classmethod
    def compound(cls, stream: bytes, name: str = "Workbook") -> bytes:
        """An OLE compound file (version 3) holding one stream, kept out of the mini stream."""
        stream = stream.ljust(max(len(stream), 4096), b"\0")
        data_sectors = math.ceil(len(stream) / cls._SECTOR)
        fat_sectors = 1
        while fat_sectors * 128 < fat_sectors + 1 + data_sectors:
            fat_sectors += 1
        fat = [cls._FAT] * fat_sectors + [cls._END]
        first = fat_sectors + 1
        fat += [first + i + 1 for i in range(data_sectors - 1)] + [cls._END]
        fat += [cls._FREE] * (fat_sectors * 128 - len(fat))
        difat = list(range(fat_sectors)) + [cls._FREE] * (109 - fat_sectors)
        header = (
            DocumentFixtures.CFB_MAGIC
            + b"\0" * 16
            + struct.pack("<HHHHH", 0x003E, 0x0003, 0xFFFE, 9, 6)
            + b"\0" * 6
            + struct.pack(
                "<IIIIIIIII", 0, fat_sectors, fat_sectors, 0, 4096, cls._END, 0, cls._END, 0
            )
            + struct.pack("<109I", *difat)
        )

        def entry(label: str, kind: int, child: int, start: int, size: int) -> bytes:
            raw = (label + "\0").encode("utf-16-le") if label else b""
            return (
                raw.ljust(64, b"\0")
                + struct.pack("<HBBIII", len(raw), kind, 1, cls._FREE, cls._FREE, child)
                + b"\0" * 16
                + struct.pack("<I", 0)
                + b"\0" * 16
                + struct.pack("<IQ", start, size)
            )

        directory = (
            entry("Root Entry", 5, 1, cls._END, 0)
            + entry(name, 2, cls._FREE, first, len(stream))
            + entry("", 0, cls._FREE, 0, 0) * 2
        )
        fat_bytes = struct.pack(f"<{len(fat)}I", *fat)
        return header + fat_bytes + directory + stream.ljust(data_sectors * cls._SECTOR, b"\0")

    @classmethod
    def budget(cls, path: Path) -> Path:
        """A ``Budget`` sheet with a date, a boolean and a decimal, and a ``Notes`` sheet."""
        sheets = {
            "Budget": [
                ["Item", "Cost", "Paid", "Due"],
                ["Rent", 1200, True, ("date", 46000)],
                ["Food", 300.5, False, None],
            ],
            "Notes": [["Reviewed by Anna"]],
            "Odd": [
                ["Error", "Noon", "Far future", "Gap", "End"],
                [("error", 7), ("date", 46000.5), ("date", 1e12), None, "x"],
            ],
        }
        path.write_bytes(cls.compound(cls.workbook(sheets)))
        return path

    @classmethod
    def sheets(cls, path: Path, sheets: dict[str, int]) -> Path:
        """One sheet per name, each a header and ``rows - 1`` numbered rows."""
        data = {
            name: [["n", name]] + [[i, f"{name}-{i}"] for i in range(1, rows)]
            for name, rows in sheets.items()
        }
        path.write_bytes(cls.compound(cls.workbook(data)))
        return path

    @classmethod
    def encrypted(cls, path: Path) -> Path:
        """A workbook saved with a password: a FILEPASS record right after the first BOF."""
        path.write_bytes(cls.compound(cls.workbook({"S": [["secret"]]}, encrypted=True)))
        return path


class TextFormatFixtures:
    """RTF, EPUB, email, notebook and iWork files."""

    @staticmethod
    def rtf(path: Path, *paragraphs: str) -> Path:
        """A Word-style RTF: a font table, a picture, a hidden field instruction, escapes, then ``paragraphs``."""
        extra = "".join(f"{p}\\par\n" for p in paragraphs)
        path.write_text(
            "{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
            "{\\*\\generator Msftedit 5.41;}\\viewkind4\\uc1\\pard\\f0\\fs20 "
            "{\\b Quarterly report}\\par\n"
            "Ren\\'e9 paid \\u8364?5.\\par\n"
            "{\\pict\\pngblip\\picw10\\pich10 89504e470d0a1a0a0000000d49484452}\\par\n"
            '{\\*\\fldinst HYPERLINK "http://evil.example"}\\par\n'
            "{\\object\\objemb{\\*\\objclass Excel.Sheet.8}{\\*\\objdata 0105000002000000}}\\par\n"
            f"{extra}}}",
            encoding="ascii",
        )
        return path

    @staticmethod
    def epub(
        path: Path, chapters: dict[str, str] | None = None, *, encrypted: tuple[str, ...] = ()
    ) -> Path:
        """A book whose spine reads ``ch2`` before ``ch1``, as the manifest lists them the other way round."""
        chapters = chapters or {
            "ch1.xhtml": (
                "<h1>Part two</h1><p>The second&nbsp;chapter.</p>"
                "<table><tr><th>Name</th><th>Age</th></tr><tr><td>Anna</td><td>30</td></tr></table>"
            ),
            "ch2.xhtml": (
                "<h1>Part one</h1><h2>Opening</h2><p>It began <em>here</em>.<br/>Next line.</p>"
                "<script>alert('x')</script><style>p { color: red }</style>"
                "<ul><li>One</li><li>Two</li></ul><pre>  code\n    block</pre>"
            ),
        }
        manifest = "".join(
            f'<item id="{Path(n).stem.replace(" ", "_")}" href="text/{quote(n)}" media-type="application/xhtml+xml"/>'
            for n in chapters
        )
        spine = "".join(
            f'<itemref idref="{Path(n).stem.replace(" ", "_")}"/>' for n in reversed(list(chapters))
        )
        opf = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Book</dc:title></metadata>'
            f'<manifest><item id="css" href="style.css" media-type="text/css"/>{manifest}</manifest>'
            f"<spine>{spine}</spine></package>"
        )
        container = (
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>"
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container)
            archive.writestr("OEBPS/content.opf", opf)
            archive.writestr("OEBPS/style.css", "body { margin: 0 }")
            for name, body in chapters.items():
                archive.writestr(
                    f"OEBPS/text/{name}",
                    '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html>'
                    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>'
                    f"<body>{body}</body></html>",
                )
            if encrypted:
                refs = "".join(
                    f'<enc:EncryptedData xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:CipherData>'
                    f'<enc:CipherReference URI="OEBPS/text/{n}"/></enc:CipherData></enc:EncryptedData>'
                    for n in encrypted
                )
                archive.writestr(
                    "META-INF/encryption.xml",
                    f'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">{refs}</encryption>',
                )
        return path

    @staticmethod
    def email(path: Path, *, html_only: bool = False, attachments: int = 1, body: str = "") -> Path:
        """A message with encoded headers, a text body (or only HTML) and ``attachments`` PDF attachments."""
        message = EmailMessage()
        message["From"] = "Anna Kovalenko <anna@example.com>"
        message["To"] = "Bob <bob@example.com>, Carol <carol@example.com>"
        message["Cc"] = "=?utf-8?q?Ren=C3=A9?= <rene@example.com>"
        message["Date"] = "Thu, 26 Nov 2026 10:00:00 +0100"
        message["Subject"] = "Q3 numbers"
        text = body or "Hi Bob,\n\nThe numbers are attached.\nSee page 2.\n\nAnna"
        if html_only:
            message.set_content(
                "<html><head><style>p{}</style></head><body><h1>Q3</h1><p>The numbers&nbsp;are "
                "<b>attached</b>.</p><script>steal()</script><table><tr><td>Revenue</td><td>10</td></tr></table>"
                "</body></html>",
                subtype="html",
            )
        else:
            message.set_content(text)
            message.add_alternative(f"<p>{escape(text)}</p>", subtype="html")
        for n in range(attachments):
            message.add_attachment(
                b"%PDF-1.4 secret payload " * 40,
                maintype="application",
                subtype="pdf",
                filename=f"report-{n + 1}.pdf",
            )
        path.write_bytes(message.as_bytes())
        return path

    @staticmethod
    def notebook(path: Path, cells: list[dict[str, object]] | None = None) -> Path:
        """A version 4 notebook: markdown, code with a stream and a plot, an error, a raw cell."""
        cells = cells or [
            {"cell_type": "markdown", "metadata": {}, "source": ["# Analysis\n", "Load the data."]},
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "source": ["x = 41\n", "print(x + 1)"],
                "outputs": [{"output_type": "stream", "name": "stdout", "text": ["42\n"]}],
            },
            {
                "cell_type": "code",
                "execution_count": 2,
                "metadata": {},
                "source": "plot(x)",
                "outputs": [
                    {
                        "output_type": "display_data",
                        "metadata": {},
                        "data": {
                            "image/png": "iVBORw0KGgo=" * 1000,
                            "text/plain": ["<Figure size 640x480>"],
                        },
                    }
                ],
            },
            {
                "cell_type": "code",
                "execution_count": 3,
                "metadata": {},
                "source": "1/0",
                "outputs": [
                    {
                        "output_type": "error",
                        "ename": "ZeroDivisionError",
                        "evalue": "division by zero",
                        "traceback": ["\u001b[0;31m---------\u001b[0m"],
                    }
                ],
            },
            {"cell_type": "raw", "metadata": {}, "source": "raw text"},
        ]
        notebook = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": cells}
        path.write_text(json.dumps(notebook), encoding="utf-8")
        return path

    @staticmethod
    def iwork_zip(
        path: Path, preview: str | None = "QuickLook/Preview.pdf", pages: int = 1
    ) -> Path:
        """An iWork file as a zip, with its preview PDF at ``preview`` or none, as current versions save it."""
        pdf = DocumentFixtures.pdf(
            path.parent / f".{path.name}.preview.pdf",
            [[f"Preview line {n}"] for n in range(1, pages + 1)],
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Index/Document.iwa", b"\x00\x01binary-iwa")
            archive.writestr("preview.jpg", b"\xff\xd8\xff\xe0jpeg")
            if preview is not None:
                archive.write(pdf, preview)
        pdf.unlink()
        return path

    @staticmethod
    def iwork_bundle(path: Path, preview: bool = True) -> Path:
        """An iWork '09 folder bundle, with ``QuickLook/Preview.pdf`` or without."""
        (path / "QuickLook").mkdir(parents=True)
        (path / "index.xml.gz").write_bytes(b"\x1f\x8bgz")
        if preview:
            DocumentFixtures.pdf(path / "QuickLook" / "Preview.pdf", [["Bundle preview"]])
        return path
