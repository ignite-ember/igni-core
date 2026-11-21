"""Entry point for `igni` and `python -m igni_core`."""

import io
import sys

from igni_core.cli import cli
from igni_core.core.dir_migration import migrate_home


def tolerate_narrow_consoles() -> None:
    """Let what igni prints reach a console that cannot spell all of it.

    igni's output is written in UTF-8 punctuation (``→``, ``—``, ``…``).
    A Windows console's code page is cp1252 or narrower, and a stream
    that encodes strictly raised ``UnicodeEncodeError`` on the first such
    character, so ``igni ext init`` died after writing its files. A
    character the console has no byte for now prints as ``?``. Only a
    strict stream changes: a stream with another handler keeps it, such
    as POSIX's ``surrogateescape`` for undecodable file names or one
    named in ``PYTHONIOENCODING`` (``cp1252:backslashreplace``).
    """
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and stream.errors == "strict":
            stream.reconfigure(errors="replace")


def main():
    tolerate_narrow_consoles()
    # Before anything reads the home directory. The rename is
    # idempotent and costs two stat calls once it has happened, and
    # ``paths.home_config_dir`` falls back to the legacy name anyway —
    # so a path that somehow skips this still works. Doing it here means
    # it happens once, early, rather than being something every reader
    # has to remember.
    migrate_home()
    cli()


if __name__ == "__main__":
    main()
