"""The orphan subsystem's models: what they accept and what they default to."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from igni_core.core.tools.orphan_schemas import OrphanProcStub, OrphanReadResult, RehydrateResult


def test_a_stub_has_an_int_pid_and_no_return_code_by_default():
    stub = OrphanProcStub(pid=7)

    assert (stub.pid, stub.returncode) == (7, None)
    assert OrphanProcStub(pid=7, returncode=-1).returncode == -1


@pytest.mark.parametrize("pid", ["not-a-pid", None, 1.5])
def test_a_stub_refuses_a_pid_that_is_not_an_integer(pid):
    with pytest.raises(ValidationError):
        OrphanProcStub(pid=pid)


def test_a_stub_refuses_a_return_code_that_is_not_an_integer():
    with pytest.raises(ValidationError):
        OrphanProcStub(pid=1, returncode="soon")


def test_a_read_result_is_real_output_unless_it_says_otherwise():
    assert OrphanReadResult(content="x").is_placeholder is False
    assert OrphanReadResult(content="x", is_placeholder=True).is_placeholder is True
    with pytest.raises(ValidationError):
        OrphanReadResult()


def test_a_rehydrate_result_is_a_success_with_nothing_surfaced_by_default():
    result = RehydrateResult()

    assert (result.ok, result.surfaced, result.pruned, result.reason) == (True, 0, 0, "")


def test_a_failed_rehydrate_result_carries_its_reason():
    result = RehydrateResult(ok=False, reason="list_all: db locked")

    assert (result.ok, result.reason) == (False, "list_all: db locked")
