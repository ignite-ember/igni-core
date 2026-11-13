"""``RpcWrite``: one RPC that succeeded, and what its arguments say it was to."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.protocol.rpc import RpcMethod

METHOD = next(iter(RpcMethod))


def write(**args: Any) -> RpcWrite:
    return RpcWrite(method=METHOD, args=args, session_id="s1")


def test_the_subject_is_the_name_the_caller_gave() -> None:
    assert write(name="notes").subject == "notes"


def test_the_subject_falls_back_to_the_server_name() -> None:
    assert write(server_name="github").subject == "github"


def test_the_name_wins_over_the_server_name() -> None:
    assert write(name="notes", server_name="github").subject == "notes"


@pytest.mark.parametrize("name", ["", None])
def test_an_empty_name_falls_through_to_the_server_name(name: Any) -> None:
    assert write(name=name, server_name="github").subject == "github"


def test_with_neither_there_is_no_subject() -> None:
    assert write(other="x").subject is None
    assert write(name="", server_name="").subject == ""


def test_text_is_the_argument_as_a_string() -> None:
    assert write(path="a/b").text("path") == "a/b"
    assert write(count=3, flag=True).text("count") == "3"
    assert write(count=3, flag=True).text("flag") == "True"


def test_text_of_an_absent_key_is_empty() -> None:
    assert write(path="a").text("other") == ""


def test_a_method_igni_does_not_have_is_not_a_write() -> None:
    with pytest.raises(ValidationError):
        RpcWrite(method="no_such_method", args={}, session_id="s1")  # type: ignore[arg-type]


def test_a_write_keeps_its_session_and_arguments() -> None:
    made = write(name="n")

    assert (made.method, made.args, made.session_id) == (METHOD, {"name": "n"}, "s1")
