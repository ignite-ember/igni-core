"""Fuzz the shell classifier plan mode trusts to run a command unasked.

``is_read_only`` is the boundary between "runs without asking" and "put
to the person", so it has to answer — never raise — for any string a
model can write, and its answer has to agree with the splitter the
permission rules use: a command it calls read-only is one that
``command_segments`` can take apart, joined only by pipes, with no
substitution anywhere in it. Pure string functions: nothing here runs
a command, opens a file or touches the network.
"""

from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports(include=["igni_core"]):
    from igni_core.core.config.permission_eval.read_only_shell import (
        command_parts,
        command_segments,
        is_read_only,
    )

_SUBSTITUTION_MARKERS = ("`", "$(", "<(", ">(", "\n", "\r")


def _text(data: bytes) -> str | None:
    """The input as the UTF-8 text a transport or settings file would hold.

    Decoded directly rather than through ``FuzzedDataProvider`` so the seed
    corpus is read as written: the provider spends the first byte choosing
    an encoding, which turns every seed into something else.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def TestOneInput(data: bytes) -> None:
    command = _text(data)
    if command is None:
        return

    read_only = is_read_only(command)
    segments = command_segments(command)
    parts = command_parts(command)

    assert isinstance(read_only, bool)
    if segments is None:
        assert parts is None
    else:
        assert parts == [part for _joiner, part in segments]
    if read_only:
        assert not any(marker in command for marker in _SUBSTITUTION_MARKERS), command
        assert segments is not None, command
        assert all(joiner in (None, "|") for joiner, _part in segments), command


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
