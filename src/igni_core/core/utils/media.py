"""Media input coordinator — detect file paths and resolve references.

Two modes, both active. Which one runs depends on the model's
``vision`` flag in the registry (see ``backend/server.py:409``):

1. **Path resolution** — resolves bare filenames and relative paths to
   absolute paths so a text-only model can read them via Bash / cat /
   pdftotext. The path lands in the user message as a string.
2. **Media attachment** — for ``vision: true`` models only. Wraps the
   resolved paths in Agno ``Image`` / ``Audio`` / ``Video`` / ``File``
   objects which Agno serializes as base64-encoded content parts in
   the provider's chat-completions request body (OpenAI ``image_url``,
   Claude ``document``, etc.). The actual file bytes go on the wire
   on every turn that includes the attachment — see
   :meth:`MediaResolver.attach_local_paths` for the entry point.

A text-only model (e.g. MiniMax-M2.7) takes path 1 only; the bytes
stay on disk in ``.igni/attachments/<session_id>/``.

**Which files may be sent.** A path typed in short (relative, ``~``,
a bare filename the resolver found) is attachable as before. A path
written out in full — every upload, which the composer names by its
absolute ``@<path>``, and every file a service's item brings — is
attachable only when it resolves, symlinks followed, inside an allowed
root: the project's uploads directory or the workspace (the project
directory). Any other absolute path stays text: the model can still
read it with its tools, under their permissions, but its bytes are not
put on the wire because a message named it.
"""

from __future__ import annotations

import re
from pathlib import Path

from igni_core.core.documents.registry import DocumentReaders
from igni_core.core.utils.attachment_store import Attachments
from igni_core.core.utils.media_schemas import (
    MediaAttachments,
    MediaTaxonomy,
    ResolvedText,
)


