"""How the host reads a handler's answers: its refusal, its status, the floor's list.

The readings are tolerant by design: an answer of the wrong shape reads
as empty, a null reads as nothing, and ids named by an extension are
compared as the floor's rows carry them.
"""

from __future__ import annotations

from typing import Any

import pytest

from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.rpc_wire.base import RpcInfo
from igni_core.extensions.host.rpc_wire.notifications import (
    MarkReadArgs,
    NotificationListing,
    NotificationRow,
)
from igni_core.extensions.host.rpc_wire.status import StatusAnswer
from igni_core.extensions.host.values import SessionStatus
from tests.host_model_helpers import manifest

# -- the refusal an info answer carries ---------------------------------------


@pytest.mark.parametrize("info", [None, [], ["error"], "error", 3])
def test_an_info_that_is_not_an_object_is_no_refusal(info: Any) -> None:
    read = RpcInfo.from_rpc(info)
    assert (read.type, read.text) == ("", "")
    assert read.refusal() is None


def test_an_error_info_is_refused_with_its_text() -> None:
    assert RpcInfo.from_rpc({"type": "error", "text": "no such model", "x": 1}).refusal() == (
        "no such model"
    )


@pytest.mark.parametrize("text", [None, 0, "", False])
def test_an_error_info_without_text_is_refused_as_refused(text: Any) -> None:
    assert RpcInfo.from_rpc({"type": "error", "text": text}).refusal() == "refused"
    assert RpcInfo.from_rpc({"type": "error"}).refusal() == "refused"


def test_a_number_as_text_is_read_as_its_digits() -> None:
    assert RpcInfo.from_rpc({"type": "error", "text": 404}).refusal() == "404"


@pytest.mark.parametrize("kind", ["info", "warning", "", "ERROR", None, 7])
def test_only_the_type_error_is_a_refusal(kind: Any) -> None:
    assert RpcInfo.from_rpc({"type": kind, "text": "words"}).refusal() is None


def test_a_type_that_is_not_text_reads_as_empty() -> None:
    read = RpcInfo.from_rpc({"type": 5, "text": "t"})
    assert (read.type, read.text) == ("", "t")


# -- get_status ---------------------------------------------------------------


def test_an_empty_status_reads_as_the_documented_defaults() -> None:
    read = StatusAnswer.from_rpc({})
    assert read.model_dump() == {
        "model": "",
        "model_configured": True,
        "permission_mode": "default",
        "max_context": 0,
        "cloud_connected": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_tokens": 0,
    }


def test_a_null_in_a_status_reads_as_nothing_not_as_the_word_none() -> None:
    raw = {"model": None, "permission_mode": None, "max_context": None, "input_tokens": None}
    read = StatusAnswer.from_rpc(raw)
    assert read.model == ""
    assert read.permission_mode == ""
    assert (read.max_context, read.input_tokens) == (0, 0)
    assert read.session_status("s") == SessionStatus.from_status(raw, "s")


def test_a_status_reads_exactly_as_the_session_status_does() -> None:
    raw = {
        "model": "m-1",
        "model_configured": False,
        "permission_mode": "plan",
        "max_context": "2048",
        "cloud_connected": 1,
        "input_tokens": 7,
        "output_tokens": "9",
        "context_tokens": 0,
        "extra": "ignored",
    }
    assert StatusAnswer.from_rpc(raw).session_status("sess") == SessionStatus.from_status(
        raw, "sess"
    )


def test_the_connection_carries_the_cloud_and_the_model_flags() -> None:
    answer = StatusAnswer.from_rpc({"cloud_connected": True, "model_configured": False})
    assert answer.connection().model_dump() == {
        "cloud_connected": True,
        "model_configured": False,
    }
    quiet = StatusAnswer.from_rpc({"cloud_connected": 0})
    assert quiet.connection().model_dump() == {"cloud_connected": False, "model_configured": True}


def test_a_session_status_names_the_session_asked_and_every_count() -> None:
    answer = StatusAnswer.from_rpc(
        {
            "model": "m",
            "permission_mode": "acceptEdits",
            "max_context": 100,
            "input_tokens": 1,
            "output_tokens": 2,
            "context_tokens": 3,
        }
    )
    got = answer.session_status("abc")
    assert got.model_dump() == {
        "session_id": "abc",
        "model": "m",
        "model_configured": True,
        "permission_mode": "acceptEdits",
        "input_tokens": 1,
        "output_tokens": 2,
        "context_tokens": 3,
        "max_context": 100,
    }
    assert answer.session_status("other").session_id == "other"


# -- the floor's notifications -------------------------------------------------


def row(id_: Any, source: str, **more: Any) -> dict[str, Any]:
    return {"id": id_, "source": source, "title": f"t{id_}", **more}


def test_mark_read_sends_none_for_every_notification_and_every_source() -> None:
    assert MarkReadArgs(ids=None, source=None).model_dump() == {"ids": None, "source": None}
    assert MarkReadArgs(ids=["1"], source="ext").model_dump() == {"ids": ["1"], "source": "ext"}


@pytest.mark.parametrize("found", [None, [], "x", {}, {"other": [row(1, "a")]}])
def test_an_answer_without_a_notifications_list_is_an_empty_floor(found: Any) -> None:
    listing = NotificationListing.from_rpc(found)
    assert listing.rows == []
    assert listing.foreign_ids([1], "a") == [1]


def test_a_row_keeps_its_raw_id_beside_the_entry() -> None:
    assert NotificationRow.from_row(row(7, "a")).key == 7
    assert NotificationRow.from_row(row("7", "a")).key == "7"
    assert NotificationRow.from_row(row(7, "a")).entry.id == "7"
    assert NotificationRow.from_row({"source": "a"}).key is None


def floor() -> NotificationListing:
    return NotificationListing.from_rpc(
        {"notifications": [row(1, "a"), row("2", "b"), row(3, "a"), row(4, "")]}
    )


def test_an_extension_without_the_grant_sees_only_its_own_rows_in_order() -> None:
    seen = floor().visible_to(HostGrants(manifest(host=[])), "a")
    assert [n.id for n in seen] == ["1", "3"]
    assert floor().visible_to(HostGrants(manifest(host=[])), "nobody") == []


def test_an_extension_with_the_grant_sees_every_row_in_the_floors_order() -> None:
    seen = floor().visible_to(HostGrants(manifest(host=["notifications"])), "a")
    assert [n.id for n in seen] == ["1", "2", "3", "4"]


def test_ids_are_foreign_unless_they_are_the_extensions_own_rows() -> None:
    assert floor().foreign_ids([1, 3], "a") == []
    assert floor().foreign_ids([1, "2", 99], "a") == ["2", 99]
    assert floor().foreign_ids([], "a") == []


def test_an_id_is_compared_as_the_row_carries_it_not_as_text() -> None:
    assert floor().foreign_ids(["1", 1], "a") == ["1"]
    assert floor().foreign_ids([2, "2"], "b") == [2]


def test_a_row_without_an_id_is_not_named_by_none() -> None:
    listing = NotificationListing.from_rpc({"notifications": [{"source": "a"}]})
    assert listing.rows[0].key is None
    assert listing.foreign_ids([1, "x"], "a") == [1, "x"]
