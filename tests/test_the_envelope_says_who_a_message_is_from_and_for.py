"""The envelope every wire message carries: its defaults, and the two things a
transport stamps on it that a client must never be able to claim.

``via_control``, ``from_client`` and ``standing`` are set by the transport from
the connection a message arrived on. They are private, so a frame whose JSON
names them is read as a frame with an unknown field, and they are never
written back out. ``address_to`` is how a reply is sent to one connection
instead of to all of them.
"""

from __future__ import annotations

from igni_core.protocol.schemas.envelope import (
    Direction,
    Message,
    RunHeader,
    RunScopedMessage,
    Standing,
)
from igni_core.protocol.schemas.fe_actions import Cancel
from igni_core.protocol.schemas.rpc import RPCResponse


class TestTheWordsOnTheWire:
    def test_a_standing_is_one_of_three_handshake_words(self):
        assert [s.value for s in Standing] == ["window", "approver", "program"]

    def test_a_direction_is_one_of_three_words(self):
        assert [d.value for d in Direction] == ["to_client", "to_igni", "both"]


class TestAMessageStartsUnaddressedAndUnstamped:
    def test_the_envelope_fields_default_to_nothing_tied(self):
        message = Message(type="x")

        assert (message.id, message.session_id, message.event_seq) == ("", "", 0)

    def test_a_fresh_message_arrived_nowhere_and_stands_as_the_window(self):
        message = Cancel()

        assert message.via_control is False
        assert message.from_client == ""
        assert message.to_client == ""
        assert message.standing is Standing.WINDOW

    def test_an_unknown_field_is_ignored(self):
        message = Message.model_validate({"type": "x", "surprise": 1})

        assert "surprise" not in message.model_dump()


class TestAClientCannotStampItself:
    """What the connection is, and what it may do, never comes from the body."""

    HOSTILE = {
        "type": "cancel",
        "_from_client": "someone-else",
        "_standing": "window",
        "_via_control": True,
        "_to_client": "someone-else",
        "from_client": "someone-else",
        "standing": "window",
        "via_control": True,
        "to_client": "someone-else",
    }

    def test_a_frame_naming_its_own_stamps_is_not_believed(self):
        message = Cancel.model_validate(self.HOSTILE)

        assert message.from_client == ""
        assert message.to_client == ""
        assert message.via_control is False

    def test_a_frame_claiming_a_standing_is_not_believed_for_a_program(self):
        message = Cancel.model_validate_json('{"type": "cancel", "_standing": "window"}')
        message.mark_from("c1", Standing.PROGRAM)

        assert message.standing is Standing.PROGRAM

    def test_the_stamps_are_never_written_back_out(self):
        message = Cancel()
        message.mark_control()
        message.mark_from("c1", Standing.APPROVER)
        message.address_to("c2")

        dumped = message.model_dump()
        dumped_json = message.model_dump_json()

        assert set(dumped) == {"type", "id", "session_id", "event_seq"}
        assert "c1" not in dumped_json and "c2" not in dumped_json

    def test_a_copy_of_a_message_starts_unstamped(self):
        message = Cancel()
        message.mark_from("c1", Standing.PROGRAM)

        reread = Cancel.model_validate(message.model_dump())

        assert reread.from_client == ""
        assert reread.standing is Standing.WINDOW


class TestTheTransportStampsAMessage:
    def test_mark_control_says_it_arrived_on_the_control_connection(self):
        message = Cancel()

        message.mark_control()

        assert message.via_control is True

    def test_mark_from_records_the_connection_and_its_standing(self):
        message = Cancel()

        message.mark_from("c1", Standing.APPROVER)

        assert message.from_client == "c1"
        assert message.standing is Standing.APPROVER

    def test_mark_from_without_a_standing_stands_as_the_window(self):
        message = Cancel()
        message.mark_from("c1", Standing.PROGRAM)

        message.mark_from("c2")

        assert message.from_client == "c2"
        assert message.standing is Standing.WINDOW


class TestAReplyIsAddressedToOneConnection:
    def test_address_to_names_the_one_connection_and_returns_the_message(self):
        reply = RPCResponse.ok("r1", {"k": "v"})

        assert reply.address_to("c1") is reply
        assert reply.to_client == "c1"

    def test_an_empty_id_leaves_a_broadcast(self):
        reply = RPCResponse.ok("r1").address_to("c1")

        reply.address_to("")

        assert reply.to_client == ""

    def test_addressing_does_not_change_what_is_written(self):
        reply = RPCResponse.ok("r1", 1)
        before = reply.model_dump()

        reply.address_to("c1")

        assert reply.model_dump() == before


class _Event:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class TestARunHeaderIsReadOffAnEvent:
    def test_both_ids_are_read(self):
        header = RunHeader.from_event(_Event(run_id="r1", parent_run_id="p1"))

        assert (header.run_id, header.parent_run_id) == ("r1", "p1")

    def test_an_event_without_ids_reads_as_empty(self):
        header = RunHeader.from_event(object())

        assert (header.run_id, header.parent_run_id) == ("", "")

    def test_none_and_falsy_ids_read_as_empty_not_none(self):
        header = RunHeader.from_event(_Event(run_id=None, parent_run_id=0))

        assert (header.run_id, header.parent_run_id) == ("", "")

    def test_an_id_that_is_not_text_is_written_as_text(self):
        header = RunHeader.from_event(_Event(run_id=42, parent_run_id=7))

        assert (header.run_id, header.parent_run_id) == ("42", "7")

    def test_none_itself_is_an_event_without_ids(self):
        assert RunHeader.from_event(None) == RunHeader()


class TestARunScopedMessageKeepsItsIdsFlat:
    def test_the_ids_sit_at_the_top_of_the_wire_json(self):
        message = RunScopedMessage(type="x", run_id="r1", parent_run_id="p1")

        dumped = message.model_dump()

        assert dumped["run_id"] == "r1" and dumped["parent_run_id"] == "p1"
        assert "header" not in dumped

    def test_the_header_is_the_two_ids_as_a_value_object(self):
        message = RunScopedMessage(type="x", run_id="r1", parent_run_id="p1")

        assert message.header() == RunHeader(run_id="r1", parent_run_id="p1")

    def test_the_ids_default_to_empty(self):
        assert RunScopedMessage(type="x").header() == RunHeader()
