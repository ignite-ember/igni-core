"""The handshake guard's own rules, asked directly rather than over a socket.

The guard reads the Origin and the offered subprotocols of an upgrade
request and answers with a standing or a reason. These tests pin its
edges: how the offers are parsed, which comes first, what the reasons say
and what the environment helper does with odd values.
"""

from __future__ import annotations

import logging
import os
from http import HTTPStatus

import pytest
from websockets.datastructures import Headers
from websockets.http11 import Request

from igni_core.protocol.schemas.envelope import Standing
from igni_core.transport.ws_auth import (
    DEV_NO_TOKEN_ENV,
    SUBPROTOCOL,
    TOKEN_ENV,
    TOKEN_FD_ENV,
    HandshakeGuard,
    origin_allowed,
    take_token_from_environment,
    take_window_token,
)

SECRET = "launch-secret-0123456789"


def _request(*protocol_headers: str, origin: str | None = None) -> Request:
    headers = Headers()
    for value in protocol_headers:
        headers["Sec-WebSocket-Protocol"] = value
    if origin is not None:
        headers["Origin"] = origin
    return Request(path="/", headers=headers)


def test_several_protocol_headers_and_comma_lists_are_both_read():
    guard = HandshakeGuard(SECRET)
    assert guard.standing(_request("igni.v1", f"igni-token.{SECRET}")) is Standing.WINDOW
    assert guard.standing(_request(f"igni.v1, igni-token.{SECRET}")) is Standing.WINDOW


def test_any_matching_offer_wins_among_several():
    guard = HandshakeGuard(SECRET)
    request = _request(f"igni-token.nope, igni-token.{guard.program_token}, igni-token.other")
    assert guard.standing(request) is Standing.PROGRAM


def test_a_request_with_no_headers_object_get_all_offers_nothing():
    class Bare:
        def get(self, name):
            return None

    class Req:
        headers = Bare()

    assert HandshakeGuard(SECRET).refusal(Req()) == "no token"


def test_an_empty_offered_token_is_wrong_not_missing():
    assert HandshakeGuard(SECRET).refusal(_request("igni.v1, igni-token.")) == "wrong token"


def test_a_non_ascii_offered_token_is_refused_without_crashing():
    class Plain:
        def get(self, name):
            return None

        def get_all(self, name):
            return ["igni-token.tökén-日本"]

    class Req:
        headers = Plain()

    assert HandshakeGuard(SECRET).refusal(Req()) == "wrong token"


def test_a_wrong_token_of_another_length_is_refused():
    guard = HandshakeGuard(SECRET)
    assert guard.refusal(_request("igni-token.x")) == "wrong token"
    assert guard.refusal(_request("igni-token." + SECRET * 3)) == "wrong token"


def test_no_protocol_header_says_no_token():
    assert HandshakeGuard(SECRET).refusal(_request()) == "no token"


def test_only_the_igni_subprotocol_offered_says_no_token():
    assert HandshakeGuard(SECRET).refusal(_request(SUBPROTOCOL)) == "no token"


def test_an_empty_launch_secret_is_no_token_at_all():
    guard = HandshakeGuard("")
    assert guard.requires_token is False
    assert guard.program_token is None
    assert guard.mint_approver_token() is None
    assert guard.standing(_request()) is Standing.WINDOW


def test_the_guard_with_a_secret_requires_it_and_mints_a_program_token():
    guard = HandshakeGuard(SECRET)
    assert guard.requires_token is True
    assert guard.program_token
    assert guard.program_token != SECRET


def test_a_foreign_origin_is_named_and_judged_before_the_token():
    guard = HandshakeGuard(SECRET)
    reason = guard.refusal(_request(origin="https://evil.example"))
    assert reason == "foreign origin 'https://evil.example'"
    assert guard.standing(_request(origin="https://evil.example")) is None


def test_without_a_secret_a_foreign_origin_is_still_refused():
    assert HandshakeGuard().refusal(_request(origin="null")) == "foreign origin 'null'"


def test_an_origin_padded_with_spaces_is_read_stripped():
    assert origin_allowed("  http://localhost:5179 \n") is True
    guard = HandshakeGuard(SECRET)
    request = _request(f"igni-token.{SECRET}", origin=" http://localhost:5179 ")
    assert guard.standing(request) is Standing.WINDOW


