"""``host.processes``, ``host.scheduler``, ``host.workflows``, ``host.tasks``,
and the two things they share: an answer read back through the model that
produced it (``reads``) and the tag that says which member a method is.

Driven over a scripted gateway (see ``Gateway``): what each member sends
igni (the RPC, its arguments, the session) and what it answers or
refuses is the whole of what is checked.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter

from igni_core.backend.schemas_processes import ProcessRow, ProcessTailResult, StopProcessResult
from igni_core.backend.schemas_rpc import RunShellResult
from igni_core.backend.schemas_workflows import WorkflowRunResponse
from igni_core.core.scheduler.models import ScheduledTask
from igni_core.extensions.host.errors import HostModelError
from igni_core.extensions.host.members.processes import ProcessMembers
from igni_core.extensions.host.members.reads import read_answer, read_answers, read_list
from igni_core.extensions.host.members.scheduler import SchedulerMembers
from igni_core.extensions.host.members.tag import (
    MemberBinding,
    MemberKey,
    MemberTag,
    call,
    get,
    put,
)
from igni_core.extensions.host.members.tasks import TaskMembers
from igni_core.extensions.host.members.workflows import WorkflowMembers
from igni_core.extensions.host.spec import ErrorCode
from igni_core.extensions.host.wire import Op
from igni_core.protocol.rpc import RpcMethod
from tests.test_the_host_members_translate_what_an_extension_asks import Gateway, a_call, host_of

ROW = {"pid": 7, "cmd": "sleep 9", "elapsed_seconds": 1.5}
TASK = {"id": "t1", "description": "nightly build", "scheduled_at": "2030-01-01T00:00:00"}


def refusal(caught: pytest.ExceptionInfo[HostModelError]) -> tuple[str, str]:
    return str(caught.value.code), caught.value.message


class TestProcesses:
    async def test_the_list_is_the_backends_rows_read_through_their_model(self) -> None:
        gateway = Gateway({RpcMethod.LIST_BACKGROUND_PROCESSES: [ROW, {**ROW, "pid": 8}]})
        rows = await ProcessMembers(host_of(gateway)).process_list(a_call())
        assert [type(r) for r in rows] == [ProcessRow, ProcessRow]
        assert [(r.pid, r.cmd, r.is_running, r.exit_code) for r in rows] == [
            (7, "sleep 9", True, None),
            (8, "sleep 9", True, None),
        ]
        assert gateway.calls == [(RpcMethod.LIST_BACKGROUND_PROCESSES, {}, "s1")]

    async def test_no_shells_is_an_empty_list(self) -> None:
        gateway = Gateway({RpcMethod.LIST_BACKGROUND_PROCESSES: []})
        assert await ProcessMembers(host_of(gateway)).process_list(a_call()) == []

    async def test_an_answer_that_is_not_a_list_is_refused_naming_the_member(self) -> None:
        gateway = Gateway({RpcMethod.LIST_BACKGROUND_PROCESSES: {"pid": 7}})
        with pytest.raises(HostModelError) as caught:
            await ProcessMembers(host_of(gateway)).process_list(a_call())
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "processes.list answered with dict, not a list",
        )

    async def test_a_row_without_a_pid_is_refused_naming_the_field(self) -> None:
        bad = {"cmd": "x", "elapsed_seconds": 1}
        gateway = Gateway({RpcMethod.LIST_BACKGROUND_PROCESSES: [bad]})
        with pytest.raises(HostModelError) as caught:
            await ProcessMembers(host_of(gateway)).process_list(a_call())
        code, message = refusal(caught)
        assert code == ErrorCode.FAILED
        assert message.startswith("processes.list answered with something ProcessRow does not")
        assert "(pid: " in message

    async def test_a_tail_asks_for_the_pid_and_the_lines_wanted(self) -> None:
        answer = {"pid": 7, "output": "hi\n", "is_running": True, "exit_code": None}
        gateway = Gateway({RpcMethod.READ_PROCESS_TAIL: answer})
        got = await ProcessMembers(host_of(gateway)).tail(a_call(pid=7, lines=30))
        assert isinstance(got, ProcessTailResult)
        assert (got.pid, got.output, got.is_running) == (7, "hi\n", True)
        assert gateway.calls == [(RpcMethod.READ_PROCESS_TAIL, {"pid": 7, "tail": 30}, "s1")]

    @pytest.mark.parametrize("lines", [None, 0])
    async def test_a_tail_with_no_lines_asks_for_two_hundred(self, lines: Any) -> None:
        answer = {"pid": 7, "output": "", "is_running": False, "exit_code": 0}
        gateway = Gateway({RpcMethod.READ_PROCESS_TAIL: answer})
        await ProcessMembers(host_of(gateway)).tail(a_call(pid=7, lines=lines))
        assert gateway.calls[0][1] == {"pid": 7, "tail": 200}

    async def test_a_tail_with_no_lines_key_asks_for_two_hundred(self) -> None:
        answer = {"pid": 7, "output": "", "is_running": False, "exit_code": 0}
        gateway = Gateway({RpcMethod.READ_PROCESS_TAIL: answer})
        await ProcessMembers(host_of(gateway)).tail(a_call(pid=7))
        assert gateway.calls[0][1] == {"pid": 7, "tail": 200}

    async def test_a_tail_without_a_pid_is_a_missing_parameter(self) -> None:
        gateway = Gateway({})
        with pytest.raises(KeyError, match="pid"):
            await ProcessMembers(host_of(gateway)).tail(a_call())
        assert gateway.calls == []

    async def test_a_malformed_tail_is_refused_naming_the_member(self) -> None:
        gateway = Gateway({RpcMethod.READ_PROCESS_TAIL: {"pid": 7}})
        with pytest.raises(HostModelError) as caught:
            await ProcessMembers(host_of(gateway)).tail(a_call(pid=7))
        code, message = refusal(caught)
        assert code == ErrorCode.FAILED
        assert message.startswith(
            "processes.tail answered with something ProcessTailResult does not describe ("
        )

    async def test_a_stop_names_the_pid_and_answers_what_happened(self) -> None:
        answer = {"pid": 7, "killed": True, "message": "stopped 7"}
        gateway = Gateway({RpcMethod.STOP_BACKGROUND_PROCESS: answer})
        got = await ProcessMembers(host_of(gateway)).stop(a_call(pid=7))
        assert isinstance(got, StopProcessResult)
        assert (got.killed, got.message) == (True, "stopped 7")
        assert gateway.calls == [(RpcMethod.STOP_BACKGROUND_PROCESS, {"pid": 7}, "s1")]

    async def test_an_unknown_pid_stop_is_the_backends_killed_false(self) -> None:
        answer = {"pid": 99, "killed": False, "message": "no such process"}
        gateway = Gateway({RpcMethod.STOP_BACKGROUND_PROCESS: answer})
        got = await ProcessMembers(host_of(gateway)).stop(a_call(pid=99))
        assert (got.killed, got.message) == (False, "no such process")

    async def test_a_stop_answered_with_nothing_is_refused(self) -> None:
        gateway = Gateway({RpcMethod.STOP_BACKGROUND_PROCESS: None})
        with pytest.raises(HostModelError) as caught:
            await ProcessMembers(host_of(gateway)).stop(a_call(pid=7))
        assert refusal(caught)[1].startswith("processes.stop answered with something")

    async def test_a_run_sends_the_command_and_answers_its_output_and_exit_code(self) -> None:
        gateway = Gateway({RpcMethod.RUN_SHELL: {"output": "a\n", "exit_code": 3}})
        got = await ProcessMembers(host_of(gateway)).run(a_call(command="echo a; exit 3"))
        assert isinstance(got, RunShellResult)
        assert (got.output, got.exit_code) == ("a\n", 3)
        assert gateway.calls == [(RpcMethod.RUN_SHELL, {"command": "echo a; exit 3"}, "s1")]

    async def test_a_run_without_a_command_is_a_missing_parameter(self) -> None:
        gateway = Gateway({})
        with pytest.raises(KeyError, match="command"):
            await ProcessMembers(host_of(gateway)).run(a_call())

    async def test_a_malformed_run_answer_is_refused_naming_the_member(self) -> None:
        gateway = Gateway({RpcMethod.RUN_SHELL: {"output": 1, "exit_code": "x"}})
        with pytest.raises(HostModelError) as caught:
            await ProcessMembers(host_of(gateway)).run(a_call(command="ls"))
        assert refusal(caught)[1].startswith(
            "processes.run answered with something RunShellResult does not describe ("
        )


class TestScheduler:
    async def test_the_tasks_include_the_done_ones_and_are_read_through_their_model(self) -> None:
        done = {**TASK, "id": "t2", "status": "completed", "result": "built"}
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: [TASK, done]})
        tasks = await SchedulerMembers(host_of(gateway)).tasks(a_call())
        assert [type(t) for t in tasks] == [ScheduledTask, ScheduledTask]
        assert [(t.id, t.status.value, t.result) for t in tasks] == [
            ("t1", "pending", ""),
            ("t2", "completed", "built"),
        ]
        assert gateway.calls == [(RpcMethod.GET_SCHEDULED_TASKS, {"include_done": True}, "s1")]

    async def test_no_tasks_is_an_empty_list(self) -> None:
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: []})
        assert await SchedulerMembers(host_of(gateway)).tasks(a_call()) == []

    async def test_a_listing_that_is_not_a_list_is_refused(self) -> None:
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: "none"})
        with pytest.raises(HostModelError) as caught:
            await SchedulerMembers(host_of(gateway)).tasks(a_call())
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "scheduler.tasks answered with str, not a list",
        )

    async def test_a_task_that_does_not_fit_is_refused_naming_the_field(self) -> None:
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: [{"id": "t1"}]})
        with pytest.raises(HostModelError) as caught:
            await SchedulerMembers(host_of(gateway)).tasks(a_call())
        code, message = refusal(caught)
        assert code == ErrorCode.FAILED
        assert message.startswith(
            "scheduler.tasks answered with something ScheduledTask does not describe (description: "
        )

    async def test_start_asks_the_backend_to_start_and_answers_nothing(self) -> None:
        gateway = Gateway({RpcMethod.START_SCHEDULER: {"anything": 1}})
        assert await SchedulerMembers(host_of(gateway)).start(a_call()) is None
        assert gateway.calls == [(RpcMethod.START_SCHEDULER, {}, "s1")]

    async def test_cancel_names_the_task_and_answers_nothing(self) -> None:
        gateway = Gateway({RpcMethod.CANCEL_SCHEDULED_TASK: {"cancelled": True}})
        assert await SchedulerMembers(host_of(gateway)).cancel(a_call(task_id="t1")) is None
        assert gateway.calls == [(RpcMethod.CANCEL_SCHEDULED_TASK, {"task_id": "t1"}, "s1")]

    async def test_run_now_runs_the_description_of_the_task_it_found_and_answers_its_result(
        self,
    ) -> None:
        other = {**TASK, "id": "t0", "description": "something else"}
        gateway = Gateway(
            {
                RpcMethod.GET_SCHEDULED_TASKS: [other, TASK],
                RpcMethod.EXECUTE_SCHEDULED_TASK: {"result": "ran nightly"},
            }
        )
        got = await SchedulerMembers(host_of(gateway)).run_now(a_call(task_id="t1"))
        assert got == "ran nightly"
        assert gateway.calls == [
            (RpcMethod.GET_SCHEDULED_TASKS, {"include_done": True}, "s1"),
            (RpcMethod.EXECUTE_SCHEDULED_TASK, {"description": "nightly build"}, "s1"),
        ]

    async def test_run_now_takes_a_task_id_that_is_not_a_string_as_its_text(self) -> None:
        numeric = {**TASK, "id": "42"}
        gateway = Gateway(
            {
                RpcMethod.GET_SCHEDULED_TASKS: [numeric],
                RpcMethod.EXECUTE_SCHEDULED_TASK: {"result": "ok"},
            }
        )
        assert await SchedulerMembers(host_of(gateway)).run_now(a_call(task_id=42)) == "ok"

    @pytest.mark.parametrize("tasks", [[], [TASK]])
    async def test_run_now_of_an_unknown_task_is_not_found_and_runs_nothing(
        self, tasks: list[dict[str, Any]]
    ) -> None:
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: tasks})
        with pytest.raises(HostModelError) as caught:
            await SchedulerMembers(host_of(gateway)).run_now(a_call(task_id="t9"))
        assert refusal(caught) == (ErrorCode.NOT_FOUND, "no scheduled task 't9'")
        assert [m for m, _, _ in gateway.calls] == [RpcMethod.GET_SCHEDULED_TASKS]

    async def test_run_now_over_a_listing_that_is_not_a_list_is_refused_before_running(
        self,
    ) -> None:
        gateway = Gateway({RpcMethod.GET_SCHEDULED_TASKS: None})
        with pytest.raises(HostModelError) as caught:
            await SchedulerMembers(host_of(gateway)).run_now(a_call(task_id="t1"))
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "scheduler.tasks answered with NoneType, not a list",
        )
        assert [m for m, _, _ in gateway.calls] == [RpcMethod.GET_SCHEDULED_TASKS]


class TestWorkflows:
    async def test_the_list_is_the_workflows_with_their_phases(self) -> None:
        item = {
            "name": "release",
            "description": "ship it",
            "whenToUse": "on a tag",
            "phases": [{"title": "build"}],
            "path": ".igni/workflows/release.js",
        }
        gateway = Gateway({RpcMethod.LIST_WORKFLOWS: [item]})
        listed = await WorkflowMembers(host_of(gateway)).workflow_list(a_call())
        assert [(w.name, w.when_to_use, [p.title for p in w.phases]) for w in listed] == [
            ("release", "on a tag", ["build"])
        ]
        assert gateway.calls == [(RpcMethod.LIST_WORKFLOWS, {}, "s1")]

    async def test_a_workflow_list_that_is_not_a_list_is_refused(self) -> None:
        gateway = Gateway({RpcMethod.LIST_WORKFLOWS: {"workflows": []}})
        with pytest.raises(HostModelError) as caught:
            await WorkflowMembers(host_of(gateway)).workflow_list(a_call())
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "workflows.list answered with dict, not a list",
        )

    async def test_a_run_starts_the_named_workflow_with_its_input_in_the_callers_session(
        self,
    ) -> None:
        gateway = Gateway({RpcMethod.RUN_WORKFLOW: {"workflow_run_id": "r1", "name": "release"}})
        got = await WorkflowMembers(host_of(gateway)).run(
            a_call(name="release", input={"tag": "v1"})
        )
        assert isinstance(got, WorkflowRunResponse)
        assert (got.workflow_run_id, got.name) == ("r1", "release")
        assert gateway.calls == [
            (
                RpcMethod.RUN_WORKFLOW,
                {"name": "release", "args": {"tag": "v1"}, "session_id": "s1"},
                "s1",
            )
        ]

    @pytest.mark.parametrize("params", [{}, {"input": None}, {"input": {}}])
    async def test_a_run_with_no_input_sends_an_empty_one(self, params: dict[str, Any]) -> None:
        gateway = Gateway({RpcMethod.RUN_WORKFLOW: {"workflow_run_id": "r1", "name": "w"}})
        await WorkflowMembers(host_of(gateway)).run(a_call(name="w", **params))
        assert gateway.calls[0][1]["args"] == {}

    async def test_a_run_without_a_name_is_a_missing_parameter(self) -> None:
        gateway = Gateway({})
        with pytest.raises(KeyError, match="name"):
            await WorkflowMembers(host_of(gateway)).run(a_call())

    async def test_a_run_answer_with_a_stray_field_is_refused(self) -> None:
        answer = {"workflow_run_id": "r1", "name": "w", "extra": 1}
        gateway = Gateway({RpcMethod.RUN_WORKFLOW: answer})
        with pytest.raises(HostModelError) as caught:
            await WorkflowMembers(host_of(gateway)).run(a_call(name="w"))
        assert refusal(caught)[1].startswith(
            "workflows.run answered with something WorkflowRunResponse does not describe (extra: "
        )

    @pytest.mark.parametrize("cancelled", [True, False])
    async def test_a_cancel_answers_whether_the_run_was_stopped(self, cancelled: bool) -> None:
        answer = {"cancelled": cancelled, "workflow_run_id": "r1"}
        gateway = Gateway({RpcMethod.CANCEL_WORKFLOW: answer})
        got = await WorkflowMembers(host_of(gateway)).cancel(a_call(workflow_run_id="r1"))
        assert got is cancelled
        assert gateway.calls == [(RpcMethod.CANCEL_WORKFLOW, {"workflow_run_id": "r1"}, "s1")]

    async def test_a_cancel_answer_without_the_flag_is_refused(self) -> None:
        gateway = Gateway({RpcMethod.CANCEL_WORKFLOW: {"workflow_run_id": "r1"}})
        with pytest.raises(HostModelError) as caught:
            await WorkflowMembers(host_of(gateway)).cancel(a_call(workflow_run_id="r1"))
        assert refusal(caught)[1].startswith(
            "workflows.cancel answered with something WorkflowCancelResponse does not describe ("
        )


class TestTasks:
    async def test_the_todos_are_the_agents_items(self) -> None:
        items = [
            {"content": "Run tests", "status": "pending", "activeForm": "Running tests"},
            {"content": "Fix", "status": "completed", "activeForm": "Fixing"},
        ]
        gateway = Gateway({RpcMethod.GET_TODOS: items})
        todos = await TaskMembers(host_of(gateway)).todos(a_call())
        assert [(t.content, str(t.status)) for t in todos] == [
            ("Run tests", "pending"),
            ("Fix", "completed"),
        ]
        assert gateway.calls == [(RpcMethod.GET_TODOS, {}, "s1")]

    async def test_no_todos_is_an_empty_list(self) -> None:
        gateway = Gateway({RpcMethod.GET_TODOS: []})
        assert await TaskMembers(host_of(gateway)).todos(a_call()) == []

    async def test_an_item_with_an_unknown_status_is_refused_naming_the_field(self) -> None:
        bad = {"content": "x", "status": "someday", "activeForm": "xing"}
        gateway = Gateway({RpcMethod.GET_TODOS: [bad]})
        with pytest.raises(HostModelError) as caught:
            await TaskMembers(host_of(gateway)).todos(a_call())
        code, message = refusal(caught)
        assert code == ErrorCode.FAILED
        assert message.startswith(
            "tasks.todos answered with something TodoItemWire does not describe (status: "
        )

    async def test_an_answer_that_is_not_a_list_is_refused(self) -> None:
        gateway = Gateway({RpcMethod.GET_TODOS: None})
        with pytest.raises(HostModelError) as caught:
            await TaskMembers(host_of(gateway)).todos(a_call())
        assert refusal(caught) == (
            ErrorCode.FAILED,
            "tasks.todos answered with NoneType, not a list",
        )


class Row(BaseModel):
    name: str
    inner: dict[str, int] = {}


class Deep(BaseModel):
    row: Row


class TestReadingAnAnswer:
    def test_an_answer_that_fits_is_the_model(self) -> None:
        assert read_answer(Row, {"name": "a"}, method="m.x") == Row(name="a")

    def test_the_first_failing_field_is_named_with_its_whole_path(self) -> None:
        with pytest.raises(HostModelError) as caught:
            read_answer(Deep, {"row": {"name": "a", "inner": {"k": "no"}}}, method="m.x")
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message.startswith(
            "m.x answered with something Deep does not describe (row.inner.k: "
        )

    def test_only_the_first_of_several_failures_is_named(self) -> None:
        with pytest.raises(HostModelError) as caught:
            read_answer(Row, {"name": 1, "inner": 2}, method="m.x")
        message = caught.value.message
        assert "(name: " in message
        assert "inner" not in message

    @pytest.mark.parametrize("answer", [None, "text", 3, ["name"]])
    def test_an_answer_that_is_not_an_object_is_refused_not_half_read(self, answer: Any) -> None:
        with pytest.raises(HostModelError) as caught:
            read_answer(Row, answer, method="m.x")
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message.startswith("m.x answered with something Row does not describe")

    def test_a_validation_error_with_no_errors_is_a_plain_sentence(self) -> None:
        from pydantic import ValidationError

        from igni_core.extensions.host.members.reads import _why

        empty = ValidationError.from_exception_data("Row", [])
        assert _why("m.x", "Row", empty) == "m.x answered with something Row does not describe"

    def test_a_list_is_each_row_read_in_order(self) -> None:
        got = read_answers(Row, [{"name": "a"}, {"name": "b"}], method="m.x")
        assert [r.name for r in got] == ["a", "b"]
        assert read_answers(Row, [], method="m.x") == []

    @pytest.mark.parametrize(
        ("answer", "kind"), [(None, "NoneType"), ({"name": "a"}, "dict"), ("ab", "str")]
    )
    def test_something_that_is_not_a_list_is_refused_with_what_it_was(
        self, answer: Any, kind: str
    ) -> None:
        with pytest.raises(HostModelError) as caught:
            read_answers(Row, answer, method="m.x")
        assert caught.value.message == f"m.x answered with {kind}, not a list"

    def test_one_bad_row_refuses_the_whole_list(self) -> None:
        with pytest.raises(HostModelError) as caught:
            read_answers(Row, [{"name": "a"}, {"nope": 1}], method="m.x")
        assert "(name: " in caught.value.message

    def test_a_list_read_through_a_type_names_what_it_expected(self) -> None:
        adapter = TypeAdapter(list[str])
        assert read_list(adapter, ["a", "b"], method="m.x", what="the names") == ["a", "b"]
        with pytest.raises(HostModelError) as caught:
            read_list(adapter, ["a", 1], method="m.x", what="the names")
        assert caught.value.code == ErrorCode.FAILED
        assert caught.value.message.startswith(
            "m.x answered with something the names does not describe (1: "
        )

    @pytest.mark.parametrize("answer", [None, "ab", {"a": 1}])
    def test_a_list_read_through_a_type_refuses_what_is_not_a_list(self, answer: Any) -> None:
        with pytest.raises(HostModelError) as caught:
            read_list(TypeAdapter(list[str]), answer, method="m.x", what="the names")
        assert caught.value.code == ErrorCode.FAILED


class TestTheTag:
    def test_each_tag_marks_an_op_and_a_path_and_gives_the_function_back(self) -> None:
        async def one(c: Any) -> None: ...

        for tag, op in ((get, Op.GET), (put, Op.SET), (call, Op.CALL)):
            assert tag("a.b")(one) is one
            assert MemberTag.of(one) == MemberKey(op=op, path="a.b")

    def test_an_untagged_function_implements_nothing(self) -> None:
        async def plain(c: Any) -> None: ...

        assert MemberTag.of(plain) is None
        assert MemberTag.of(None) is None

    def test_an_attribute_of_the_same_name_that_is_not_a_key_is_not_a_tag(self) -> None:
        async def plain(c: Any) -> None: ...

        plain._host_member = ("get", "a.b")  # type: ignore[attr-defined]
        assert MemberTag.of(plain) is None

    def test_a_bound_method_carries_its_functions_tag(self) -> None:
        class Mine:
            @get("a.b")
            async def one(self, c: Any) -> None: ...

        assert MemberTag.of(Mine().one) == MemberKey(op=Op.GET, path="a.b")

    def test_one_path_under_two_ops_is_two_keys(self) -> None:
        assert MemberKey(op=Op.GET, path="a.b") != MemberKey(op=Op.SET, path="a.b")
        assert len({MemberKey(op=Op.GET, path="a.b"), MemberKey(op=Op.SET, path="a.b")}) == 2

    def test_a_key_and_a_binding_cannot_be_changed(self) -> None:
        from pydantic import ValidationError

        key = MemberKey(op=Op.GET, path="a.b")
        with pytest.raises(ValidationError):
            key.path = "other"  # type: ignore[misc]

        async def impl(c: Any) -> None: ...

        binding = MemberBinding(key=key, impl=impl)
        assert binding.impl is impl
        with pytest.raises(ValidationError):
            binding.key = MemberKey(op=Op.SET, path="x")  # type: ignore[misc]
