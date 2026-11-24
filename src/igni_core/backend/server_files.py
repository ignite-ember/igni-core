"""File I/O RPCs — sandboxed preview + FE-uploaded attachment save.

:class:`FilesController` is the sole public surface, constructed with
a :class:`Session`.

* :meth:`FilesController.read_file` — sandboxed text file
  preview for the FE.
* :meth:`FilesController.upload_attachment` — persist a
  FE-uploaded file: to a per-session attachments dir on the desktop,
  as a PostgreSQL large object in the cloud
  (:mod:`igni_core.core.utils.attachment_store`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from os.path import expanduser
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from igni_core.core.db.locator import StorageLocator
from igni_core.core.paths import DEFAULT_DATA_DIR
from igni_core.core.utils.attachment_store import (
    AttachmentReferences,
    Attachments,
    Base64Chunks,
)

if TYPE_CHECKING:
    from igni_core.core.session import Session


class ReadFileResult(BaseModel):
    """The contents of a text file, for previewing it.

    Only files inside the project or igni's data folder can be read,
    and large files are refused."""

    path: str = Field(
        description="The file's path (resolved to an absolute path when the read got that far)."
    )
    contents: str = Field(description="The file's text; empty when `error` is set.")
    size: int = Field(description="The file's size in bytes.")
    error: str = Field(default="", description="Why the file could not be read; empty on success.")
    language: str = Field(
        default="",
        description=(
            "Syntax-highlighting language guessed from the file extension "
            "(e.g. `python`); empty if unknown or on error."
        ),
    )


class ReadFileBytesResult(BaseModel):
    """The raw bytes of a file, base64-encoded, for previewing an image
    or a PDF the text reader cannot show.

    Same sandbox as :class:`ReadFileResult`; a larger cap, since an image
    or a PDF is bigger than a source file."""

    path: str = Field(description="The file's path (resolved when the read got that far).")
    data: str = Field(description="The file's bytes, base64-encoded; empty when `error` is set.")
    size: int = Field(description="The file's size in bytes.")
    media_type: str = Field(
        default="",
        description="The file's media type guessed from its extension (e.g. `image/png`); empty if unknown or on error.",
    )
    error: str = Field(default="", description="Why the file could not be read; empty on success.")


class UploadAttachmentResult(BaseModel):
    """Where an uploaded attachment was saved."""

    path: str = Field(
        description=(
            "The stored file's path, or an `attachment:<id>` reference when attachments are "
            "kept in shared storage; use it to refer to the file in a message. Empty on error."
        )
    )
    size: int = Field(description="Size of the stored file in bytes; `0` on error.")
    error: str = Field(
        default="", description="Why the upload failed, e.g. invalid base64; empty on success."
    )


# Cap on ``read_file`` size. The RPC is only invoked by the
# plain-browser fallback preview.
_READ_FILE_MAX_BYTES = 256 * 1024

# Cap on ``read_file_bytes`` size — larger, since it carries an image or
# a PDF rather than source, but still bounded so one read cannot flood
# the socket.
_READ_FILE_BYTES_MAX = 10 * 1024 * 1024


class FilesController:
    """File-I/O RPCs for one :class:`Session`.

    Owns the sandbox-check and language-guess helpers as private
    methods — both are used only from this class, so a module-level
    free-function seam would be pure ceremony.
    """

    # Filename-extension → Prism-compatible highlight hint. Kept on
    # the controller class (not module scope) so behavior + data sit
    # together — a second consumer would trigger a promotion to a
    # dedicated LanguageHints class.
    _LANG_BY_EXT: dict[str, str] = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".json": "json",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".markdown": "markdown",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".html": "html",
        ".css": "css",
        ".sql": "sql",
    }

    # Filename-extension → media type, for the images and PDFs the bytes
    # preview renders inline. Kept beside the controller for the same
    # reason as ``_LANG_BY_EXT``.
    _MEDIA_BY_EXT: dict[str, str] = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".ico": "image/x-icon",
        ".avif": "image/avif",
        ".svg": "image/svg+xml",
        ".pdf": "application/pdf",
    }

    def __init__(self, session: Session) -> None:
        self._session = session

    # ── Sandbox / language helpers ─────────────────────────────────

    @staticmethod
    def _within_root(child: Path, root: Path) -> bool:
        """True iff ``child`` (already resolved) sits under ``root``."""
        try:
            child.relative_to(root)
            return True
        except ValueError:
            return False

    def _allowed_roots(self) -> list[Path]:
        """The directories a preview may read from, resolved.

        Asks where the data actually is rather than naming a constant,
        which is what the constant got wrong twice:

        * ``home_config_dir()`` falls back to the pre-rename ``~/.ember``
          while that is the directory on disk. Hardcoding ``~/.igni``
          meant that on a machine which had not migrated, the app read
          and wrote its config in one place and refused to *preview* it.
        * ``storage.data_dir`` is configurable, and a deployment that
          moves it — onto a bigger volume, say — could not preview
          anything in it.

        ``DEFAULT_DATA_DIR`` stays in the list so this only ever widens
        from where it was, never narrows.

        Widening is safe here for the reason the sandbox works at all:
        the requested path is resolved *before* these comparisons, so
        ``..`` is already collapsed and a symlink has already been
        followed to wherever it really points.
        """
        roots = [Path(self._session.project_dir), Path(expanduser(DEFAULT_DATA_DIR))]

        try:
            from igni_core.core.paths import home_config_dir

            roots.append(home_config_dir())
        except Exception:  # noqa: BLE001, S110 — a preview must not fail on this
            pass

        configured = getattr(getattr(self._session, "settings", None), "storage", None)
        data_dir = getattr(configured, "data_dir", None)
        if data_dir:
            roots.append(Path(expanduser(str(data_dir))))

        resolved: list[Path] = []
        for root in roots:
            try:
                resolved.append(root.resolve())
            except OSError:
                continue
        return resolved

    @classmethod
    def _guess_language(cls, suffix: str) -> str:
        """Return the Prism-compatible language string for a file
        suffix. Unknown extensions collapse to ``""`` so the FE
        renders the file as plain text."""
        return cls._LANG_BY_EXT.get(suffix.lower(), "")

    @classmethod
    def _media_type(cls, suffix: str) -> str:
        """The media type for a file suffix, or ``""`` if unknown."""
        return cls._MEDIA_BY_EXT.get(suffix.lower(), "")

    def _attachments(self) -> Attachments:
        return Attachments.for_project(
            Path(self._session.project_dir), StorageLocator.from_settings(self._session.settings)
        )

    def _resolve_readable(self, path: str) -> tuple[Path | None, str, str]:
        """Resolve a requested path inside the sandbox for a preview.

        Returns ``(path, display, error)``: the resolved file and ``""``
        on success, or ``(None, display, reason)`` when it cannot be
        read. ``display`` is the path to report back — the original for
        an attachment or a bad path, the resolved one otherwise.

        Sandboxed: the resolved path must live under the current project
        dir OR under ``~/.igni``.
        """
        display = path
        if AttachmentReferences.present(path):
            # An upload kept in the database: read through the store.
            local = self._attachments().local_path(path)
            if local is None:
                return None, display, "File not found."
            path = str(local)
        display = path
        try:
            requested = Path(path).expanduser()
            if not requested.is_absolute():
                requested = (self._session.project_dir / requested).resolve()
            else:
                requested = requested.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            # Narrow catch: Path.resolve raises OSError, expanduser can
            # raise RuntimeError when HOME is unresolvable, and malformed
            # inputs surface as ValueError. Anything else should bubble.
            return None, display, f"bad path: {exc}"

        display = str(requested)
        if not any(self._within_root(requested, root) for root in self._allowed_roots()):
            return (
                None,
                display,
                (
                    f"Refused: path is outside the project and {DEFAULT_DATA_DIR}. "
                    "Open it in your editor instead."
                ),
            )
        if not requested.exists():
            return None, display, "File not found."
        if requested.is_dir():
            return None, display, "Path is a directory."
        return requested, display, ""

    def read_file(self, path: str) -> ReadFileResult:
        """Read a small text file for FE preview.

        Sandboxed: the resolved path must live under the current
        project dir OR under ``~/.igni``.
        """
        requested, display, error = self._resolve_readable(path)
        if requested is None:
            return ReadFileResult(path=display, contents="", size=0, error=error)

        try:
            size = requested.stat().st_size
        except OSError as exc:
            # The file went between the checks above and here.
            return ReadFileResult(path=display, contents="", size=0, error=f"read failed: {exc}")
        if size > _READ_FILE_MAX_BYTES:
            return ReadFileResult(
                path=display,
                contents="",
                size=size,
                error=(f"File too large to preview ({size} bytes; cap {_READ_FILE_MAX_BYTES})."),
            )
        try:
            contents = requested.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            return ReadFileResult(path=display, contents="", size=size, error=f"read failed: {exc}")
        return ReadFileResult(
            path=display,
            contents=contents,
            size=size,
            language=self._guess_language(requested.suffix),
        )

    def read_file_bytes(self, path: str) -> ReadFileBytesResult:
        """Read a file's raw bytes (base64) for previewing an image or PDF.

        The same sandbox as :meth:`read_file`, with a larger size cap.
        """
        requested, display, error = self._resolve_readable(path)
        if requested is None:
            return ReadFileBytesResult(path=display, data="", size=0, error=error)

        try:
            size = requested.stat().st_size
        except OSError as exc:
            return ReadFileBytesResult(path=display, data="", size=0, error=f"read failed: {exc}")
        if size > _READ_FILE_BYTES_MAX:
            return ReadFileBytesResult(
                path=display,
                data="",
                size=size,
                error=(f"File too large to preview ({size} bytes; cap {_READ_FILE_BYTES_MAX})."),
            )
        try:
            raw = requested.read_bytes()
        except OSError as exc:
            return ReadFileBytesResult(
                path=display, data="", size=size, error=f"read failed: {exc}"
            )
        return ReadFileBytesResult(
            path=display,
            data=base64.b64encode(raw).decode("ascii"),
            size=size,
            media_type=self._media_type(requested.suffix),
        )

    async def upload_attachment(self, filename: str, content_base64: str) -> UploadAttachmentResult:
        """Persist a FE-uploaded file for this session, decoding and
        writing it a slice at a time — in a worker thread: a large file
        held up every session while it was decoded and written."""
        store = self._attachments()
        try:
            stored = await asyncio.to_thread(
                store.save, self._session.session_id, filename, Base64Chunks(content_base64)
            )
        except (binascii.Error, ValueError) as exc:
            return UploadAttachmentResult(path="", size=0, error=f"invalid base64: {exc}")
        return UploadAttachmentResult(path=stored.ref, size=stored.size)
