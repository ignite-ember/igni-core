"""Each host member turns an extension's request into igni's own call, and
igni's answer back into the model's.

These drive the member classes over a scripted host: the gateway answers
each RPC from a table and records what it was asked, so what a member
sends (the RPC, its arguments, the session) and what it answers or
refuses is the whole of what is checked.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from igni_core import __version__
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.grants import HostGrants
from igni_core.extensions.host.members import ALL_MEMBERS
from igni_core.extensions.host.members.account import AccountMembers
from igni_core.extensions.host.members.agents import AgentMembers, PromoteTarget
from igni_core.extensions.host.members.answers import CompactResult
from igni_core.extensions.host.members.approvals import (
    ApprovalAction,
    ApprovalAnswer,
    ApprovalMembers,
)
from igni_core.extensions.host.members.base import Members
from igni_core.extensions.host.members.call import Arguments, Call
from igni_core.extensions.host.members.chat import ChatReadMembers, ChatWriteMembers
from igni_core.extensions.host.members.choice import Choice
from igni_core.extensions.host.members.commands import CommandMembers
from igni_core.extensions.host.members.params import MemberParams
from igni_core.extensions.host.members.tag import call as tagged_call
from igni_core.extensions.host.session_target import SessionTarget
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.state.approvals import ApprovalMirror
from igni_core.extensions.host.values import CommandOutcome
from igni_core.protocol import messages as msg
from igni_core.protocol.rpc import RpcMethod
from tests.host_model_helpers import manifest


class Gateway:
    """Answers each RPC from ``answers`` and remembers what was asked."""

    def __init__(self, answers: dict[RpcMethod, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[RpcMethod, dict[str, Any], str]] = []
        self.account_for: list[str] = []
        self.outcome = SimpleNamespace(kind="info", content="ok", display_content="ok")
        self.commands: list[tuple[str, str]] = []

    async def rpc(self, method: RpcMethod, args: dict[str, Any], *, session_id: str) -> Any:
        self.calls.append((method, args, session_id))
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def account(self, session_id: str) -> Any:
        self.account_for.append(session_id)
        status = self.answers[RpcMethod.GET_STATUS]
        from igni_core.extensions.host.values import Account

        return Account.from_status(status)

    async def command(self, text: str, *, session_id: str) -> CommandOutcome:
        self.commands.append((text, session_id))
        return CommandOutcome.from_answer(self.outcome)


class Backend:
    def __init__(self) -> None:
        self.acted: list[tuple[Any, str]] = []
        self.waiting: list[msg.Message] = []

    async def act(self, message: Any, *, session_id: str) -> None:
        self.acted.append((message, session_id))

    def waiting_approvals(self, session_id: str) -> list[msg.Message]:
        return list(self.waiting)


def host_of(gateway: Gateway, backend: Backend | None = None) -> Any:
    state = SimpleNamespace(approvals=ApprovalMirror())
    return SimpleNamespace(
        gateway=gateway,
        backend=backend or Backend(),
        state=state,
        bus=SimpleNamespace(emit=lambda *a, **k: None),
    )


def a_call(window: str | None = None, **params: Any) -> Call:
    return Call(
        extension_id="acme.reviewer",
        grants=HostGrants(manifest("acme.reviewer")),
        window=window,
        target=SessionTarget(session_id="s1", on_screen=True),
        arguments=Arguments(params=params),
    )


STATUS = {
    "model": "small",
    "max_context": 8000,
    "cloud_connected": True,
    "cloud_email": "ada@acme.test",
    "cloud_name": "Ada",
    "cloud_org": "Acme",
    "api_key": "sk-secret",
}


class TestTheMemberTable:
    def test_no_member_class_is_listed_twice(self) -> None:
        assert len(set(ALL_MEMBERS)) == len(ALL_MEMBERS) == 31

    def test_every_listed_class_is_a_members_class(self) -> None:
        assert all(issubclass(cls, Members) for cls in ALL_MEMBERS)

    def test_the_table_binds_tagged_methods_only_not_helpers_or_dunders(self) -> None:
        class Mine(Members):
            @tagged_call("chat.cancel")
            async def cancel(self, c: Call) -> None:
                return None

            async def helper(self, c: Call) -> None:
                return None

            def __hidden__(self) -> None:
                return None

        table = Mine(host_of(Gateway({}))).table()
        assert [(b.key.op.value, b.key.path) for b in table] == [("call", "chat.cancel")]

    def test_a_class_with_nothing_tagged_binds_nothing(self) -> None:
        assert Members(host_of(Gateway({}))).table() == []

    def test_a_binding_runs_the_instances_own_method(self) -> None:
        members = AccountMembers(host_of(Gateway({RpcMethod.GET_STATUS: STATUS})))
        paths = {b.key.path: b.impl for b in members.table()}
        assert paths["account.status"] == members.status


class TestRpcHelpers:
    async def test_an_rpc_runs_in_the_callers_session_with_the_dumped_args(self) -> None:
        gateway = Gateway({RpcMethod.GET_RUN_TIMEOUT: 5})
        members = Members(host_of(gateway))
        await members.rpc(RpcMethod.GET_RUN_TIMEOUT, c=a_call())
        assert gateway.calls == [(RpcMethod.GET_RUN_TIMEOUT, {}, "s1")]

    async def test_an_rpc_in_names_its_own_session(self) -> None:
        from igni_core.extensions.host.rpc_wire.chat import RunIdArgs

        gateway = Gateway({RpcMethod.CANCEL_AGENT_RUN: None})
        await Members(host_of(gateway)).rpc_in(
            RpcMethod.CANCEL_AGENT_RUN, RunIdArgs(run_id="r1"), "other"
        )
        assert gateway.calls == [(RpcMethod.CANCEL_AGENT_RUN, {"run_id": "r1"}, "other")]

    async def test_an_answer_that_says_error_is_a_refusal_with_its_text(self) -> None:
        gateway = Gateway({RpcMethod.CANCEL_RUN: {"type": "error", "text": "nope"}})
        with pytest.raises(HostModelError) as caught:
            await Members(host_of(gateway)).rpc_checked(RpcMethod.CANCEL_RUN, c=a_call())
        assert caught.value.code == ErrorCode.REFUSED
        assert caught.value.message == "nope"

    async def test_an_error_answer_without_text_is_refused_as_refused(self) -> None:
        gateway = Gateway({RpcMethod.CANCEL_RUN: {"type": "error"}})
        with pytest.raises(HostModelError) as caught:
            await Members(host_of(gateway)).rpc_checked(RpcMethod.CANCEL_RUN, c=a_call())
        assert caught.value.message == "refused"

    @pytest.mark.parametrize("info", [{"type": "info", "text": "fine"}, "fine", None, {}])
    async def test_any_other_answer_is_returned_as_it_came(self, info: Any) -> None:
        gateway = Gateway({RpcMethod.CANCEL_RUN: info})
        got = await Members(host_of(gateway)).rpc_checked(RpcMethod.CANCEL_RUN, c=a_call())
        assert got == info

    async def test_the_status_is_read_once_through_get_status(self) -> None:
        gateway = Gateway({RpcMethod.GET_STATUS: STATUS})
        status = await Members(host_of(gateway)).read_status(a_call())
        assert status.max_context == 8000
        assert [m for m, _, _ in gateway.calls] == [RpcMethod.GET_STATUS]


class TestTheCall:
    def test_its_session_and_screen_come_from_the_target(self) -> None:
        c = Call(
            extension_id="x.y",
            grants=HostGrants(manifest("x.y")),
            window=None,
            target=SessionTarget(session_id="s9", on_screen=False),
            arguments=Arguments(params={"a": 1}, value="v"),
        )
        assert (c.session_id, c.on_screen, c.params, c.value) == ("s9", False, {"a": 1}, "v")

    def test_the_arguments_default_to_no_params_and_no_value(self) -> None:
        args = Arguments()
        assert args.params == {} and args.value is None

    def test_a_calls_own_client_id_is_the_extensions_prefixed(self) -> None:
        assert a_call().own_client_id == "ext:acme.reviewer"

    def test_a_call_cannot_be_changed(self) -> None:
        with pytest.raises(ValidationError):
            a_call().window = "w"  # type: ignore[misc]

    def test_no_window_open_is_not_found(self) -> None:
        with pytest.raises(HostModelError) as caught:
            a_call(window=None).require_window()
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert caught.value.message == "no igni window is open"

    def test_the_window_is_returned_when_there_is_one(self) -> None:
        assert a_call(window="w-1").require_window() == "w-1"

    def test_not_granted_names_the_extension_the_member_and_the_permission(self) -> None:
        refusal = a_call().not_granted("chat.post", "chat.post")
        assert refusal.code == ErrorCode.NOT_GRANTED
        assert "'acme.reviewer' is not granted 'chat.post', which chat.post needs" in (
            refusal.message
        )
        assert "Add 'chat.post' to `permissions.host`" in refusal.message

    def test_the_parameters_are_read_as_the_members_model(self) -> None:
        class Params(MemberParams):
            PATH = "demo.run"
            name: str

        assert a_call(name="x").params_as(Params).name == "x"

    def test_a_missing_field_is_invalid_with_the_path_and_the_field(self) -> None:
        class Params(MemberParams):
            PATH = "demo.run"
            name: str

        with pytest.raises(HostModelError) as caught:
            a_call().params_as(Params)
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message.startswith("demo.run(name): ")


class TestAChoice:
    def test_a_name_in_the_set_is_the_member(self) -> None:
        assert PromoteTarget.of("personal") is PromoteTarget.PERSONAL

    @pytest.mark.parametrize("value", ["", "Project", "PERSONAL", "projekt", "projét"])
    def test_a_name_outside_the_set_is_invalid_in_the_sets_own_words(self, value: str) -> None:
        with pytest.raises(HostModelError) as caught:
            PromoteTarget.of(value)
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == PromoteTarget.refusal(value)
        assert repr(value) in caught.value.message

    def test_a_set_that_says_nothing_of_its_refusal_is_a_programming_error(self) -> None:
        class Bare(Choice):
            ONE = "one"

        assert Bare.of("one") is Bare.ONE
        with pytest.raises(NotImplementedError, match="Bare"):
            Bare.of("two")


class TestCompactResult:
    @pytest.mark.parametrize("cleared", [None, {}, {"compacted": False, "summary": "s"}])
    def test_nothing_compacted_is_nothing(self, cleared: Any) -> None:
        assert CompactResult.from_rpc("s1", cleared) is None

    def test_a_compaction_carries_the_session_the_new_one_and_the_summary(self) -> None:
        got = CompactResult.from_rpc(
            "s1", {"compacted": True, "new_session_id": "s2", "summary": "Gist"}
        )
        assert got == CompactResult(session_id="s1", new_session_id="s2", summary="Gist")

    def test_what_the_answer_leaves_out_is_empty_and_what_is_not_text_is_text(self) -> None:
        assert CompactResult.from_rpc("s1", {"compacted": True}) == CompactResult(
            session_id="s1", new_session_id="", summary=""
        )
        got = CompactResult.from_rpc("s1", {"compacted": True, "new_session_id": 7})
        assert got is not None and got.new_session_id == "7"


class TestAccount:
    async def test_the_status_is_the_four_words_and_no_credential(self) -> None:
        gateway = Gateway({RpcMethod.GET_STATUS: STATUS})
        account = await AccountMembers(host_of(gateway)).status(a_call())
        assert account.model_dump() == {
            "signed_in": True,
            "email": "ada@acme.test",
            "name": "Ada",
            "org": "Acme",
        }
        assert gateway.account_for == ["s1"]

    async def test_signed_out_is_an_empty_account(self) -> None:
        gateway = Gateway({RpcMethod.GET_STATUS: {"cloud_connected": False}})
        account = await AccountMembers(host_of(gateway)).status(a_call())
        assert account.model_dump() == {"signed_in": False, "email": "", "name": "", "org": ""}

    async def test_signing_in_starts_the_login_and_returns_nothing_of_it(self) -> None:
        gateway = Gateway(
            {RpcMethod.LOGIN: {"started": True, "flow_id": "f1", "url": "https://x/code=1"}}
        )
        assert await AccountMembers(host_of(gateway)).sign_in(a_call()) is None
        assert gateway.calls == [(RpcMethod.LOGIN, {}, "s1")]

    async def test_signing_out_clears_the_credentials_then_answers_the_account_now(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.CLEAR_CLOUD_CREDENTIALS: None,
                RpcMethod.GET_STATUS: {"cloud_connected": False},
            }
        )
        account = await AccountMembers(host_of(gateway)).sign_out(a_call())
        assert gateway.calls == [(RpcMethod.CLEAR_CLOUD_CREDENTIALS, {}, "s1")]
        assert gateway.account_for == ["s1"]
        assert account.signed_in is False

    async def test_a_sign_out_that_fails_is_not_answered_as_signed_out(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.CLEAR_CLOUD_CREDENTIALS: HostModelError(ErrorCode.REFUSED, "no"),
                RpcMethod.GET_STATUS: STATUS,
            }
        )
        with pytest.raises(HostModelError):
            await AccountMembers(host_of(gateway)).sign_out(a_call())
        assert gateway.account_for == []


class TestAgents:
    ROW = {"name": "main", "description": "The main agent"}

    async def test_the_agents_are_listed_as_their_model(self) -> None:
        gateway = Gateway({RpcMethod.GET_AGENT_DETAILS: [self.ROW]})
        [agent] = await AgentMembers(host_of(gateway)).details(a_call())
        assert (agent.name, agent.description) == ("main", "The main agent")

    async def test_an_answer_that_is_not_a_list_is_a_failure_saying_so(self) -> None:
        gateway = Gateway({RpcMethod.GET_AGENT_DETAILS: {"agents": []}})
        with pytest.raises(HostModelError) as caught:
            await AgentMembers(host_of(gateway)).details(a_call())
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message == "agents.details answered with dict, not a list"

    async def test_a_row_that_does_not_fit_is_a_failure_naming_the_field(self) -> None:
        gateway = Gateway({RpcMethod.GET_AGENT_DETAILS: [{"description": "no name"}]})
        with pytest.raises(HostModelError) as caught:
            await AgentMembers(host_of(gateway)).details(a_call())
        assert caught.value.code == ErrorCode.FAILED
        assert "agents.details answered with something AgentInfo does not describe" in (
            caught.value.message
        )
        assert "name" in caught.value.message

    @pytest.mark.parametrize("params", [{"name": "ghost"}, {"name": "ghost", "target": ""}])
    async def test_promoting_without_a_target_keeps_the_agent_under_project(
        self, params: dict[str, str]
    ) -> None:
        gateway = Gateway(
            {
                RpcMethod.PROMOTE_EPHEMERAL_AGENT: {
                    "ok": True,
                    "name": "ghost",
                    "dest": ".igni/agents/ghost.md",
                }
            }
        )
        got = await AgentMembers(host_of(gateway)).promote(a_call(**params))
        assert gateway.calls == [
            (RpcMethod.PROMOTE_EPHEMERAL_AGENT, {"name": "ghost", "target": "project"}, "s1")
        ]
        assert got.dest == ".igni/agents/ghost.md"

    async def test_a_target_that_is_not_one_moves_nothing(self) -> None:
        gateway = Gateway({})
        with pytest.raises(HostModelError) as caught:
            await AgentMembers(host_of(gateway)).promote(a_call(name="ghost", target="team"))
        assert caught.value.code == ErrorCode.INVALID
        assert "project" in caught.value.message and "personal" in caught.value.message
        assert gateway.calls == []

    async def test_a_promote_answer_that_does_not_fit_is_a_failure(self) -> None:
        gateway = Gateway({RpcMethod.PROMOTE_EPHEMERAL_AGENT: {"nothing": 1}})
        with pytest.raises(HostModelError) as caught:
            await AgentMembers(host_of(gateway)).promote(a_call(name="ghost"))
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message.startswith("agents.promote_ephemeral answered with something")

    async def test_discarding_names_the_agent_even_when_it_is_not_ascii(self) -> None:
        gateway = Gateway({RpcMethod.DISCARD_EPHEMERAL_AGENT: {"ok": True, "name": "Agént-É"}})
        got = await AgentMembers(host_of(gateway)).discard(a_call(name="Agént-É"))
        assert gateway.calls == [(RpcMethod.DISCARD_EPHEMERAL_AGENT, {"name": "Agént-É"}, "s1")]
        assert got.name == "Agént-É"

    async def test_discarding_without_a_name_is_not_sent(self) -> None:
        gateway = Gateway({})
        with pytest.raises(KeyError):
            await AgentMembers(host_of(gateway)).discard(a_call())
        assert gateway.calls == []


def request(requirement_id: str) -> msg.HITLRequest:
    return msg.HITLRequest(
        requirement_id=requirement_id,
        tool_name="Bash",
        friendly_name="Run a command",
        tool_args={"command": "ls"},
        details="",
        agent_path=[],
    )


def approvals_with(*ids: str) -> tuple[ApprovalMembers, Backend]:
    backend = Backend()
    members = ApprovalMembers(host_of(Gateway({}), backend))
    for one in ids:
        members.host.state.approvals.record("s1", request(one))
    return members, backend


class TestApprovals:
    async def test_what_waits_in_the_session_is_listed(self) -> None:
        members, _ = approvals_with("r1", "r2")
        got = await members.waiting(a_call())
        assert [a.requirement_id for a in got] == ["r1", "r2"]

    async def test_a_session_nobody_waits_in_lists_nothing(self) -> None:
        members, _ = approvals_with()
        assert await members.waiting(a_call()) == []

    async def test_one_answer_is_one_response_to_the_request(self) -> None:
        members, backend = approvals_with("r1")
        await members.answer(a_call(requirement_id="r1", action="confirm", choice="once"))
        assert backend.acted == [
            (msg.HITLResponse(requirement_id="r1", action="confirm", choice="once"), "s1")
        ]

    async def test_a_choice_left_out_is_empty(self) -> None:
        members, backend = approvals_with("r1")
        await members.answer(a_call(requirement_id="r1", action="reject", choice=None))
        [(sent, _)] = backend.acted
        assert (sent.action, sent.choice) == ("reject", "")

    async def test_a_request_not_waiting_is_not_found_and_nothing_is_sent(self) -> None:
        members, backend = approvals_with("r1")
        with pytest.raises(HostModelError) as caught:
            await members.answer(a_call(requirement_id="r9", action="confirm"))
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert caught.value.message == "no request 'r9' is waiting in session s1"
        assert backend.acted == []

    async def test_an_action_that_is_not_confirm_or_reject_is_invalid(self) -> None:
        members, backend = approvals_with("r1")
        with pytest.raises(HostModelError) as caught:
            await members.answer(a_call(requirement_id="r1", action="CONFIRM"))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == "action is 'confirm' or 'reject', not 'CONFIRM'"
        assert backend.acted == []

    async def test_a_batch_is_one_message_of_every_decision(self) -> None:
        members, backend = approvals_with("r1", "r2")
        await members.answer_all(
            a_call(
                answers=[
                    {"requirement_id": "r1", "action": "confirm"},
                    {"requirement_id": "r2", "action": "reject", "choice": "deny"},
                ]
            )
        )
        [(sent, session)] = backend.acted
        assert session == "s1"
        assert isinstance(sent, msg.HITLResponseBatch)
        assert [(d.requirement_id, d.action, d.choice) for d in sent.decisions] == [
            ("r1", "confirm", ""),
            ("r2", "reject", "deny"),
        ]

    async def test_one_unknown_request_in_a_batch_sends_none_of_it(self) -> None:
        members, backend = approvals_with("r1")
        with pytest.raises(HostModelError) as caught:
            await members.answer_all(
                a_call(
                    answers=[
                        {"requirement_id": "r1", "action": "confirm"},
                        {"requirement_id": "gone", "action": "confirm"},
                    ]
                )
            )
        assert caught.value.code == ErrorCode.NOT_FOUND
        assert "'gone'" in caught.value.message
        assert backend.acted == []

    async def test_an_empty_batch_is_sent_as_an_empty_batch(self) -> None:
        members, backend = approvals_with("r1")
        await members.answer_all(a_call(answers=[]))
        [(sent, _)] = backend.acted
        assert sent.decisions == []

    def test_an_answer_reads_its_ids_as_text_and_its_action_as_a_choice(self) -> None:
        answer = ApprovalAnswer.of({"requirement_id": 5, "action": "confirm"})
        assert answer.requirement_id == "5"
        assert answer.action is ApprovalAction.CONFIRM


class TestChatReads:
    async def test_the_context_is_the_count_the_limit_and_the_breakdown(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.COUNT_CONTEXT_TOKENS: 1234,
                RpcMethod.GET_CONTEXT_BREAKDOWN: {"total": 1234},
                RpcMethod.GET_STATUS: STATUS,
            }
        )
        got = await ChatReadMembers(host_of(gateway)).context(a_call())
        assert (got.tokens, got.max, got.breakdown) == (1234, 8000, {"total": 1234})

    async def test_a_count_that_is_absent_is_zero_tokens(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.COUNT_CONTEXT_TOKENS: None,
                RpcMethod.GET_CONTEXT_BREAKDOWN: {},
                RpcMethod.GET_STATUS: {},
            }
        )
        got = await ChatReadMembers(host_of(gateway)).context(a_call())
        assert (got.tokens, got.max) == (0, 0)

    async def test_the_timeout_is_a_whole_number(self) -> None:
        gateway = Gateway({RpcMethod.GET_RUN_TIMEOUT: "600"})
        assert await ChatReadMembers(host_of(gateway)).timeout(a_call()) == 600

    @pytest.mark.parametrize("answer", [True, 1, "yes"])
    async def test_processing_is_a_truth_value(self, answer: Any) -> None:
        gateway = Gateway({RpcMethod.GET_PROCESSING: answer})
        assert await ChatReadMembers(host_of(gateway)).processing(a_call()) is True

    async def test_the_history_is_asked_for_the_calls_session(self) -> None:
        gateway = Gateway({RpcMethod.GET_CHAT_HISTORY: ["a"]})
        got = await ChatReadMembers(host_of(gateway)).messages(a_call())
        assert got == ["a"]
        assert gateway.calls == [(RpcMethod.GET_CHAT_HISTORY, {"session_id": "s1"}, "s1")]

    async def test_pending_and_interrupted_ask_for_the_calls_session(self) -> None:
        gateway = Gateway(
            {RpcMethod.GET_PENDING_MESSAGES: [1], RpcMethod.GET_INTERRUPTED_RUNS: [2]}
        )
        members = ChatReadMembers(host_of(gateway))
        assert await members.pending(a_call()) == [1]
        assert await members.interrupted_runs(a_call()) == [2]
        assert [a for _, a, _ in gateway.calls] == [{"session_id": "s1"}] * 2

    async def test_the_status_is_the_sessions_own(self) -> None:
        gateway = Gateway({RpcMethod.GET_STATUS: STATUS})
        got = await ChatReadMembers(host_of(gateway)).status(a_call())
        assert (got.session_id, got.model, got.max_context) == ("s1", "small", 8000)

    @pytest.mark.parametrize(("given", "sent"), [(None, 50), (0, 50), (7, 7)])
    async def test_a_search_defaults_its_limit_to_fifty(self, given: Any, sent: int) -> None:
        gateway = Gateway({RpcMethod.SEARCH_CHAT: []})
        params = {"query": "needle", "limit": given}
        await ChatReadMembers(host_of(gateway)).search(a_call(**params))
        assert gateway.calls == [
            (RpcMethod.SEARCH_CHAT, {"session_id": "s1", "query": "needle", "limit": sent}, "s1")
        ]


class TestChatWrites:
    COMPACT = {
        RpcMethod.COUNT_CONTEXT_TOKENS: 7000,
        RpcMethod.GET_STATUS: STATUS,
    }

    async def test_a_compaction_is_asked_with_the_count_and_the_limit_and_answered(self) -> None:
        gateway = Gateway(
            {
                **self.COMPACT,
                RpcMethod.COMPACT_IF_NEEDED: {
                    "compacted": True,
                    "new_session_id": "s2",
                    "summary": "Gist",
                },
            }
        )
        got = await ChatWriteMembers(host_of(gateway)).compact(a_call())
        assert (RpcMethod.COMPACT_IF_NEEDED, {"ctx_tokens": 7000, "max_ctx": 8000}, "s1") in (
            gateway.calls
        )
        assert got == CompactResult(session_id="s1", new_session_id="s2", summary="Gist")

    async def test_nothing_compacted_is_answered_as_nothing(self) -> None:
        gateway = Gateway({**self.COMPACT, RpcMethod.COMPACT_IF_NEEDED: {"compacted": False}})
        assert await ChatWriteMembers(host_of(gateway)).compact(a_call()) is None

    async def test_an_absent_count_is_asked_as_zero(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.COUNT_CONTEXT_TOKENS: None,
                RpcMethod.GET_STATUS: STATUS,
                RpcMethod.COMPACT_IF_NEEDED: {"compacted": False},
            }
        )
        await ChatWriteMembers(host_of(gateway)).compact(a_call())
        assert gateway.calls[-1][1] == {"ctx_tokens": 0, "max_ctx": 8000}

    async def test_each_write_names_its_rpc_and_arguments(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.CANCEL_RUN: None,
                RpcMethod.CANCEL_AGENT_RUN: None,
                RpcMethod.TRUNCATE_HISTORY: {"ok": True},
                RpcMethod.EXTRACT_LEARNINGS: None,
                RpcMethod.DISCARD_INTERRUPTED_RUN: {"ok": True},
                RpcMethod.UPLOAD_ATTACHMENT: {"path": "/tmp/a"},
            }
        )
        members = ChatWriteMembers(host_of(gateway))
        await members.cancel(a_call())
        await members.cancel_agent(a_call(run_id="r1"))
        assert await members.truncate(a_call(run_id="r1")) == {"ok": True}
        assert await members.extract_learnings(a_call(user_msg="q", assistant_msg="a")) is None
        assert await members.discard_interrupted(a_call(message_id="m1")) == {"ok": True}
        assert await members.upload(a_call(filename="é.txt", content_base64="Zm9v")) == {
            "path": "/tmp/a"
        }
        assert gateway.calls == [
            (RpcMethod.CANCEL_RUN, {}, "s1"),
            (RpcMethod.CANCEL_AGENT_RUN, {"run_id": "r1"}, "s1"),
            (RpcMethod.TRUNCATE_HISTORY, {"session_id": "s1", "run_id": "r1"}, "s1"),
            (RpcMethod.EXTRACT_LEARNINGS, {"user_msg": "q", "assistant_msg": "a"}, "s1"),
            (
                RpcMethod.DISCARD_INTERRUPTED_RUN,
                {"session_id": "s1", "message_id": "m1"},
                "s1",
            ),
            (RpcMethod.UPLOAD_ATTACHMENT, {"filename": "é.txt", "content_base64": "Zm9v"}, "s1"),
        ]

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    async def test_a_blank_post_is_invalid_with_or_without_attachments(self, text: str) -> None:
        members = ChatWriteMembers(host_of(Gateway({})))
        with pytest.raises(HostModelError) as caught:
            await members.post(a_call(text=text, attachments={"a.txt": "x"}))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == "an empty message is not posted"

    @pytest.mark.parametrize("text", ["", "  "])
    async def test_a_blank_queue_is_invalid(self, text: str) -> None:
        members = ChatWriteMembers(host_of(Gateway({})))
        with pytest.raises(HostModelError) as caught:
            await members.queue(a_call(text=text))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == "an empty message is not queued"


class TestCommands:
    async def test_the_skill_names_are_strings(self) -> None:
        gateway = Gateway({RpcMethod.GET_SKILL_NAMES: ["review", "plan"]})
        assert await CommandMembers(host_of(gateway)).skills(a_call()) == ["review", "plan"]

    async def test_no_skills_is_an_empty_list(self) -> None:
        gateway = Gateway({RpcMethod.GET_SKILL_NAMES: []})
        assert await CommandMembers(host_of(gateway)).skills(a_call()) == []

    async def test_a_skill_name_that_is_not_text_is_a_failure_naming_it(self) -> None:
        gateway = Gateway({RpcMethod.GET_SKILL_NAMES: [["x"]]})
        with pytest.raises(HostModelError) as caught:
            await CommandMembers(host_of(gateway)).skills(a_call())
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message.startswith(
            "commands.skills answered with something a skill name does not describe (0:"
        )

    async def test_a_slash_command_of_a_kind_nobody_knows_is_a_failure(self) -> None:
        gateway = Gateway({RpcMethod.GET_SLASH_COMMANDS: [{"source": "alien", "name": "/x"}]})
        with pytest.raises(HostModelError) as caught:
            await CommandMembers(host_of(gateway)).command_list(a_call())
        assert caught.value.code == ErrorCode.FAILED
        assert "a slash command" in caught.value.message

    async def test_the_skill_definitions_and_details_are_typed_rows(self) -> None:
        gateway = Gateway(
            {
                RpcMethod.GET_SKILL_DEFINITIONS: [
                    {"name": "review", "description": "Reviews", "prompt": "Review it."}
                ],
                RpcMethod.GET_SKILL_DETAILS: [{"name": "review", "description": "Reviews"}],
            }
        )
        members = CommandMembers(host_of(gateway))
        [definition] = await members.skill_definitions(a_call())
        [detail] = await members.skill_details(a_call())
        assert (definition.name, detail.name) == ("review", "review")

    async def test_a_skill_list_that_is_not_a_list_is_a_failure(self) -> None:
        gateway = Gateway({RpcMethod.GET_SKILL_DETAILS: None})
        with pytest.raises(HostModelError) as caught:
            await CommandMembers(host_of(gateway)).skill_details(a_call())
        assert caught.value.message == "commands.skill_details answered with NoneType, not a list"

    @pytest.mark.parametrize("command", ["hello", "", "  ", " hello /x"])
    async def test_a_command_not_starting_with_a_slash_is_not_run(self, command: str) -> None:
        gateway = Gateway({})
        with pytest.raises(HostModelError) as caught:
            await CommandMembers(host_of(gateway)).run(a_call(command=command))
        assert caught.value.code == ErrorCode.INVALID
        assert caught.value.message == f"{command.strip()!r} is not a slash command"
        assert gateway.commands == []

    async def test_a_command_is_trimmed_run_in_the_session_and_its_outcome_returned(self) -> None:
        gateway = Gateway({})
        got = await CommandMembers(host_of(gateway)).run(a_call(command="  /ctx now \n"))
        assert gateway.commands == [("/ctx now", "s1")]
        assert (got.kind, got.content) == ("info", "ok")


def test_the_version_is_the_installed_one() -> None:
    from igni_core.extensions.host.members.app import AppMembers

    members = AppMembers(host_of(Gateway({})))
    import asyncio

    assert asyncio.run(members.version(a_call())) == __version__
