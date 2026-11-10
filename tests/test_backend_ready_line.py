"""The ready line's bytes: the first thing a backend prints, read by its parent.

The desktop shell's parser lives outside this repository and reads the line
as bytes, so the separators ``json.dumps`` writes (``": "`` and ``", "``)
and the key order are the contract, not just the JSON value.
"""

from __future__ import annotations

import pytest

from igni_core.backend.schemas_rpc import BackendReadyLine


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            BackendReadyLine(ws_port=4321, ws_url="ws://127.0.0.1:4321"),
            '{"status": "ready", "ws_port": 4321, "ws_url": "ws://127.0.0.1:4321"}\n',
        ),
        (
            BackendReadyLine(socket="/tmp/igni.sock"),
            '{"status": "ready", "socket": "/tmp/igni.sock"}\n',
        ),
        (
            BackendReadyLine(ws_port=4321, ws_url="ws://127.0.0.1:4321", socket="/tmp/igni.sock"),
            '{"status": "ready", "ws_port": 4321, "ws_url": "ws://127.0.0.1:4321", '
            '"socket": "/tmp/igni.sock"}\n',
        ),
    ],
    ids=["websocket", "socket", "both"],
)
def test_the_ready_line_keeps_its_bytes(line, expected, capsys):
    line.emit()
    assert capsys.readouterr().out == expected
