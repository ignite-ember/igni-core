"""A Jupyter notebook, cell by cell: markdown and code, with their text outputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from igni_core.core.documents.budget import ReadBudget
from igni_core.core.documents.notes import DocumentNotes
from igni_core.core.documents.reader import DocumentReader
from igni_core.core.documents.schemas import DocumentText, NotebookCell, NotebookOutput
from igni_core.core.documents.unit_run import UnitRun
from igni_core.core.documents.window import ReadWindow


class IpynbDocumentReader(DocumentReader):
    """Each cell under ``--- Cell n (kind) ---``; images, HTML and other non-text outputs are left out, and said to be."""

    extensions: ClassVar[frozenset[str]] = frozenset({".ipynb"})
    _TEXT_OUTPUT: ClassVar[str] = "text/plain"

    def _read(self, path: Path, budget: ReadBudget, window: ReadWindow) -> DocumentText:
        cells = self._cells(json.loads(path.read_bytes()))
        count = DocumentNotes.counted(len(cells), "cell")
        if window.past(len(cells)):
            raise window.past_end(str(path), count)
        dropped: list[bool] = []
        run = UnitRun.collect(
            len(cells), lambda n: self._cell(n, cells[n - 1], budget, dropped), budget, window
        )
        notes = [DocumentNotes.outputs_dropped()] if any(dropped) else []
        return DocumentText(
            path=str(path),
            count=count,
            body="\n".join(text for text in run.texts if text),
            has_text=any(run.texts),
            notes=notes + budget.notes(window.continuation(budget, run.stopped)),
        )

    @staticmethod
    def _cells(notebook: Any) -> list[NotebookCell]:
        """The cells of a version 4 notebook, or of a version 3 notebook's worksheets; a malformed cell is skipped."""
        if not isinstance(notebook, dict):
            raise ValueError("not a notebook")
        raw = notebook.get("cells")
        if raw is None:
            sheets = notebook.get("worksheets") or []
            raw = [c for ws in sheets if isinstance(ws, dict) for c in ws.get("cells") or []]
        cells: list[NotebookCell] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                cells.append(NotebookCell.model_validate(item))
            except ValidationError:
                continue
        return cells

    def _cell(self, n: int, cell: NotebookCell, budget: ReadBudget, dropped: list[bool]) -> str:
        lines = [f"--- Cell {n} ({cell.cell_type}) ---"]
        if cell.code:
            lines.append(cell.code)
        if cell.attachments:
            dropped.append(True)
        outputs = [self._output(output, dropped) for output in cell.outputs]
        shown = "\n".join(text for text in outputs if text).strip("\n")
        if shown:
            lines.append(f"Output:\n{shown}")
        return budget.take("\n".join(lines))

    def _output(self, raw: Any, dropped: list[bool]) -> str:
        """An output's text; anything else is dropped, and ``dropped`` records it."""
        try:
            output = NotebookOutput.model_validate(raw)
        except ValidationError:
            return ""
        if output.output_type in ("error", "pyerr"):
            return f"{output.ename}: {output.evalue}"
        if not output.data:
            return NotebookCell.joined(output.text)
        if any(mime != self._TEXT_OUTPUT for mime in output.data):
            dropped.append(True)
        return NotebookCell.joined(output.data.get(self._TEXT_OUTPUT, ""))
