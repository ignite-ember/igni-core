"""Wire schemas for the composer-paste ``search_code`` RPC.

Extracted from :mod:`igni_core.backend.server_search` to match
the sibling ``schemas_*.py`` convention used across ``backend/``
(``schemas_files``, ``schemas_panels``, ``schemas_context`` …).

Wire schemas (RPC-returned Pydantic models):

* :class:`SearchCodeMatch` — one hit row shipped to the FE.
* :class:`SearchCodeResult` — the wire envelope. Carries
  ``matches`` / ``truncated`` / ``error``, plus two behaviour
  methods that keep the truncation + timeout logic in one place:

    - :meth:`SearchCodeResult.timed_out` (classmethod) — the
      canonical timeout payload; used by
      :class:`RgSearchStrategy` when ``rg`` blows past the
      subprocess timeout.
    - :meth:`SearchCodeResult.append` — push a match and return
      ``True`` when the ``max_results`` cap is hit. Both search
      strategies use it so the "append then check the cap and
      flip ``truncated``" branch exists in exactly one place.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchCodeMatch(BaseModel):
    """One place in the project where the searched text occurs."""

    path: str = Field(description="File path, relative to the project root.")
    line: int = Field(description="1-based line where the match starts.")
    end_line: int = Field(description="1-based line where the match ends (inclusive).")
    preview: str = Field(description="The first matching line, trimmed of surrounding whitespace.")


class SearchCodeResult(BaseModel):
    """Where a piece of text (e.g. pasted code) occurs in the project.

    Text shorter than 5 characters is not searched and gives an empty
    result."""

    matches: list[SearchCodeMatch] = Field(
        default=[], description="The matches found, up to the limit."
    )
    truncated: bool = Field(
        default=False,
        description="`true` when the result limit was hit or the search timed out, so there may be more matches.",
    )
    error: str = Field(
        default="", description="Why the search failed, e.g. `search timed out`; empty on success."
    )

    @classmethod
    def timed_out(cls) -> SearchCodeResult:
        """Canonical "search timed out" payload — used by strategies
        that can hit a hard timeout (currently
        :class:`RgSearchStrategy`) so the wire string lives in one
        place."""
        return cls(matches=[], truncated=True, error="search timed out")

    def append(self, match: SearchCodeMatch, max_results: int) -> bool:
        """Push ``match`` onto :attr:`matches` and return ``True``
        once ``max_results`` is reached.

        Also flips :attr:`truncated` when the cap is hit — the
        strategy loop should ``break`` (or ``return``) as soon as
        this returns True. Consolidates the append-then-check
        pattern that used to be duplicated in both
        :class:`RgSearchStrategy` and :class:`PythonWalkSearchStrategy`.
        """
        self.matches.append(match)
        if len(self.matches) >= max_results:
            self.truncated = True
            return True
        return False
