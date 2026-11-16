"""The extension contract version igni implements, and the range a manifest declares.

An extension's ``contract`` is the only compatibility axis it has: a range
of contract versions it works against, never an igni version
(``docs/EXTENSIBILITY.md`` §2). One out of range is refused at install,
naming the version and which half is behind (§7) — never "works mostly".

A range is space-separated comparisons, all of which must hold:
``>=1.0 <2``. A bare version (``1.0``) means exactly that version.
Versions are ``major[.minor[.patch]]`` and compare as numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

#: The contract this igni implements; ``igni ext doctor`` prints it.
CONTRACT_VERSION = "1.0"


def _version(text: str) -> tuple[int, int, int]:
    parts = text.split(".")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"{text!r} is not a version (major[.minor[.patch]])")
    numbers = [int(p) for p in parts] + [0] * (3 - len(parts))
    return numbers[0], numbers[1], numbers[2]


@dataclass(frozen=True)
class ContractRange:
    """A parsed ``contract`` range."""

    text: str
    bounds: tuple[tuple[str, tuple[int, int, int]], ...]

    _TERM: ClassVar[re.Pattern[str]] = re.compile(r"^(>=|<=|==|=|>|<)?(.+)$")

    @classmethod
    def parse(cls, text: str) -> ContractRange:
        """Raises ValueError when ``text`` is not a range."""
        terms = text.split()
        if not terms:
            raise ValueError("an empty contract range")
        bounds: list[tuple[str, tuple[int, int, int]]] = []
        for term in terms:
            match = cls._TERM.match(term)
            if match is None:
                raise ValueError(f"{term!r} is not a comparison")
            op = match.group(1) or "=="
            bounds.append(("==" if op == "=" else op, _version(match.group(2))))
        return cls(text=text, bounds=tuple(bounds))

    def admits(self, version: str) -> bool:
        return not self._failing(_version(version))

    def why_not(self, version: str) -> str:
        """Why ``version`` is out of this range, saying which half is behind."""
        failing = self._failing(_version(version))
        if not failing:
            return ""
        if any(op in (">=", ">") for op, _ in failing):
            return (
                f"it needs contract {self.text} and this igni implements {version}: "
                "it is for a newer igni"
            )
        return (
            f"it needs contract {self.text} and this igni implements {version}: "
            "it was written for an older contract; update the extension"
        )

    def _failing(self, have: tuple[int, int, int]) -> list[tuple[str, tuple[int, int, int]]]:
        holds = {
            ">=": lambda b: have >= b,
            ">": lambda b: have > b,
            "<=": lambda b: have <= b,
            "<": lambda b: have < b,
            "==": lambda b: have == b,
        }
        return [(op, bound) for op, bound in self.bounds if not holds[op](bound)]


__all__ = ["CONTRACT_VERSION", "ContractRange"]