def test_a_refusal_never_contains_either_token():
    guard = HandshakeGuard(SECRET)
    reason = guard.refusal(_request("igni-token.offered-secret-value"))
    assert "offered-secret-value" not in reason
    assert SECRET not in reason


def test_a_minted_token_is_fresh_each_time_and_revoking_counts_them():
    guard = HandshakeGuard(SECRET)
    assert guard.revoke_approver_tokens() == 0
    first, second = guard.mint_approver_token(), guard.mint_approver_token()
    assert first and second and first != second
    assert guard.standing(_request(f"igni-token.{first}")) is Standing.APPROVER
    assert guard.revoke_approver_tokens() == 2
    assert guard.refusal(_request(f"igni-token.{first}")) == "wrong token"
    assert guard.revoke_approver_tokens() == 0
    assert guard.standing(_request(f"igni-token.{SECRET}")) is Standing.WINDOW


class _Connection:
    def __init__(self, remote=("10.0.0.9", 4242), *, has_remote=True):
        if has_remote:
            self.remote_address = remote
        self.responses = []

    def respond(self, status, text):
        self.responses.append((status, text))
        return ("response", status, text)


def test_a_refused_upgrade_is_a_403_forbidden_and_logs_who_and_why(caplog):
    guard = HandshakeGuard(SECRET)
    connection = _Connection()
    with caplog.at_level(logging.WARNING, logger="igni_core.transport.ws_auth"):
        answer = guard.process_request(connection, _request("igni-token.nope"))
    assert answer == ("response", HTTPStatus.FORBIDDEN, "Forbidden\n")
    assert "10.0.0.9" in caplog.text
    assert "wrong token" in caplog.text
    assert "nope" not in caplog.text


def test_an_allowed_upgrade_proceeds_without_a_response():
    connection = _Connection()
    assert (
        HandshakeGuard(SECRET).process_request(connection, _request(f"igni-token.{SECRET}")) is None
    )
    assert connection.responses == []


def test_a_connection_without_a_remote_address_is_still_refused_cleanly():
    answer = HandshakeGuard(SECRET).process_request(_Connection(has_remote=False), _request())
    assert answer == ("response", HTTPStatus.FORBIDDEN, "Forbidden\n")


def test_igni_v1_is_answered_when_offered_and_the_token_never():
    select = HandshakeGuard.select_subprotocol
    assert select(None, ["igni-token.abc", SUBPROTOCOL]) == SUBPROTOCOL
    assert select(None, ["igni-token.abc"]) is None
    assert select(None, ["other.v2"]) is None
    assert select(None, []) is None


def test_a_whitespace_only_token_in_the_environment_is_none_and_removed():
    env = {TOKEN_ENV: "  \t "}
    assert take_token_from_environment(env) is None
    assert TOKEN_ENV not in env


def test_the_token_is_stripped():
    env = {TOKEN_ENV: f"  {SECRET}\n"}
    assert take_token_from_environment(env) == SECRET


@pytest.mark.parametrize("value", ["0", "true", "yes", "", "11"])
def test_a_dev_switch_other_than_one_keeps_the_token(value):
    env = {TOKEN_ENV: SECRET, DEV_NO_TOKEN_ENV: value}
    assert take_token_from_environment(env) == SECRET
    assert TOKEN_ENV not in env


def test_the_dev_switch_drops_a_token_that_was_set_and_still_removes_it(caplog):
    env = {TOKEN_ENV: SECRET, DEV_NO_TOKEN_ENV: " 1 "}
    with caplog.at_level(logging.WARNING, logger="igni_core.transport.ws_auth"):
        assert take_token_from_environment(env) is None
    assert TOKEN_ENV not in env
    assert DEV_NO_TOKEN_ENV in caplog.text
    assert SECRET not in caplog.text


def test_the_names_the_launcher_and_clients_speak_are_igni_s():
    from igni_core.transport import ws_auth

    assert ws_auth.TOKEN_ENV == "IGNI_WS_TOKEN"
    assert ws_auth.DEV_NO_TOKEN_ENV == "IGNI_DEV_NO_WS_TOKEN"
    assert ws_auth.SUBPROTOCOL == "igni.v1"
    assert ws_auth.TOKEN_PROTOCOL_PREFIX == "igni-token."
    assert set(ws_auth.TAURI_ORIGINS) == {
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    }


