"""Backend process entry point.

Usage: python -m igni_core.backend --socket <path>

Starts a :class:`BackendApp`, listens on the given Unix socket
and/or WebSocket port, and processes FE messages until shutdown.
The real work lives in :mod:`igni_core.backend.app` — this module
is the CLI + the module-load side effects (logging config).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import click

from igni_core.backend.app import BackendApp
from igni_core.backend.logging_setup import configure_logging
from igni_core.backend.schemas_lifecycle import BackendLaunch
from igni_core.core.data_paths import canonical_project_dir
from igni_core.core.layer import layers
from igni_core.transport.ws_auth import take_window_token

logger = logging.getLogger(__name__)

#: The interpreter this ships on. Kept in step with ``requires-python``
#: in pyproject.toml and ``PYTHON_VERSION`` in the Tauri runtime, which
#: is what actually installs it for a user.
SUPPORTED_PYTHON = (3, 12)


def warn_if_unsupported_python(emit: Callable[..., None] = logger.warning) -> bool:
    """Say so, loudly, when the backend is running off-version.

    The reason this is worth a check rather than a note in a README: the
    failure is silent. On 3.14 a Neo4j sidecar the backend used to run
    exited without writing stderr, without a log, and without raising —
    the feature behind it simply never came up, and the panel said
    "disabled" as though someone had configured it that way. Two hours
    went into that before the interpreter turned out to be the variable.

    A warning rather than a refusal. Contributors run the test suite on
    whatever they have, and most of the codebase is fine there; the part
    that is not now announces itself.

    Returns True when the version is the supported one.
    """
    actual = sys.version_info[:2]
    if actual == SUPPORTED_PYTHON:
        return True
    emit(
        "running on Python %d.%d; this backend is supported on %d.%d only. "
        "Some subsystems fail silently off-version.",
        *actual,
        *SUPPORTED_PYTHON,
    )
    return False


@click.command()
@click.option("--socket", "socket_path", default=None, help="Unix socket path")
@click.option(
    "--ws-port",
    "ws_port",
    type=int,
    default=None,
    help="Listen on a loopback WebSocket port instead of a Unix socket "
    "(0 = auto-assign; the bound port is printed in the ready line). "
    "Used by GUI clients (the Tauri desktop app, the web client).",
)
@click.option("--project-dir", type=click.Path(exists=True), default=".")
@click.option("--resume-session", "resume_session_id", default=None)
@click.option("--additional-dirs", multiple=True, default=())
@click.option("--debug", is_flag=True, default=False)
def main(
    socket_path: str | None,
    ws_port: int | None,
    project_dir: str,
    resume_session_id: str | None,
    additional_dirs: tuple[str, ...],
    debug: bool,
) -> None:
    """Start the igni backend server."""
    if socket_path is None and ws_port is None:
        raise click.UsageError("at least one of --socket or --ws-port is required")
    # Always. ``--debug`` is unreachable for the backend the desktop
    # app spawns (the app builds the argv), and the env var that
    # replaced it still had to be set by someone who already suspected
    # a problem and knew its name — which is nobody, before the fact.
    # ``--debug`` / ``IGNI_DEBUG_LOG`` now choose the level.
    log_path = configure_logging(debug_flag=debug)
    if log_path is not None:
        logger.info("backend log: %s", log_path)

    warn_if_unsupported_python()

    # Taken out of the environment at once, before anything is spawned
    # that would inherit it (shell tools, hooks, MCP servers).
    # A backend started by hand has no launcher to give it one, so on a
    # desktop it mints its own and says so once (the cloud's gateway
    # authenticates every client; there the backend takes no token).
    ws_token, minted = take_window_token(
        mint=ws_port is not None and layers.current().name == "desktop"
    )
    if minted:
        click.echo(
            f"igni window token for this backend (shown once, never written to disk): {ws_token} "
            "-- a program connects as the window with igni-client's Local(url=..., token=...).",
            err=True,
        )

    extra_dirs = [Path(d) for d in additional_dirs] if additional_dirs else None
    # One spelling of the project dir, shared with ``igni migrate`` and
    # every store, so all of them open the same ``state.db``.
    resolved_project = canonical_project_dir(project_dir)
    app = BackendApp(
        BackendLaunch(
            socket_path=socket_path,
            ws_port=ws_port,
            project_dir=resolved_project,
            resume_session_id=resume_session_id,
            additional_dirs=extra_dirs,
            ws_token=ws_token,
        )
    )
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
