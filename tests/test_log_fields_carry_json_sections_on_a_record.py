"""``LogFields``: models become JSON sections that travel on a log record."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from enum import Enum

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.core.log_fields import LogFields


class _Colour(Enum):
    RED = "red"


class _Audit(BaseModel):
    when: datetime
    colour: _Colour
    tool: str


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_each_model_is_its_json_under_its_keyword_name():
    audit = _Audit(when=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), colour=_Colour.RED, tool="rm")
    fields = LogFields.of(audit=audit)
    assert fields.sections == {
        "audit": {"when": "2026-01-02T03:04:05Z", "colour": "red", "tool": "rm"}
    }
    json.dumps(fields.sections)  # a line writer can serialise it as it is


def test_no_models_is_no_sections():
    assert LogFields.of().sections == {}


def test_fields_travel_through_a_real_logging_call():
    caught: list[logging.LogRecord] = []

    class _Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caught.append(record)

    logger = logging.getLogger("igni_test_log_fields")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(_Catch())
    fields = LogFields.of(audit=_Audit(when=datetime.now(UTC), colour=_Colour.RED, tool="x"))
    logger.info("resume timeline", extra=fields.extra())
    (record,) = caught
    assert LogFields.carried_by(record) is fields


def test_extra_names_the_attribute_the_record_carries_them_on():
    fields = LogFields.of()
    assert fields.extra() == {"fields": fields}


def test_a_record_without_fields_carries_none():
    assert LogFields.carried_by(_record()) is None


@pytest.mark.parametrize("foreign", [{"audit": {}}, "text", 3, None])
def test_a_foreign_fields_attribute_is_ignored(foreign):
    assert LogFields.carried_by(_record(fields=foreign)) is None


def test_the_fields_are_frozen():
    fields = LogFields.of()
    with pytest.raises(ValidationError):
        fields.sections = {"x": 1}  # type: ignore[misc]
