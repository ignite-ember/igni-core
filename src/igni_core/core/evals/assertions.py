"""File-system assertions for eval cases.

Each assertion kind is its own model carrying its data and its check.
``type`` is the discriminator, so a typo in the eval YAML fails
validation at load time, and an assertion reaches :meth:`check` only
as a kind that knows how to evaluate itself.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class FileCheckResult(BaseModel):
    """Result of a single file-assertion check."""

    model_config = ConfigDict(extra="forbid")

    type: str
    passed: bool
    detail: str


class BaseFileAssertion(BaseModel, ABC):
    """One file-system assertion against a case's sandbox.

    A relative ``path`` resolves against the case's ``work_dir`` so the
    assertion checks the sandbox, not the process cwd. Absolute paths
    are honoured as-is.
    """

    type: str
    path: str = ""

    def check(self, work_dir: Path | None = None) -> FileCheckResult:
        """Evaluate the assertion against the resolved path."""
        return self.evaluate(self.resolve(work_dir))

    def resolve(self, work_dir: Path | None) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() or work_dir is None else work_dir / p

    @abstractmethod
    def evaluate(self, path: Path) -> FileCheckResult:
        """Check ``path`` and report the outcome."""

    def result(self, passed: bool, detail: str) -> FileCheckResult:
        return FileCheckResult(type=self.type, passed=passed, detail=detail)


def _search(pattern: str, path: Path) -> bool | str:
    """Whether ``pattern`` matches the text of ``path``.

    Returns the reason as a string when the check cannot be made (a file
    that is not text, an unreadable one, a pattern that is not a valid
    regex), so one bad assertion fails with a message instead of aborting
    the case's whole run.
    """
    try:
        return re.search(pattern, path.read_text()) is not None
    except (OSError, UnicodeDecodeError, re.error) as exc:
        return f"{path} could not be checked: {exc}"


class FileExists(BaseFileAssertion):
    type: Literal["file_exists"] = "file_exists"

    def evaluate(self, path: Path) -> FileCheckResult:
        if path.is_file():
            return self.result(True, f"{path} exists")
        return self.result(False, f"{path} does not exist")


class FileNotExists(BaseFileAssertion):
    type: Literal["file_not_exists"] = "file_not_exists"

    def evaluate(self, path: Path) -> FileCheckResult:
        if not path.exists():
            return self.result(True, f"{path} does not exist")
        return self.result(False, f"{path} exists (expected not to)")


class FileContains(BaseFileAssertion):
    type: Literal["file_contains"] = "file_contains"
    pattern: str = ""

    def evaluate(self, path: Path) -> FileCheckResult:
        if not path.is_file():
            return self.result(False, f"{path} does not exist")
        found = _search(self.pattern, path)
        if isinstance(found, str):
            return self.result(False, found)
        if found:
            return self.result(True, f"{path} contains /{self.pattern}/")
        return self.result(False, f"{path} does not contain /{self.pattern}/")


class FileNotContains(BaseFileAssertion):
    type: Literal["file_not_contains"] = "file_not_contains"
    pattern: str = ""

    def evaluate(self, path: Path) -> FileCheckResult:
        if not path.is_file():
            return self.result(True, f"{path} does not exist (OK for not_contains)")
        found = _search(self.pattern, path)
        if isinstance(found, str):
            return self.result(False, found)
        if found:
            return self.result(False, f"{path} contains /{self.pattern}/ (expected not to)")
        return self.result(True, f"{path} does not contain /{self.pattern}/")


FileAssertion = Annotated[
    FileExists | FileNotExists | FileContains | FileNotContains,
    Field(discriminator="type"),
]

#: Validates one YAML entry into the matching :data:`FileAssertion` kind.
FILE_ASSERTION = TypeAdapter(FileAssertion)