# ── The token on an inherited descriptor (campaign R622) ─────────────
#
# The environment is readable by any process of the same user (``ps eww``,
# ``/proc/<pid>/environ``); a pipe the launcher writes once is not.


def _pipe_with(text: bytes, *, close: bool = True) -> tuple[int, int]:
    read_end, write_end = os.pipe()
    os.write(write_end, text)
    if close:
        os.close(write_end)
        write_end = -1
    return read_end, write_end


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_the_token_is_read_from_the_descriptor_named_and_it_is_closed():
    read_end, _ = _pipe_with(f"{SECRET}\n".encode())
    env = {TOKEN_FD_ENV: str(read_end)}

    assert take_token_from_environment(env) == SECRET
    assert TOKEN_FD_ENV not in env
    assert not _is_open(read_end)


def test_one_line_is_enough_and_the_launcher_need_not_close_the_pipe():
    read_end, write_end = _pipe_with(f"{SECRET}\nnot read\n".encode(), close=False)
    try:
        assert take_token_from_environment({TOKEN_FD_ENV: str(read_end)}) == SECRET
    finally:
        os.close(write_end)


def test_the_descriptor_wins_over_the_environment_and_both_are_removed():
    read_end, _ = _pipe_with(b"from-the-pipe")
    env = {TOKEN_FD_ENV: str(read_end), TOKEN_ENV: SECRET}

    assert take_token_from_environment(env) == "from-the-pipe"
    assert TOKEN_ENV not in env and TOKEN_FD_ENV not in env


@pytest.mark.parametrize("written", [b"", b"   \n"])
def test_a_descriptor_with_no_token_stops_the_backend(written: bytes):
    read_end, _ = _pipe_with(written)

    with pytest.raises(RuntimeError, match="no token was written"):
        take_token_from_environment({TOKEN_FD_ENV: str(read_end)})
    assert not _is_open(read_end)


@pytest.mark.parametrize("name", ["stdin", "-x"])
def test_a_descriptor_that_is_not_one_stops_the_backend(name: str):
    with pytest.raises(RuntimeError, match=TOKEN_FD_ENV):
        take_token_from_environment({TOKEN_FD_ENV: name})


def test_a_descriptor_that_cannot_be_read_stops_the_backend():
    read_end, _ = _pipe_with(b"x")
    os.close(read_end)

    with pytest.raises(RuntimeError, match="could not be read"):
        take_token_from_environment({TOKEN_FD_ENV: str(read_end)})


def test_without_the_descriptor_the_environment_still_hands_the_token():
    env = {TOKEN_ENV: SECRET}
    assert take_token_from_environment(env) == SECRET
    assert TOKEN_ENV not in env


def test_the_window_token_comes_off_the_descriptor_and_is_not_minted():
    read_end, _ = _pipe_with(f"{SECRET}\n".encode())

    assert take_window_token({TOKEN_FD_ENV: str(read_end)}, mint=True) == (SECRET, False)


def test_stdin_carries_the_token_and_is_left_on_the_null_device():
    """``IGNI_WS_TOKEN_FD=0``: the launcher pipes the token into stdin, the
    one way that works on every platform. Descriptor 0 must not be left
    closed, or the next file opened would become stdin."""
    import subprocess
    import sys

    script = (
        "import os\n"
        "from igni_core.transport.ws_auth import take_token_from_environment\n"
        "print(take_token_from_environment())\n"
        "print(os.read(0, 10) == b'')\n"
        "print('IGNI_WS_TOKEN_FD' in os.environ)\n"
    )
    done = subprocess.run(  # noqa: S603 — this interpreter, a fixed script
        [sys.executable, "-c", script],
        input=f"{SECRET}\n".encode(),
        env={**os.environ, TOKEN_FD_ENV: "0"},
        capture_output=True,
        timeout=60,
        check=True,
    )

    assert done.stdout.decode().split() == [SECRET, "True", "False"]
