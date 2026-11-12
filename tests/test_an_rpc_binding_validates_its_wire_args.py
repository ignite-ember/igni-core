"""What an ``@rpc`` binding does with the arguments a call carries.

The wording of ``RpcArgsError`` is what a person writing a call reads, so
each shape of failure is pinned: a missing field, an unknown key, a value
of the wrong type, a nested error, a model-level error, and several at once.
"""

from __future__ import annotations

import pytest
from pydantic import field_validator, model_validator

from igni_core.backend.rpc_binding import (
    NoArgs,
    RpcArgs,
    RpcArgsError,
    RpcBinding,
    rpc,
)
from igni_core.protocol.rpc import RpcMethod, RpcScope


class _Inner(RpcArgs):
    count: int


class _Args(RpcArgs):
    name: str
    options: dict[str, int] = {}
    inner: _Inner | None = None


class _Checked(RpcArgs):
    a: int = 0
    b: int = 0

    @model_validator(mode="after")
    def _exclusive(self) -> _Checked:
        if self.a and self.b:
            raise ValueError("give a or b, not both")
        return self


class _Positive(RpcArgs):
    n: int

    @field_validator("n")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v


def _binding(args: type, method: RpcMethod = RpcMethod.GET_SCHEDULED_TASKS) -> RpcBinding:
    return RpcBinding(method, args, RpcScope.SESSION)


def _refusal(binding: RpcBinding, wire) -> str:
    with pytest.raises(RpcArgsError) as raised:
        binding.validate(wire)
    return str(raised.value)


class TestValidate:
    def test_wire_args_become_the_model(self):
        args = _binding(_Args).validate({"name": "x", "options": {"a": 1}})

        assert isinstance(args, _Args)
        assert (args.name, args.options) == ("x", {"a": 1})

    @pytest.mark.parametrize("wire", [None, {}])
    def test_no_wire_args_are_an_empty_call(self, wire):
        assert isinstance(_binding(NoArgs).validate(wire), NoArgs)

    @pytest.mark.parametrize("wire", [None, {}])
    def test_an_empty_call_to_a_method_with_a_required_field_says_so(self, wire):
        assert _refusal(_binding(_Args), wire) == "get_scheduled_tasks: 'name' is required"

    def test_an_unknown_key_is_not_an_argument_of_the_call(self):
        sentence = _refusal(_binding(NoArgs), {"stray": 1})

        assert sentence == "get_scheduled_tasks: 'stray' is not an argument of this call"

    def test_a_too_short_string_is_reported_as_required(self):
        from pydantic import Field

        class Named(RpcArgs):
            name: str = Field(min_length=1)

        assert _refusal(_binding(Named), {"name": ""}) == "get_scheduled_tasks: 'name' is required"

    def test_a_value_that_is_not_a_dict_names_the_type_it_was(self):
        assert (
            _refusal(_binding(_Args), {"name": "x", "options": "nope"})
            == "get_scheduled_tasks: 'options' must be a dict, got str"
        )
        assert (
            _refusal(_binding(_Args), {"name": "x", "options": [1]})
            == "get_scheduled_tasks: 'options' must be a dict, got list"
        )

    def test_a_nested_error_names_the_dotted_path_and_the_reason(self):
        sentence = _refusal(_binding(_Args), {"name": "x", "inner": {"count": "many"}})

        assert sentence.startswith("get_scheduled_tasks: 'inner.count': ")
        assert "integer" in sentence

    def test_a_field_rule_that_fails_gives_its_message(self):
        sentence = _refusal(_binding(_Positive), {"n": 0})

        assert sentence == "get_scheduled_tasks: 'n': Value error, must be positive"

    def test_an_error_about_the_whole_model_has_no_field_name(self):
        sentence = _refusal(_binding(_Checked), {"a": 1, "b": 2})

        assert sentence == "get_scheduled_tasks: Value error, give a or b, not both"

    def test_several_errors_are_joined_in_one_sentence(self):
        sentence = _refusal(_binding(_Args), {"stray": 1})

        assert sentence == (
            "get_scheduled_tasks: 'name' is required; "
            "get_scheduled_tasks: 'stray' is not an argument of this call"
        )

    def test_the_refusal_is_a_value_error_so_every_route_reports_it_as_a_refusal(self):
        assert issubclass(RpcArgsError, ValueError)

    def test_the_method_named_is_the_bindings_own(self):
        sentence = _refusal(_binding(NoArgs, RpcMethod.LOGIN), {"x": 1})

        assert sentence.startswith("login: ")

    def test_a_list_for_the_whole_call_is_refused_not_crashed(self):
        with pytest.raises(RpcArgsError):
            _binding(NoArgs).validate(["a"])  # type: ignore[arg-type]


