"""Debug logging bootstrap.

Owns the :class:`RotatingFileHandler` lifecycle and the
``logging.root`` mutation sequence in one place so the CLI
callback doesn't reach into the root logger inline. Bootstrap is
still module-mutating by nature — you can't add a handler to the
root logger without touching global state — but wrapping it in a
class gives the mutation a single named owner (grep for
``DebugLogging.enable`` and you'll find every path that flips
root-logger state on the CLI startup path).
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from igni_core.core.layer import layers
from igni_core.core.paths import CONFIG_DIR


class DebugLogging:
    """Bootstrap for the ``igni --debug`` file-log surface."""

    DEFAULT_PATH = Path.home() / CONFIG_DIR / "debug.log"
    _FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
    _MAX_BYTES = 10_000_000
    _BACKUPS = 2

    @classmethod
    def enable(cls, path: Path | None = None) -> Path:
        """Attach a rotating file handler to the root logger and
        return the log-file path.

        Called from the CLI callback when ``--debug`` is passed.
        Replacing the root logger's handler list (rather than
        appending) matches the pre-refactor behavior — the debug
        session wants a clean slate so noisy imports don't pollute
        the trace.
        """
        log_path = path if path is not None else cls.DEFAULT_PATH
        handler: logging.Handler
        if not layers.current().log_files():
            # No debug.log where the layer keeps no log files.
            handler = layers.current().log_handler()
        else:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                str(log_path),
                maxBytes=cls._MAX_BYTES,
                backupCount=cls._BACKUPS,
            )
            handler.setFormatter(logging.Formatter(cls._FORMAT))

        logging.root.handlers.clear()
        logging.root.addHandler(handler)
        logging.root.setLevel(logging.DEBUG)
        logging.getLogger("igni_core").setLevel(logging.DEBUG)

        # ``igni_core.llm_calls`` and the transport loggers deliberately do not
        # propagate — they write request/response bodies, and letting those reach
        # root put them on stderr (see ``LlmCallLogger._attach_transport_loggers``).
        # A --debug run still wants them in one place, so hand them the handler
        # directly instead of reopening the propagation path. They stop
        # propagating here too: until the first model call configures them,
        # a record would reach the handler twice, once on the logger and once
        # through root.
        for name in ("igni_core.llm_calls", "httpx", "httpcore"):
            named = logging.getLogger(name)
            named.addHandler(handler)
            named.propagate = False

        return log_path


class AgnoLogs:
    """Agno's own loggers, sent to stderr for a headless run.

    Agno gives its loggers (``agno``, ``agno-team``, ``agno-workflow``) a
    handler of their own that writes to stdout — a rich console on a
    terminal, a plain stream otherwise — and stops them propagating. On
    ``igni -m`` and ``igni -p`` stdout is the reply, so a tool the model
    named that the run did not offer printed ``ERROR   Function save_file
    not found`` above it, and a failed turn printed agno's "Error in Agent
    run: …" there as well as igni's own report. What agno logs is still
    said, on stderr.
    """

    LOGGERS = ("agno", "agno-team", "agno-workflow")
    _FORMAT = "%(levelname)-7s %(message)s"

    @classmethod
    def to_stderr(cls) -> None:
        """Replace each agno logger's handlers with one that writes to stderr."""
        for name in cls.LOGGERS:
            agno_logger = logging.getLogger(name)
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(cls._FORMAT))
            agno_logger.handlers = [handler]
