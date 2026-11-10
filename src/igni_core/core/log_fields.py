"""Structured fields a log record carries beside its message.

A layer that writes logs as JSON lines (:meth:`igni_core.core.layer.Layer.log_handler`;
the cloud's :mod:`igni_cloud.logs`) writes each section as a key of its
line: the audit log's ``audit``, a resume's ``timeline``. The desktop's
log files ignore them.

A record carries them as its ``fields`` attribute, set through
``extra``::

    logger.info("resume timeline", extra=LogFields.of(timeline=record).extra())
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, JsonValue


class LogFields(BaseModel):
    """Named JSON sections, each a key of the record's line."""

    model_config = ConfigDict(frozen=True)

    #: The record attribute the fields travel on.
    ATTRIBUTE: ClassVar[str] = "fields"

    sections: dict[str, JsonValue]

    @classmethod
    def of(cls, **models: BaseModel) -> LogFields:
        """Each model, as its JSON, under its name."""
        return cls(sections={name: model.model_dump(mode="json") for name, model in models.items()})

    @classmethod
    def carried_by(cls, record: logging.LogRecord) -> LogFields | None:
        """The fields ``record`` carries, if any."""
        fields = getattr(record, cls.ATTRIBUTE, None)
        return fields if isinstance(fields, cls) else None

    def extra(self) -> dict[str, LogFields]:
        """What a logging call takes as ``extra`` to carry these fields."""
        return {self.ATTRIBUTE: self}


__all__ = ["LogFields"]
