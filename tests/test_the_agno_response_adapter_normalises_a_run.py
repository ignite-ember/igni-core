"""What the Agno response adapter reports about a run, and what it leaves alone.

Real adapter, plain namespaces for the Agno objects (the adapter reads them
by attribute only), so every assertion is about the adapter's own logic.
"""

from types import SimpleNamespace as NS

import pytest

from igni_core.core.evals.agno_adapter import AgnoResponseAdapter


def call(call_id, name="read_file", key="id"):
    return {key: call_id, "function": {"name": name}}


def assistant(*calls, from_history=False):
    return NS(role="assistant", tool_calls=list(calls) or None, from_history=from_history)


def tool_reply(call_id, content="ok", error=False):
    return NS(role="tool", tool_call_id=call_id, content=content, tool_call_error=error)


def run(messages=None, tools=None, content=None):
    return NS(messages=messages, tools=tools, content=content)


class TestOutputText:
    def test_a_string_is_returned_as_it_is(self):
        assert AgnoResponseAdapter(run(content="hello")).output_text == "hello"

    def test_other_content_is_stringified(self):
        assert AgnoResponseAdapter(run(content={"a": 1})).output_text == "{'a': 1}"

    def test_missing_content_is_an_empty_string(self):
        assert AgnoResponseAdapter(run(content=None)).output_text == ""

    def test_an_empty_string_stays_empty(self):
        assert AgnoResponseAdapter(run(content="")).output_text == ""


class TestHasMessages:
    def test_a_list_counts_even_when_empty(self):
        assert AgnoResponseAdapter(run(messages=[])).has_messages() is True

    def test_none_messages_means_the_run_errored(self):
        assert AgnoResponseAdapter(run(messages=None)).has_messages() is False

    def test_no_response_at_all(self):
        assert AgnoResponseAdapter(None).has_messages() is False


class TestStripFromHistory:
    def test_replayed_messages_are_dropped_from_a_copy(self):
        old, new = assistant(call("1"), from_history=True), assistant(call("2"))
        original = run(messages=[old, new])
        adapter = AgnoResponseAdapter(original)

        assert adapter.strip_from_history() is adapter

        assert adapter.response.messages == [new]
        assert original.messages == [old, new]
        assert adapter.response is not original

    def test_nothing_to_drop_keeps_the_very_same_response(self):
        original = run(messages=[assistant(call("1"))])
        adapter = AgnoResponseAdapter(original).strip_from_history()
        assert adapter.response is original

    def test_missing_messages_are_left_alone(self):
        original = run(messages=None)
        assert AgnoResponseAdapter(original).strip_from_history().response is original

    def test_history_tool_calls_do_not_reach_the_names(self):
        msgs = [assistant(call("1", "old_tool"), from_history=True), assistant(call("2", "new"))]
        names = AgnoResponseAdapter(run(messages=msgs)).strip_from_history().called_tool_names()
        assert names == {"new"}


