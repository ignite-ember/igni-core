"""The pool's payload models: lenient values, refused keys, the receipt copied
field for field, and an answer dumped in the mode it was given."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from igni_core.backend.schemas_pool import (
    ApproverTokenMinted,
    ApproverTokensRevoked,
    AttachSessionParams,
    ExtensionPost,
    PoolAnswer,
    ServiceEventsAck,
    ServiceEventsParams,
    VmCredentialState,
    VmSignInParams,
)
from igni_core.extensions.host.attachments import ChatAttachments
from igni_core.extensions.service_events import DeliveryReceipt, NotHandled
from igni_core.protocol.schemas.rpc import RpcErrorCode


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ({}, ("", "")),
        ({"session_id": None, "project_dir": None}, ("", "")),
        ({"session_id": 42, "project_dir": "/p"}, ("42", "/p")),
        ({"session_id": 0}, ("", "")),
    ],
    ids=["absent", "null", "numeric", "zero"],
)
def test_an_attach_reads_absent_null_and_numbers_as_the_strings_they_always_were(wire, expected):
    params = AttachSessionParams.model_validate(wire)
    assert (params.session_id, params.project_dir) == expected


@pytest.mark.parametrize("model", [AttachSessionParams, VmSignInParams, ServiceEventsParams])
def test_an_argument_model_refuses_a_key_it_does_not_name(model):
    with pytest.raises(ValidationError):
        model.model_validate({"surprise": 1})


def test_a_sign_in_with_values_of_the_wrong_type_degrades_instead_of_failing():
    params = VmSignInParams.model_validate({"session_token": 123, "organisation": 7})
    assert (params.session_token, params.organisation) == ("", None)
    kept = VmSignInParams.model_validate({"session_token": "t", "organisation": "acme"})
    assert (kept.session_token, kept.organisation) == ("t", "acme")
    assert VmSignInParams.model_validate({}).organisation is None


def test_an_empty_events_batch_is_a_batch_of_none():
    assert ServiceEventsParams.model_validate({}).events == []
    assert ServiceEventsParams.model_validate({"events": []}).events == []


def test_an_ack_copies_the_receipt_and_acknowledges_what_may_be_forgotten():
    receipt = DeliveryReceipt(
        delivered=["a"],
        duplicate=["b"],
        failed=["c"],
        not_handled=[NotHandled(event_id="d", reason="no handler")],
    )
    ack = ServiceEventsAck.of(receipt)
    assert ack.delivered == ["a"]
    assert ack.duplicate == ["b"]
    assert ack.failed == ["c"]
    assert ack.not_handled == [NotHandled(event_id="d", reason="no handler")]
    # A failed event stays unacknowledged so it is sent again.
    assert ack.acknowledge == ["a", "d", "b"]


@pytest.mark.parametrize("state", ["ready", "needs_sign_in", "needs_reissue", "unreachable"])
def test_a_credential_state_is_one_of_four_words_with_an_empty_message(state):
    credential = VmCredentialState(state=state)
    assert (credential.state, credential.message) == (state, "")


def test_a_credential_state_outside_the_four_is_refused():
    with pytest.raises(ValidationError):
        VmCredentialState(state="expired")


def test_the_approver_token_answers_carry_their_token_and_their_count():
    assert ApproverTokenMinted(token="t").model_dump() == {"token": "t"}
    assert ApproverTokensRevoked(revoked=3).model_dump() == {"revoked": 3}


def test_an_extension_post_without_media_carries_an_empty_bundle():
    post = ExtensionPost(session_id="s", extension_id="e", text="hi", media=None, queued=True)
    assert post.media == ChatAttachments()
    assert post.media.has_files() is False
    assert post.from_service == ""
    with_files = ExtensionPost(
        session_id="s",
        extension_id="e",
        text="hi",
        media={"files": {"a.txt": "x"}},
        queued=False,
        from_service="svc",
    )
    assert with_files.media.files == {"a.txt": "x"}
    assert (with_files.queued, with_files.from_service) == (False, "svc")


class _Payload(BaseModel):
    when: datetime
    where: Path


class _Richer(_Payload):
    extra: str = "kept"


def test_an_answer_with_no_result_has_nothing_on_the_wire():
    assert PoolAnswer().wire_result() is None
    failed = PoolAnswer.failed("nope", RpcErrorCode.REFUSED)
    assert (failed.error, failed.code, failed.wire_result()) == (
        "nope",
        RpcErrorCode.REFUSED,
        None,
    )


def test_an_answer_dumps_its_result_in_python_values_unless_asked_for_json():
    result = _Payload(when=datetime(2026, 1, 2, 3, 4, 5), where=Path("/tmp/x"))
    python = PoolAnswer.ok(result).wire_result()
    assert python == {"when": datetime(2026, 1, 2, 3, 4, 5), "where": Path("/tmp/x")}
    json_mode = PoolAnswer.ok(result, dump_mode="json").wire_result()
    assert json_mode == {"when": "2026-01-02T03:04:05", "where": "/tmp/x"}


def test_an_answer_dumps_every_field_of_a_result_subclass():
    result = _Richer(when=datetime(2026, 1, 2), where=Path("/p"))
    dumped = PoolAnswer.ok(result, dump_mode="json").wire_result()
    assert dumped["extra"] == "kept"


def test_an_answer_dumped_whole_keeps_the_fields_of_its_result_subclass():
    answer = PoolAnswer.ok(_Richer(when=datetime(2026, 1, 2), where=Path("/p")))
    assert answer.model_dump()["result"]["extra"] == "kept"
