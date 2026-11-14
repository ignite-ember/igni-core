"""igni's prompt files, read through the table of anchors.

A prompt is read from a file and then has the anchors an extension
supplies put into it. Both steps belong to one object: the files are
igni's own, the table of anchors is every extension's, and a prompt
that is read without its anchors filled is a prompt whose owner never
got to speak.
"""

from __future__ import annotations

from pathlib import Path

from igni_core.core.prompts.fragments import PromptFragments

#: Where igni's own prompt files live.
PROMPTS_DIR = Path(__file__).parent


class PromptLoader:
    """igni's prompt files, and the anchors filled into them.

    One instance per process: the files are the same for every session,
    and so is the table of anchors an extension fills, so a prompt reads
    the same wherever it is built.
    """

    def __init__(self, fragments: PromptFragments, directory: Path | None = None) -> None:
        """
        Args:
            fragments: The table of anchors an extension fills.
            directory: Where the prompt files are. Igni's own by
                default; a test may point it elsewhere.
        """
        self._fragments = fragments
        self._directory = directory if directory is not None else PROMPTS_DIR

    @property
    def fragments(self) -> PromptFragments:
        """The table of anchors an extension fills."""
        return self._fragments

    def load(self, name: str) -> str:
        """The prompt file ``name``, with the anchors it has filled.

        Raises ``FileNotFoundError`` when there is no such file.
        """
        path = self._directory / f"{name}.md"
        return self.expand(path.read_text(encoding="utf-8").strip())

    def expand(self, text: str) -> str:
        """``text``, with the anchors this process's extensions fill."""
        return self._fragments.expand(text)


__all__ = ["PROMPTS_DIR", "PromptLoader"]
