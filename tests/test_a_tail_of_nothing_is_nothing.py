"""``read_process_tail`` with ``tail: 0`` answered the whole output.

``output[-0:]`` is every line. A tail below one is refused as an
argument, and a process asked for no lines gives none.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from igni_core.backend.rpc_binding import RpcArgsError
from igni_core.backend.rpc_handlers.panels import PanelRpcHandler
from igni_core.core.tools.managed_process import ManagedProcess
from igni_core.protocol.rpc import RpcMethod


@pytest.mark.parametrize("tail", [0, -3])
def test_a_tail_below_one_is_refused(tail: int) -> None:
    call = PanelRpcHandler(MagicMock()).methods()[RpcMethod.READ_PROCESS_TAIL]
    with pytest.raises(RpcArgsError, match="'tail'"):
        call({"pid": 1, "tail": tail})


def test_a_process_asked_for_no_lines_gives_none() -> None:
    process = ManagedProcess.__new__(ManagedProcess)
    process.output = ["one", "two", "three"]
    process.lock = MagicMock()
    assert process.read(0) == ""
    assert process.read(2) == "two\nthree"