class MediaResolver:
    """Turn free-form user text into resolved absolute paths and typed
    :class:`MediaAttachments` bundles.

    Constructor takes ``project_dir`` (the primary search location for
    bare filenames) plus an optional :class:`MediaTaxonomy` (tests
    can substitute a narrower one) and optional ``search_dirs``
    override (defaults preserve the pre-refactor behavior: project
    first, then Downloads / Desktop / Documents / home). Optional
    ``allowed_roots`` are where a path written out in full must lie to
    be attached; by default the project's uploads directory and the
    project itself, and none without a project.

    The compiled regexes are built once per instance from the
    taxonomy's ``all_extensions`` — cheap enough that per-turn
    construction from PromptBuilder / runner / interactive_loop is
    fine. A future hoisting onto Session would be a signature-stable
    optimization if profiling ever asks for it.
    """

    # Default search directories for bare filenames. Instance-level
    # attribute so a caller can override without patching a module
    # constant.
    _DEFAULT_SEARCH_DIRS: tuple[Path, ...] = (
        Path.home() / "Downloads",
        Path.home() / "Desktop",
        Path.home() / "Documents",
        Path.home(),
    )

    def __init__(
        self,
        project_dir: Path | None = None,
        *,
        taxonomy: MediaTaxonomy | None = None,
        search_dirs: tuple[Path, ...] | None = None,
        allowed_roots: tuple[Path, ...] | None = None,
        document_extensions: frozenset[str] | None = None,
    ) -> None:
        self._project_dir = project_dir
        self._taxonomy = taxonomy or MediaTaxonomy.default()
        self._search_dirs = search_dirs if search_dirs is not None else self._DEFAULT_SEARCH_DIRS
        if allowed_roots is None:
            allowed_roots = (Attachments.directory(project_dir), project_dir) if project_dir else ()
        self._allowed_roots = tuple(root.expanduser().resolve() for root in allowed_roots)

        # Build regex alternation from all known extensions
        # (without the dot). Compiled per instance so a custom
        # taxonomy in a test surfaces the right alt set.
        ext_alt = "|".join(ext.lstrip(".") for ext in sorted(self._taxonomy.all_extensions))
        if document_extensions is None:
            document_extensions = DocumentReaders.default().extensions
        # A document is found and attached like media, and read as text.
        file_exts = self._taxonomy.all_extensions | document_extensions
        file_alt = "|".join(ext.lstrip(".") for ext in sorted(file_exts))
        # ``(?![.\w])`` after the extension: the URL ends *at* the
        # extension. Without it ``photo.tiff`` matched as ``photo.tif``
        # (the shorter extension first) and ``page.png.html`` matched
        # as ``page.png`` — both put a URL on the wire that names a
        # file that does not exist.
        self._url_pattern = re.compile(
            rf"(https?://\S+/\S+\.(?:{ext_alt})(?![.\w])(?:\?\S*)?)", re.IGNORECASE
        )
        # Explicit paths (starts with ~, ., /, or drive letter), optionally
        # as an ``@`` mention — how the composer names every upload. The
        # whitespace around a path is looked at, not consumed, so two
        # side by side (``@/a.png @/b.png``) are both found.
        self._file_pattern = re.compile(
            rf"(?:^|(?<=\s))@?((?:[~/.]|\w:)[^\s]*\.(?:{file_alt}))(?=\s|$)",
            re.IGNORECASE,
        )
        # Bare filenames without a path prefix ("photo.avif", "report.pdf"),
        # optionally as an ``@`` mention.
        self._bare_file_pattern = re.compile(
            rf"(?:^|(?<=\s))@?([^\s/~.@][^\s]*\.(?:{file_alt}))(?=\s|$)",
            re.IGNORECASE,
        )

    # ── Path resolution ───────────────────────────────────────────────

    def _find_bare_file(self, filename: str) -> Path | None:
        """Search common directories for a bare filename.

        Checks the project directory first, then Downloads, Desktop,
        Documents, and home. Returns the first match or None.
        """
        candidates: list[Path] = []
        if self._project_dir:
            candidates.append(self._project_dir)
        candidates.extend(self._search_dirs)

        for directory in candidates:
            candidate = directory / filename
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _allowed(self, path: Path) -> bool:
        """Whether ``path`` (already resolved) lies in an allowed root."""
        return any(path.is_relative_to(root) for root in self._allowed_roots)

    def resolve_text_references(self, text: str) -> ResolvedText:
        """Resolve file references in user text to absolute paths.

        Replaces bare filenames and relative paths with their resolved
        absolute paths so the AI can use Read or other tools on them.

        A path typed in short (relative, ``~``, a bare filename) is
        :attr:`ResolvedText.attachable` once found, as it always was. A
        path the text spells out in full — an upload's ``@<path>`` — is
        attachable only when it resolves inside an allowed root; one
        outside stays text only.

        Explicit paths are processed before bare filenames so a
        message like ``check ./photo.png and report.pdf`` resolves
        deterministically when both syntaxes appear.

        Returns a :class:`ResolvedText` — the rewritten text, the
        paths it names, the paths that may be attached, and the bare
        filenames it could not find.
        """
        resolved: list[str] = []
        attachable: dict[str, None] = {}
        missed: list[str] = []

        # Each match is rewritten where it stands. A global replace per
        # match rewrote a name inside a path already written out, so a
        # name matched twice came out as ``/p//p/b.png``.
        def _explicit(match: re.Match[str]) -> str:
            raw = match.group(1)
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                return match.group(0)
            # Written out in full, it must lie in an allowed root — the
            # resolved path, so neither ``..`` nor a symlink leaves one.
            if not Path(raw).is_absolute() or self._allowed(path):
                attachable[str(path)] = None
            if str(path) == raw:
                return match.group(0)
            resolved.append(str(path))
            return self._rewrite(match, str(path))

        def _bare(match: re.Match[str]) -> str:
            filename = match.group(1)
            found = self._find_bare_file(filename)
            if found is None:
                missed.append(filename)
                return match.group(0)
            resolved.append(str(found))
            attachable[str(found)] = None
            return self._rewrite(match, str(found))

        # Explicit paths (~/..., ./..., /..., C:\...), then bare filenames.
        text = self._file_pattern.sub(_explicit, text)
        text = self._bare_file_pattern.sub(_bare, text)

        return ResolvedText(text=text, paths=resolved, attachable=list(attachable), missed=missed)

    @staticmethod
    def _rewrite(match: re.Match[str], path: str) -> str:
        """The matched text with its file reference replaced by ``path``."""
        whole = match.group(0)
        return (
            whole[: match.start(1) - match.start(0)] + path + whole[match.end(1) - match.start(0) :]
        )

    def locate(self, reference: str) -> str | None:
        """The absolute path one file reference resolves to, as :meth:`resolve_text_references` finds it."""
        explicit = self._file_pattern.fullmatch(reference)
        if explicit is not None:
            path = Path(explicit.group(1)).expanduser().resolve()
            return str(path) if path.is_file() else None
        bare = self._bare_file_pattern.fullmatch(reference)
        if bare is not None:
            found = self._find_bare_file(bare.group(1))
            return str(found) if found is not None else None
        return None

    # ── Media attachment (vision models) ──────────────────────────────

    def attach_local_paths(self, paths: list[str]) -> MediaAttachments | None:
        """Convert resolved file paths to a typed
        :class:`MediaAttachments` for vision models.

        Returns ``None`` if no attachable media was found so the
        caller can decide "text-only path" versus "attach and
        announce". The Agno container class is the taxonomy row's
        (:meth:`MediaKindSpec.build`) — no if/elif ladder — and the
        attachment goes into the bucket that row names, on the
        bundle itself.
        """
        attachments = MediaAttachments()

        for path_str in paths:
            p = Path(path_str)
            spec = self._taxonomy.by_extension.get(p.suffix.lower())
            if spec is None:
                continue
            attachments.add(spec.bucket, spec.build(filepath=p))
            attachments.local_paths.append(path_str)

        if attachments.is_empty():
            return None
        return attachments

    def extract_url_media(self, text: str) -> MediaAttachments | None:
        """Extract media URLs from text and return as a typed
        :class:`MediaAttachments` for vision-capable models.

        Uses the same taxonomy dispatch as
        :meth:`attach_local_paths` — a new kind added to the
        taxonomy participates in both local and remote paths
        automatically.
        """
        attachments = MediaAttachments()

        for match in self._url_pattern.finditer(text):
            url = match.group(1)
            path_part = url.split("?")[0]
            ext = Path(path_part).suffix.lower()
            # The pattern was built from the taxonomy's own
            # extensions, so the extension it matched always has a
            # row — a KeyError here would mean the two drifted apart.
            spec = self._taxonomy.by_extension[ext]
            attachments.add(spec.bucket, spec.build(url=url))

        if attachments.is_empty():
            return None
        return attachments
