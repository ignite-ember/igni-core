"""@file mention processing — shared between FE and BE runners."""

from __future__ import annotations

import re
from pathlib import PurePath

from igni_core.core.utils.media_schemas import MediaTaxonomy

_AT_MENTION_RE = re.compile(r"(?:^|(?<=\s))@(\S+)")


def process_file_mentions(text: str, *, vision: bool = True) -> tuple[str, list[str]]:
    """Pick out @file mentions and surface them in a hint block.

    Returns (cleaned_text, referenced_paths). NOTE: the ``@<path>``
    tokens stay in the body — they're not stripped. The literal
    token is what the rendered user bubble shows inline (live AND
    restored), so the user can see their reference. On top of that,
    an ``<attached-files>`` wrapper is prepended carrying the
    extracted path list — the agent reads that to decide which files
    to actually open. The web FE strips the wrapper from the
    displayed bubble (and from restored history), so what the user
    sees stays clean: just the prompt they typed plus the inline
    ``@<path>`` reference.

    Email-style ``user@domain`` is NOT a mention — the regex
    requires the ``@`` to be preceded by whitespace or
    start-of-string.

    With ``vision`` false, image, audio and video mentions are left out of
    the "read before responding" line: a text-only model cannot read them,
    and :class:`~igni_core.core.utils.text_only_media.TextOnlyMediaNotice`
    adds their metadata to the block instead. They stay in the returned paths.
    """
    paths: list[str] = []

    def _collect(m: re.Match) -> str:
        paths.append(m.group(1))
        # Preserve the literal @<path> token so the rendered bubble
        # (live AND restored) shows the user's reference inline.
        # The wrapper below tells the agent to actually read the
        # files — both representations stay in the message.
        return m.group(0)

    cleaned = _AT_MENTION_RE.sub(_collect, text)

    taxonomy = MediaTaxonomy.default()
    readable = (
        paths if vision else [p for p in paths if not taxonomy.needs_vision(PurePath(p).suffix)]
    )
    if readable:
        hint = (
            "<attached-files>\n"
            "[Referenced files: " + ", ".join(readable) + " — read before responding]\n"
            "</attached-files>"
        )
        cleaned = hint + ("\n" + cleaned if cleaned else "")

    return cleaned, paths