class TestStripErroredToolCalls:
    def test_a_flagged_reply_removes_its_call_only(self):
        a = assistant(call("1", "bad"), call("2", "good"))
        original = run(messages=[a, tool_reply("1", "boom", error=True), tool_reply("2")])
        adapter = AgnoResponseAdapter(original)

        assert adapter.strip_errored_tool_calls() is adapter

        kept = adapter.response.messages[0]
        assert [c["function"]["name"] for c in kept.tool_calls] == ["good"]
        # the original message and response are untouched
        assert [c["function"]["name"] for c in a.tool_calls] == ["bad", "good"]
        assert original.messages[0] is a
        assert adapter.response is not original

    @pytest.mark.parametrize("marker", AgnoResponseAdapter.ERROR_MARKERS)
    def test_each_marker_in_a_reply_marks_the_call_errored(self, marker):
        msgs = [assistant(call("1", "bad")), tool_reply("1", f"x {marker} y")]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls is None

    def test_a_rejected_tool_name_marks_the_call_errored(self):
        msgs = [assistant(call("1", "Read")), tool_reply("1", "Function Read not found")]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls is None

    def test_a_call_with_every_call_removed_gets_none(self):
        msgs = [assistant(call("1")), tool_reply("1", error=True)]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls is None

    def test_the_id_may_sit_under_tool_call_id(self):
        msgs = [assistant(call("1", key="tool_call_id")), tool_reply("1", error=True)]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls is None

    def test_an_errored_reply_without_an_id_strips_nothing(self):
        original = run(messages=[assistant(call("1")), tool_reply(None, error=True)])
        adapter = AgnoResponseAdapter(original).strip_errored_tool_calls()
        assert adapter.response is original

    def test_a_clean_run_keeps_the_same_response(self):
        original = run(messages=[assistant(call("1")), tool_reply("1", "fine")])
        assert AgnoResponseAdapter(original).strip_errored_tool_calls().response is original

    def test_a_legitimate_result_mentioning_not_found_is_not_an_error(self):
        msgs = [assistant(call("1")), tool_reply("1", "file not found: no matches found")]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls == [call("1")]

    def test_a_result_that_merely_contains_the_word_function_is_not_an_error(self):
        """Reading source code returns text like ``Function parse``; that must
        not erase the read call from the trace."""
        msgs = [assistant(call("1")), tool_reply("1", "# Function parse splits the line")]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls == [call("1")]

    def test_a_message_that_cannot_be_copied_is_kept_unchanged(self):
        class Stubborn:
            role = "assistant"
            tool_calls = [call("1"), call("2")]

            def __copy__(self):
                raise RuntimeError("no copies")

        stubborn = Stubborn()
        msgs = [stubborn, tool_reply("1", error=True)]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0] is stubborn

    def test_non_tool_replies_are_never_errors(self):
        msgs = [assistant(call("1")), NS(role="user", tool_call_id="1", content="ValidationError")]
        adapter = AgnoResponseAdapter(run(messages=msgs)).strip_errored_tool_calls()
        assert adapter.response.messages[0].tool_calls == [call("1")]


class TestCalledToolNames:
    def test_run_tools_win(self):
        tools = [NS(tool_name="a"), NS(tool_name=None), NS(tool_name="b")]
        msgs = [assistant(call("1", "from_message"))]
        assert AgnoResponseAdapter(run(messages=msgs, tools=tools)).called_tool_names() == {
            "a",
            "b",
        }

    def test_message_tool_calls_are_the_fallback(self):
        msgs = [assistant(call("1", "x"), call("2", "y")), assistant()]
        assert AgnoResponseAdapter(run(messages=msgs, tools=[])).called_tool_names() == {"x", "y"}

    def test_malformed_entries_are_skipped(self):
        bad = ["text", {"function": None}, {"function": {}}, {"function": {"name": ""}}]
        msgs = [NS(tool_calls=bad), assistant(call("1", "ok"))]
        assert AgnoResponseAdapter(run(messages=msgs)).called_tool_names() == {"ok"}

    def test_nothing_called(self):
        assert AgnoResponseAdapter(run(messages=None, tools=None)).called_tool_names() == set()


class TestToolTrace:
    def test_one_entry_per_named_tool(self):
        tools = [
            NS(tool_name="read", tool_args={"p": 1}, result="data", tool_call_error=False),
            NS(tool_name="", tool_args={}, result="skipped"),
            NS(tool_name="write", tool_args={"p": 2}, result=None, tool_call_error=True),
        ]
        trace = AgnoResponseAdapter(run(tools=tools)).tool_trace()

        assert [(t.name, t.args, t.result_preview, t.error) for t in trace] == [
            ("read", {"p": 1}, "data", False),
            ("write", {"p": 2}, "", True),
        ]

    def test_the_preview_boundary_is_400_characters(self):
        exact, over = "a" * 400, "b" * 401
        tools = [NS(tool_name="t", result=exact), NS(tool_name="t", result=over)]
        trace = AgnoResponseAdapter(run(tools=tools)).tool_trace()

        assert trace[0].result_preview == exact
        assert trace[1].result_preview == "b" * 397 + "..."
        assert len(trace[1].result_preview) == 400

    def test_a_non_dict_result_is_stringified(self):
        trace = AgnoResponseAdapter(run(tools=[NS(tool_name="t", result=["x"])])).tool_trace()
        assert trace[0].result_preview == "['x']"

    @pytest.mark.parametrize("raw", [None, "text", ["a"], 3])
    def test_args_that_are_not_a_dict_become_none(self, raw):
        trace = AgnoResponseAdapter(run(tools=[NS(tool_name="t", tool_args=raw)])).tool_trace()
        assert trace[0].args is None

    def test_no_tools_gives_an_empty_trace(self):
        assert AgnoResponseAdapter(run(tools=None)).tool_trace() == []
