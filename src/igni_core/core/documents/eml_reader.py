"""A saved email: its headers, its attachments by name and size, and its text body."""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from pathlib import Path
from typing import ClassVar

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.exceptions import DocumentRefused
from igni_core.core.documents.html_blocks import HtmlBlocks
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.safe_xml import SafeXml
from igni_core.core.documents.schemas import ContentBlock, DocumentText
from igni_core.core.documents.unit_run import UnitRun
from igni_core.core.documents.window import ReadWindow


class EmlDocumentReader(DocumentReader):
    """An HTML-only body is turned into text; attachments are listed, never decoded; a part counts the body's paragraphs."""

    extensions: ClassVar[frozenset[str]] = frozenset({".eml"})
    MAX_EML_BYTES: ClassVar[int] = 50 * 1024 * 1024
    """The parser cannot be stopped midway, so a larger email is not handed to it."""
    MAX_ATTACHMENTS: ClassVar[int] = 100
    MAX_HEADER_CHARS: ClassVar[int] = 4000
    """A longer header is cut, so the body always has room."""
    _HEADERS: ClassVar[tuple[str, ...]] = ("From", "To", "Cc", "Date", "Subject")
    _PARAGRAPH: ClassVar[re.Pattern[str]] = re.compile(r"\n[ \t]*\n")
    _SPACE: ClassVar[re.Pattern[str]] = re.compile(r"\s+")

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        if path.stat().st_size > self.MAX_EML_BYTES:
            raise DocumentRefused(DocumentNotes.too_large(str(path)))
        with path.open("rb") as handle:
            message = BytesParser(policy=policy.default).parse(handle)
        body_part = message.get_body(preferencelist=("plain", "html"))
        blocks = self._body(body_part)
        if window.past(len(blocks)):
            raise window.past_end(str(path), DocumentNotes.counted(len(blocks), "paragraph"))
        head = [budget.take(self._headers(message))]
        head.append(self._attachments(message, body_part, budget))
        run = UnitRun.collect(
            len(blocks), lambda n: blocks[n - 1].render(budget, self.MAX_ROWS), budget, window
        )
        texts = [text for text in head + run.texts if text]
        return DocumentText(
            path=str(path),
            body="\n\n".join(texts),
            has_text=bool(texts),
            notes=budget.notes(window.continuation(budget, run.stopped)),
        )

    def _headers(self, message: EmailMessage) -> str:
        lines = []
        for name in self._HEADERS:
            value = message.get(name)
            if value is not None and str(value).strip():
                text = self._SPACE.sub(" ", str(value)).strip()
                lines.append(f"{name}: {text[: self.MAX_HEADER_CHARS]}")
        return "\n".join(lines)

    def _body(self, part: Message | None) -> list[ContentBlock]:
        """The body's paragraphs: a plain body split at blank lines, an HTML one by its blocks."""
        if not isinstance(part, EmailMessage):
            return []
        try:
            text = part.get_content()
        except LookupError:
            text = bytes(part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_subtype() == "html":
            return HtmlBlocks.of(SafeXml.html(text)) if text.strip() else []
        paragraphs = self._PARAGRAPH.split(text.replace("\r\n", "\n"))
        return [ContentBlock(text=p.strip("\n")) for p in paragraphs if p.strip()]

    def _attachments(self, message: EmailMessage, body: Message | None, budget: ReadBudget) -> str:
        found = [part for part in self._parts(message) if part is not body and self._attached(part)]
        if not found:
            return ""
        lines = ["Attachments:"]
        for part in found[: self.MAX_ATTACHMENTS]:
            name = part.get_filename() or "(no name)"
            size = DocumentNotes.counted(self._size(part), "byte")
            lines.append(f"- {name} ({part.get_content_type()}, {size})")
        if len(found) > self.MAX_ATTACHMENTS:
            lines.append(DocumentNotes.parts_cut("Attachments", self.MAX_ATTACHMENTS, len(found)))
        return budget.take("\n".join(lines))

    def _parts(self, part: Message) -> list[Message]:
        """Every leaf part, an attached email counting as one leaf, its own parts unopened."""
        if not part.is_multipart() or part.get_content_type() == "message/rfc822":
            return [part]
        payload = part.get_payload()
        children = payload if isinstance(payload, list) else []
        return [leaf for child in children for leaf in self._parts(child)]

    @staticmethod
    def _attached(part: Message) -> bool:
        return part.get_content_disposition() == "attachment" or part.get_filename() is not None

    def _size(self, part: Message) -> int:
        """The attachment's decoded size, worked out from its encoded length, never decoding it."""
        payload = part.get_payload()
        if isinstance(payload, list):
            return sum(len(item.as_bytes()) for item in payload if isinstance(item, Message))
        raw = payload if isinstance(payload, str) else ""
        if part.get("Content-Transfer-Encoding", "").strip().lower() == "base64":
            compact = self._SPACE.sub("", raw)
            return max(len(compact) * 3 // 4 - compact[-2:].count("="), 0)
        return len(raw.encode("utf-8", errors="replace"))
