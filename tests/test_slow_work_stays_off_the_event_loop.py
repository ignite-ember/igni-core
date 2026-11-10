"""A code search and an upload run off the event loop.

``search_code`` ran ``rg`` (or a walk of the tree, for up to ten
seconds) and ``upload_attachment`` decoded and wrote a whole file on the
event loop, holding up every session while they did.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from igni_core.backend.schemas_search import SearchCodeResult
from igni_core.backend.server_files import FilesController
from igni_core.backend.server_search import SearchController


async def test_a_code_search_runs_in_a_worker_thread(tmp_path: Path) -> None:
    seen: list[threading.Thread] = []

    class Strategy:
        def search(self, root: Path, snippet: str, lines: int, limit: int) -> SearchCodeResult:
            seen.append(threading.current_thread())
            return SearchCodeResult()

    controller = SearchController(
        SimpleNamespace(project_dir=tmp_path),  # type: ignore[arg-type]
        strategy=Strategy(),  # type: ignore[arg-type]
    )

    await controller.search_code("def something_long")

    assert seen and seen[0] is not threading.current_thread()


async def test_an_upload_is_written_in_a_worker_thread(tmp_path: Path) -> None:
    seen: list[threading.Thread] = []
    stored = SimpleNamespace(ref="attachment:1", size=3)

    class Store:
        def save(self, session_id: str, filename: str, chunks: object) -> object:
            seen.append(threading.current_thread())
            return stored

    controller = FilesController.__new__(FilesController)
    controller._session = SimpleNamespace(session_id="s-1")  # type: ignore[attr-defined]
    controller._attachments = lambda: Store()  # type: ignore[method-assign]

    result = await controller.upload_attachment("a.txt", "YWJj")

    assert result.path == "attachment:1"
    assert seen and seen[0] is not threading.current_thread()
