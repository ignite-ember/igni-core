"""Keeps a model from reading the content of the media files its turn names but it cannot see.

Told that vision is disabled, a model may still try: ``cat`` on the PNG,
``base64`` of it, ``tesseract``. This guard refuses those calls with the
file's metadata. It is deliberately narrow and deterministic: it knows only
the media files named in the current turn, matches them by file name and
media extension, and looks only at a read tool's path or at the commands
in a shell line that dump or OCR a file. It does not read scripts.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import PurePath
from typing import Any, ClassVar

from igni_core.core.config.permission_eval.catalog import BashCommand
from igni_core.core.utils.media_schemas import MediaFileMetadata


class TextOnlyMediaGuard:
    """The media files of a turn the model cannot see, and the calls that would read them."""

    #: The model-facing refusal, followed by the metadata lines.
    REFUSAL: ClassVar[str] = (
        "Not run: vision is disabled for this model; only the metadata below is available."
    )
    #: The refusal for a file a vision model could not be sent, followed by the metadata lines.
    UNATTACHED_REFUSAL: ClassVar[str] = (
        "Not run: this media file could not be attached; only the metadata below is available."
    )
    #: Tools that return a file's content, by the path they are given.
    READ_TOOLS: ClassVar[frozenset[str]] = frozenset({"read_file", "read_file_chunk"})
    #: The argument names a read tool's path arrives under.
    PATH_ARG_KEYS: ClassVar[tuple[str, ...]] = ("file_path", "file_name", "path")
    #: The shell tool.
    SHELL_TOOL: ClassVar[str] = "run_shell_command"
    #: Commands that dump, encode or OCR a file's bytes.
    READERS: ClassVar[frozenset[str]] = frozenset(
        {
            "cat",
            "head",
            "tail",
            "less",
            "more",
            "bat",
            "xxd",
            "hexdump",
            "od",
            "base64",
            "strings",
            "tesseract",
        }
    )
    #: Words that run the command after them.
    _PREFIXES: ClassVar[frozenset[str]] = frozenset({"sudo", "command", "env", "time", "nice"})
    #: Shell operators that start a new command.
    _SEPARATORS: ClassVar[frozenset[str]] = frozenset({"|", "||", "&&", ";", "&", "|&", "(", ")"})

    def __init__(self) -> None:
        self._unseen: list[MediaFileMetadata] = []
        self._unattached: list[MediaFileMetadata] = []

    @property
    def files(self) -> list[MediaFileMetadata]:
        """The media files the current turn named that the model cannot see."""
        return [*self._unseen, *self._unattached]

    def begin_turn(
        self,
        unseen: Sequence[MediaFileMetadata],
        unattached: Sequence[MediaFileMetadata] = (),
    ) -> None:
        """Start a turn guarding a text-only model's ``unseen`` and a vision model's ``unattached`` files."""
        self._unseen = list(unseen)
        self._unattached = list(unattached)

    def refusal(self, tool_name: str, args: dict[str, Any]) -> str | None:
        """The refusal for a call that would read a guarded file's content, or ``None``."""
        if tool_name in self.READ_TOOLS:
            operands = [self._read_path(args)]
        elif tool_name == self.SHELL_TOOL:
            operands = self._reader_operands(BashCommand.from_args(args).command)
        else:
            return None
        lines = [
            *self._refusal_lines(self.REFUSAL, self._matching(self._unseen, operands)),
            *self._refusal_lines(
                self.UNATTACHED_REFUSAL, self._matching(self._unattached, operands)
            ),
        ]
        return "\n".join(lines) if lines else None

    @staticmethod
    def _refusal_lines(refusal: str, matched: Sequence[MediaFileMetadata]) -> list[str]:
        """``refusal`` and the metadata line of each matched file, or nothing when none matched."""
        return [refusal, *(f.describe() for f in matched)] if matched else []

    def _read_path(self, args: dict[str, Any]) -> str:
        """The path a read tool was given, or ``""``."""
        for key in self.PATH_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    @staticmethod
    def _matching(
        files: Sequence[MediaFileMetadata], operands: Sequence[str]
    ) -> list[MediaFileMetadata]:
        """The ``files`` the operands name: same file name, so same media extension."""
        names = {PurePath(op).name for op in operands if op}
        return [f for f in files if f.name in names]

    def _reader_operands(self, command: str) -> list[str]:
        """Every word of each command in ``command`` whose program is a reader."""
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return []
        operands: list[str] = []
        segment: list[str] = []
        for token in [*tokens, ";"]:
            if token not in self._SEPARATORS:
                segment.append(token)
                continue
            if self._runs_reader(segment):
                for word in segment:
                    operands.extend({word, word.split("=", 1)[-1]})
            segment = []
        return operands

    def _runs_reader(self, segment: list[str]) -> bool:
        """Whether the command in ``segment`` runs one of :attr:`READERS`."""
        for word in segment:
            if "=" in word and not word.startswith("-") and "/" not in word.split("=", 1)[0]:
                continue
            if word in self._PREFIXES:
                continue
            return PurePath(word).name in self.READERS
        return False
