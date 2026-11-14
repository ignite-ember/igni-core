"""Puts back the loggers a test reconfigured.

Production code configures logging as a process would: ``LlmCallLogger``
gives ``igni_core.llm_calls``, ``httpx`` and ``httpcore`` a file handler and
stops them propagating, ``DebugLogging.enable`` hands them its own, and the
backend's and the eval runners' set-up raise ``igni_core`` to INFO or WARNING.
In one process of sixteen thousand tests, that stayed: a later test reading
DEBUG records through ``caplog`` saw none, and one checking that a fresh
logger has no handler found the previous test's.

:class:`LoggingState` takes the loggers' level, propagation, disabled flag and
handlers before a test and restores them after it, so each test starts from
the logging the process began with.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

#: The loggers igni's own code reconfigures; ``""`` is the root.
WATCHED = ("", "igni_core", "igni_core.llm_calls", "httpx", "httpcore")


class LoggerState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    level: int
    propagate: bool
    disabled: bool
    handlers: list[logging.Handler]

    @classmethod
    def of(cls, logger: logging.Logger) -> LoggerState:
        return cls(
            level=logger.level,
            propagate=logger.propagate,
            disabled=logger.disabled,
            handlers=list(logger.handlers),
        )

    def restore(self, logger: logging.Logger) -> None:
        logger.setLevel(self.level)
        logger.propagate = self.propagate
        logger.disabled = self.disabled
        logger.handlers[:] = self.handlers


class LoggingState(BaseModel):
    loggers: dict[str, LoggerState]
    disable: int

    @classmethod
    def take(cls) -> LoggingState:
        return cls(
            loggers={name: LoggerState.of(logging.getLogger(name)) for name in WATCHED},
            disable=logging.root.manager.disable,
        )

    def restore(self) -> None:
        for name, state in self.loggers.items():
            state.restore(logging.getLogger(name))
        logging.disable(self.disable)
