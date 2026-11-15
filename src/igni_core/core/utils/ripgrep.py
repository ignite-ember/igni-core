"""Where ``rg`` comes from.

ripgrep is the user's own install first: an ``rg`` on ``PATH`` (Homebrew,
apt, the formula's ``depends_on "ripgrep"``) always wins. The PyPI
``ripgrep`` package is the second source. igni-core requires it only where
it has a wheel (macOS arm64, Linux x86_64) — elsewhere its sdist needs Rust,
and requiring it made ``pip install igni-core`` fail there. When it is
installed, its binary sits in the interpreter's scripts directory, which is
not on ``PATH`` for a ``uv tool`` install or the desktop app's venv.

Two users of that answer:

* :func:`find_rg` — the composer's code search
  (:class:`~igni_core.backend.search_strategies.SearchStrategy`), which runs
  ``rg`` by path and falls back to a walk without it.
* :func:`shell_path` — the agent's shell, where ``rg`` is typed as a
  command. When ``PATH`` has none and the package's binary exists, a
  directory holding only an ``rg`` link to it is appended: appending keeps
  the user's own tools first, and linking ``rg`` alone keeps the venv's
  ``python`` and ``pip`` off the agent's ``PATH``.

With neither, :data:`MISSING` is the one line that says how to install it.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import sysconfig
from functools import lru_cache
from pathlib import Path

from igni_core.core.paths import home_config_dir

logger = logging.getLogger(__name__)

_EXE = "rg.exe" if sys.platform == "win32" else "rg"

#: What the user is told when there is no ``rg`` at all.
MISSING = (
    "ripgrep (rg) is not installed, so code search falls back to a slower "
    "walk and shell commands that run `rg` fail. Install it: "
    "`brew install ripgrep` (macOS), `sudo apt install ripgrep` "
    "(Debian/Ubuntu), `sudo dnf install ripgrep` (Fedora), or "
    "`pip install ripgrep` where PyPI has a wheel for your platform."
)


def bundled_rg() -> Path | None:
    """The PyPI ``ripgrep`` package's binary next to this interpreter, if
    it is installed."""
    candidate = Path(sysconfig.get_path("scripts")) / _EXE
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    return None


def find_rg(path: str | None = None) -> str | None:
    """``rg`` on ``path`` (default: this process's ``PATH``), else the
    package's binary, else ``None``."""
    on_path = shutil.which("rg", path=path)
    if on_path:
        return on_path
    bundled = bundled_rg()
    return str(bundled) if bundled else None


@lru_cache(maxsize=1)
def _link_dir(target: str) -> str | None:
    """A directory under the igni home holding only ``rg`` → ``target``,
    or ``None`` if it cannot be made (a read-only home, no symlinks)."""
    directory = home_config_dir() / "bin"
    link = directory / _EXE
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and os.readlink(link) == target:
            return str(directory)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
    except OSError as exc:
        logger.debug("Could not link the packaged rg into %s: %s", directory, exc)
        return None
    return str(directory)


def shell_path(path: str | None = None) -> str:
    """The ``PATH`` the agent's shell gets: ``path`` (default: this
    process's) as it is when it has ``rg`` or there is no packaged one,
    otherwise with a directory holding only the packaged ``rg`` appended."""
    current = os.environ.get("PATH", "") if path is None else path
    if shutil.which("rg", path=current):
        return current
    bundled = bundled_rg()
    if bundled is None:
        return current
    directory = _link_dir(str(bundled))
    if directory is None:
        return current
    return f"{current}{os.pathsep}{directory}" if current else directory
