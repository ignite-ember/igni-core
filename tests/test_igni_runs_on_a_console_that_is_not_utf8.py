"""igni's output reaches a console whose code page cannot spell all of it.

``igni ext init`` died with ``UnicodeEncodeError`` on a Windows console
(cp1252), found by igni-app's enterprise suite (F11, EXT-022 on Windows):
its templates carry ``→``, which it wrote with the platform's default
encoding, and igni prints ``—`` and ``…``, which a strict stream refuses
when its code page has no byte for them. The files are now UTF-8 on every
platform, and a strict console stream replaces what it cannot encode.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest

import igni_core.__main__ as entry


def _console(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors, write_through=True)


def _bytes(stream: io.TextIOWrapper) -> bytes:
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue()


def test_what_igni_prints_reaches_a_cp1252_console(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = _console("cp1252"), _console("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(entry, "migrate_home", lambda: None)

    def _cli() -> None:
        click.echo("next: → then — …")
        click.echo("! → refused", err=True)

    monkeypatch.setattr(entry, "cli", _cli)

    entry.main()

    # cp1252 has a byte for the dash and the ellipsis, none for the arrow.
    assert _bytes(out) == "next: ? then — …\n".encode("cp1252")
    assert _bytes(err) == b"! ? refused\n"


def test_a_stream_with_its_own_error_handler_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX gives a C-locale stdout ``surrogateescape`` so that a file name
    that is not UTF-8 prints as its own bytes; replacing it would lose them."""
    out = _console("ascii", errors="surrogateescape")
    err = _console("cp1252", errors="backslashreplace")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    entry.tolerate_narrow_consoles()

    assert (out.errors, err.errors) == ("surrogateescape", "backslashreplace")


def test_a_stream_that_is_not_a_text_wrapper_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    entry.tolerate_narrow_consoles()

    assert sys.stdout is out


@pytest.mark.parametrize("shape", ["--dev", "--service"])
def test_ext_init_runs_where_neither_the_console_nor_the_locale_is_utf8(
    tmp_path: Path, shape: str
) -> None:
    """The whole command, as a person runs it: the console encodes cp1252
    and the platform's default encoding cannot spell ``→`` (the C locale's
    ASCII, with Python's UTF-8 coercion off, stands in for Windows' cp1252)."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LC_", "PYTHONUTF8", "PYTHONIOENCODING"))
    }
    env.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        LANG="C",
        LC_ALL="C",
        PYTHONCOERCECLOCALE="0",
        PYTHONUTF8="0",
        PYTHONIOENCODING="cp1252",
    )
    target = tmp_path / "hello"

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "igni_core",
            "ext",
            "init",
            str(target),
            shape,
            "--id",
            "acme.hello",
        ],
        env=env,
        capture_output=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr.decode("cp1252", "replace")
    assert b"created " in done.stdout
    written = [path for path in target.rglob("*") if path.is_file()]
    texts = [path.read_bytes().decode("utf-8") for path in written]
    assert any("→" in text for text in texts)
