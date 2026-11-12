"""``RpcWatch``: a window's write is a signal once its reply goes out, and only then.

A window's RPC broadcasts nothing of its own, so the backend remembers each
request on the way in and, when the matching reply leaves, tells the host
model what was written. A refusal, a reply nobody asked for, a pool-level call
and a method igni does not know are not writes.
"""

from __future__ import annotations

from igni_core.backend.rpc_watch import RpcWatch
from igni_core.extensions.host.rpc_write import RpcWrite
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod


def _watch() -> tuple[RpcWatch, list[RpcWrite]]:
    told: list[RpcWrite] = []
    return RpcWatch(told.append), told


def _request(
    request_id: str = "r1",
    method: str = RpcMethod.SET_WORKSPACE_TRUST,
    args: dict | None = None,
    session_id: str = "s-1",
) -> msg.RPCRequest:
    return msg.RPCRequest(id=request_id, method=str(method), args=args or {}, session_id=session_id)


class TestAWriteIsToldOnItsSuccessfulReply:
    def test_the_reply_tells_the_method_the_args_and_the_session(self):
        watch, told = _watch()

        watch.inbound(_request(args={"trusted": True}))
        watch.outbound(msg.RPCResponse.ok("r1", {"ok": True}))

        assert told == [
            RpcWrite(method=RpcMethod.SET_WORKSPACE_TRUST, args={"trusted": True}, session_id="s-1")
        ]

    def test_nothing_is_told_before_the_reply(self):
        watch, told = _watch()

        watch.inbound(_request())

        assert told == []

    def test_a_second_reply_with_the_same_id_is_not_told_again(self):
        watch, told = _watch()
        watch.inbound(_request())

        watch.outbound(msg.RPCResponse.ok("r1"))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert len(told) == 1

    def test_a_request_without_a_session_takes_the_one_the_reply_names(self):
        watch, told = _watch()
        watch.inbound(_request(session_id=""))

        reply = msg.RPCResponse.ok("r1")
        reply.session_id = "s-reply"
        watch.outbound(reply)

        assert [w.session_id for w in told] == ["s-reply"]

    def test_a_session_the_request_named_is_not_replaced_by_the_reply_s(self):
        watch, told = _watch()
        watch.inbound(_request(session_id="s-1"))

        reply = msg.RPCResponse.ok("r1")
        reply.session_id = "s-other"
        watch.outbound(reply)

        assert [w.session_id for w in told] == ["s-1"]

    def test_a_null_result_is_still_a_success(self):
        watch, told = _watch()
        watch.inbound(_request())

        watch.outbound(msg.RPCResponse.ok("r1", None))

        assert len(told) == 1

    def test_the_args_are_a_copy_later_mutation_does_not_reach(self):
        watch, told = _watch()
        args = {"trusted": True}
        watch.inbound(_request(args=args))
        args["trusted"] = False

        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told[0].args == {"trusted": True}

    def test_a_duplicate_id_overwrites_the_earlier_request(self):
        watch, told = _watch()
        watch.inbound(_request(args={"n": 1}))
        watch.inbound(_request(args={"n": 2}))

        watch.outbound(msg.RPCResponse.ok("r1"))

        assert [w.args for w in told] == [{"n": 2}]


class TestAFailureIsNotAWrite:
    def test_an_error_response_tells_nothing_and_forgets_the_request(self):
        watch, told = _watch()
        watch.inbound(_request())

        watch.outbound(msg.RPCResponse.fail("r1", "no", code=msg.RpcErrorCode.REFUSED))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told == []

    def test_an_error_message_with_the_id_tells_nothing_and_forgets_the_request(self):
        watch, told = _watch()
        watch.inbound(_request())

        watch.outbound(msg.Error(id="r1", text="boom"))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told == []


class TestWhatIsNotWatched:
    def test_a_message_that_is_not_a_request_is_ignored(self):
        watch, told = _watch()

        watch.inbound(msg.Info(id="r1", text="hi"))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told == []

    def test_a_request_without_an_id_is_ignored(self):
        watch, told = _watch()

        watch.inbound(_request(request_id=""))
        watch.outbound(msg.RPCResponse.ok(""))

        assert told == []

    def test_a_method_igni_does_not_know_is_ignored(self):
        watch, told = _watch()

        watch.inbound(_request(method="ext.not-a-platform-method"))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told == []

    def test_a_pool_level_call_is_ignored(self):
        watch, told = _watch()

        watch.inbound(_request(method=RpcMethod.ATTACH_SESSION))
        watch.outbound(msg.RPCResponse.ok("r1"))

        assert told == []

    def test_a_reply_nobody_asked_for_is_ignored(self):
        watch, told = _watch()

        watch.outbound(msg.RPCResponse.ok("never-asked"))

        assert told == []


class TestTheTableIsBounded:
    def test_the_oldest_request_is_forgotten_past_the_limit(self):
        watch, told = _watch()
        for n in range(RpcWatch.LIMIT + 1):
            watch.inbound(_request(request_id=f"r{n}"))

        watch.outbound(msg.RPCResponse.ok("r0"))
        watch.outbound(msg.RPCResponse.ok("r1"))
        watch.outbound(msg.RPCResponse.ok(f"r{RpcWatch.LIMIT}"))

        assert len(told) == 2

    def test_the_limit_is_a_thousand(self):
        assert RpcWatch.LIMIT == 1000