class TestNoArgs:
    def test_accepts_only_an_empty_call(self):
        _binding(NoArgs).validate({})
        assert "'x' is not an argument" in _refusal(_binding(NoArgs), {"x": 1})


class TestCall:
    def test_the_handler_receives_the_validated_model(self):
        seen = []
        entry = _binding(_Args).call(lambda args: seen.append(args) or "answer")

        assert entry({"name": "x"}) == "answer"
        assert isinstance(seen[0], _Args)

    def test_a_call_that_does_not_fit_never_reaches_the_handler(self):
        seen: list = []
        entry = _binding(_Args).call(seen.append)

        with pytest.raises(RpcArgsError):
            entry({})

        assert seen == []


class _Handler:
    @rpc(RpcMethod.GET_SCHEDULED_TASKS, args=NoArgs, scope=RpcScope.SESSION)
    def tasks(self, args: NoArgs) -> None: ...

    def plain(self) -> None: ...

    not_callable = 3


class _Sub(_Handler):
    @rpc(RpcMethod.LOGIN, args=NoArgs, scope=RpcScope.IGNI)
    def login(self, args: NoArgs) -> None: ...


class _Twice:
    @rpc(RpcMethod.LOGIN, args=NoArgs, scope=RpcScope.IGNI)
    def one(self, args: NoArgs) -> None: ...

    @rpc(RpcMethod.LOGIN, args=NoArgs, scope=RpcScope.IGNI)
    def two(self, args: NoArgs) -> None: ...


class TestTheTag:
    def test_the_decorator_leaves_the_function_as_it_was(self):
        def fn(self, args): ...

        assert rpc(RpcMethod.LOGIN, args=NoArgs, scope=RpcScope.IGNI)(fn) is fn
        assert fn.__name__ == "fn"

    def test_the_binding_carries_what_was_declared(self):
        binding = RpcBinding.of(_Handler.tasks)

        assert binding is not None
        assert (binding.method, binding.args, binding.scope) == (
            RpcMethod.GET_SCHEDULED_TASKS,
            NoArgs,
            RpcScope.SESSION,
        )
        assert binding.streams == () and binding.may_stream == () and binding.correlate is None

    @pytest.mark.parametrize("attr", [_Handler.plain, _Handler.not_callable, None, "text"])
    def test_something_not_bound_has_no_binding(self, attr):
        assert RpcBinding.of(attr) is None

    def test_a_tag_that_is_not_a_binding_is_not_one(self):
        class Impostor:
            _igni_rpc_binding = "not a binding"

        assert RpcBinding.of(Impostor) is None

    def test_on_finds_each_bound_method_by_the_name_that_carries_it(self):
        found = RpcBinding.on(_Handler)

        assert {m: name for m, (name, _) in found.items()} == {
            RpcMethod.GET_SCHEDULED_TASKS: "tasks"
        }

    def test_on_includes_what_an_owner_inherits(self):
        assert set(RpcBinding.on(_Sub)) == {RpcMethod.GET_SCHEDULED_TASKS, RpcMethod.LOGIN}

    def test_two_methods_for_one_rpc_in_an_owner_is_an_error(self):
        with pytest.raises(RuntimeError, match=r"duplicate @rpc\(.*login.*\) binding in _Twice"):
            RpcBinding.on(_Twice)
