"""``scripts/codewrite_judge.py`` re-judges two codewrite eval runs.

It is an ad-hoc eval tool that talks to an LLM endpoint, so nothing ran
it and nothing said what it does with the model's habits: a ``<think>``
block before the answer, markdown fences, a prose preamble, Python-style
quotes, an answer that is not JSON at all. Those are exactly the cases it
exists for, and each one decides whether a case is scored or silently
zeroed.

The endpoint is a ``httpx.MockTransport`` that answers like a chat
completions API; the script itself runs unmodified from a copy placed
beside a scratch ``.env`` so the key fallback reads that file and not the
maintainer's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import sys

import httpx
import pytest
from pydantic import ValidationError

_SOURCE = pathlib.Path(__file__).resolve().parent.parent / "scripts/codewrite_judge.py"

_GOOD = {
    "located": {"score": 3, "reason": "looked"},
    "reused": {"score": 2, "reason": "mostly"},
    "not_duplicated": {"score": 1, "reason": "partly"},
    "hallucinations": ["Foo.bar"],
    "overall": "PASS",
}


@pytest.fixture
def judge(tmp_path, monkeypatch):
    """The script, loaded from ``<tmp>/scripts`` so ``<tmp>/.env`` is its project .env."""
    for name in ("IGNI_TEST_LLM_API_KEY", "IGNI_TEST_LLM_BASE_URL", "IGNI_TEST_LLM_MODEL"):
        # The script's .env fallback writes os.environ; have it undone afterwards.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    (tmp_path / "scripts").mkdir()
    copy = tmp_path / "scripts/codewrite_judge.py"
    shutil.copy(_SOURCE, copy)
    spec = importlib.util.spec_from_file_location("codewrite_judge_under_test", copy)
    module = importlib.util.module_from_spec(spec)
    # pydantic resolves the script's postponed annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Endpoint:
    """A chat-completions endpoint that replays prepared answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "nope"})
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def _judge_one(judge, endpoint, base_url="http://llm.test/v1"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            return await judge.judge_one(
                client,
                base_url=base_url,
                api_key="k-123",
                model_id="m-1",
                case_name="case-a",
                mode="WITH",
                task_input=" the task ",
                expected_output=" expected ",
                judge_guidelines=" rubric ",
                response_text=" the answer ",
            )

    return asyncio.run(go())


class TestExtractJson:
    @pytest.mark.parametrize(
        "text",
        [
            '{"a": 1}',
            '<think>{"a": 99} let me think\n</think>\n{"a": 1}',
            '```json\n{"a": 1}\n```',
            '```\n{"a": 1}\n```',
            'Let me analyze this.\n{"a": 1}\nHope that helps.',
            "{'a': 1}",
            "  \n<think>x</think>```json\n{'a': 1}\n```  ",
        ],
    )
    def test_it_finds_the_object_however_the_model_wrapped_it(self, judge, text):
        assert judge.extract_json(text) == {"a": 1}

    @pytest.mark.parametrize("text", ["no json here", "", "{not: json}", "<think>{}</think>"])
    def test_it_raises_when_nothing_can_be_recovered(self, judge, text):
        with pytest.raises(json.JSONDecodeError):
            judge.extract_json(text)


class TestTheScoreModels:
    @pytest.mark.parametrize("score", [-1, 4])
    def test_a_score_outside_zero_to_three_is_rejected(self, judge, score):
        with pytest.raises(ValidationError):
            judge.AxisScore(score=score, reason="r")

    @pytest.mark.parametrize("score", [0, 3])
    def test_the_bounds_are_allowed(self, judge, score):
        assert judge.AxisScore(score=score, reason="r").score == score


class TestJudgeOne:
    def test_it_posts_the_rubric_at_temperature_zero_with_the_key(self, judge):
        endpoint = _Endpoint([json.dumps(_GOOD)])

        result = _judge_one(judge, endpoint, base_url="http://llm.test/v1")

        request = endpoint.requests[0]
        assert str(request.url) == "http://llm.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer k-123"
        body = endpoint.bodies()[0]
        assert body["model"] == "m-1" and body["temperature"] == 0.0
        prompt = body["messages"][1]["content"]
        for part in ("the task", "expected", "rubric", "the answer"):
            assert part in prompt
        assert result.case_name == "case-a" and result.mode == "WITH"
        assert result.located.score == 3 and result.not_duplicated.score == 1
        assert result.hallucinations == ["Foo.bar"] and result.overall == "PASS"

    def test_a_think_block_and_fences_around_the_answer_are_fine(self, judge):
        wrapped = "<think>hmm</think>\n```json\n" + json.dumps(_GOOD) + "\n```"
        endpoint = _Endpoint([wrapped])

        result = _judge_one(judge, endpoint)

        assert result.reused.score == 2
        assert len(endpoint.requests) == 1

    def test_an_unparseable_answer_is_asked_again_with_a_stricter_prompt(self, judge):
        endpoint = _Endpoint(["Let me analyze...", json.dumps(_GOOD)])

        result = _judge_one(judge, endpoint)

        assert result.overall == "PASS" and result.located.score == 3
        first, second = endpoint.bodies()
        assert "previous answer was not valid JSON" in second["messages"][0]["content"]
        assert "previous answer" not in first["messages"][0]["content"]

    def test_an_out_of_range_score_goes_down_the_retry_path(self, judge):
        bad = {**_GOOD, "located": {"score": 9, "reason": "x"}}
        endpoint = _Endpoint([json.dumps(bad), json.dumps(_GOOD)])

        result = _judge_one(judge, endpoint)

        assert result.located.score == 3
        assert len(endpoint.requests) == 2

    def test_a_missing_field_goes_down_the_retry_path(self, judge):
        incomplete = {k: v for k, v in _GOOD.items() if k != "overall"}
        endpoint = _Endpoint([json.dumps(incomplete), json.dumps(_GOOD)])

        assert _judge_one(judge, endpoint).overall == "PASS"

    def test_when_the_retry_fails_too_the_case_scores_zero_and_fails(self, judge):
        endpoint = _Endpoint(["prose", "more prose"])

        result = _judge_one(judge, endpoint)

        assert result.overall == "FAIL"
        assert [result.located.score, result.reused.score, result.not_duplicated.score] == [0, 0, 0]
        assert result.hallucinations == []
        assert "judge parse failure" in result.located.reason

    def test_a_failing_retry_request_also_scores_zero(self, judge):
        endpoint = _Endpoint(["prose", 500])

        result = _judge_one(judge, endpoint)

        assert result.overall == "FAIL" and result.located.score == 0

    def test_an_http_error_on_the_first_call_is_not_swallowed(self, judge):
        with pytest.raises(httpx.HTTPStatusError):
            _judge_one(judge, _Endpoint([500]))


class TestLoadRun:
    def test_it_returns_the_mode_and_the_cases(self, judge, tmp_path):
        path = tmp_path / "run.json"
        path.write_text(json.dumps({"mode": "with-index", "cases": [{"x": 1}]}))

        assert judge.load_run(path) == ("with-index", [{"x": 1}])

    def test_the_mode_falls_back_to_the_file_stem(self, judge, tmp_path):
        path = tmp_path / "codewrite-without.json"
        path.write_text(json.dumps({"cases": []}))

        assert judge.load_run(path) == ("codewrite-without", [])


def _case(name, response="resp"):
    return {
        "case": {"name": name, "input": "task", "expected_output": "e", "judge_guidelines": "g"},
        "response_text": response,
    }


def _write_runs(tmp_path, with_cases, without_cases):
    with_path, without_path = tmp_path / "w.json", tmp_path / "wo.json"
    with_path.write_text(json.dumps({"mode": "with", "cases": with_cases}))
    without_path.write_text(json.dumps({"mode": "without", "cases": without_cases}))
    return with_path, without_path


def _main(judge, monkeypatch, endpoint, *argv):
    real = httpx.AsyncClient
    monkeypatch.setattr(judge.httpx, "AsyncClient", lambda: real(transport=httpx.MockTransport(endpoint)))
    monkeypatch.setattr(sys, "argv", ["codewrite_judge.py", *argv])
    return asyncio.run(judge.main())


class TestMain:
    def test_without_a_key_it_exits_two_and_says_so(self, judge, monkeypatch, tmp_path, capsys):
        with_path, without_path = _write_runs(tmp_path, [_case("a")], [_case("a")])
        endpoint = _Endpoint([])

        code = _main(judge, monkeypatch, endpoint, "--with", str(with_path), "--without", str(without_path))

        assert code == 2
        assert "IGNI_TEST_LLM_API_KEY not set" in capsys.readouterr().err
        assert endpoint.requests == []

    def test_the_key_is_read_from_the_project_env_but_only_igni_test_llm_lines(
        self, judge, monkeypatch, tmp_path
    ):
        (tmp_path / ".env").write_text(
            "OTHER_SECRET=nope\n"
            "IGNI_TEST_LLM_API_KEY=from-file\n"
            "IGNI_TEST_LLM_BASE_URL=http://file.test/v9/\n"
            "IGNI_TEST_LLM_MODEL=file-model\n"
        )
        with_path, without_path = _write_runs(tmp_path, [_case("a")], [])
        endpoint = _Endpoint([json.dumps(_GOOD)])

        code = _main(judge, monkeypatch, endpoint, "--with", str(with_path), "--without", str(without_path))

        assert code == 0
        request = endpoint.requests[0]
        assert request.headers["authorization"] == "Bearer from-file"
        assert str(request.url) == "http://file.test/v9/chat/completions"
        assert endpoint.bodies()[0]["model"] == "file-model"
        assert "OTHER_SECRET" not in os.environ

    def test_a_quoted_key_in_the_env_file_is_used_without_its_quotes(self, judge, monkeypatch, tmp_path):
        (tmp_path / ".env").write_text('IGNI_TEST_LLM_API_KEY="quoted-key"\n')
        with_path, without_path = _write_runs(tmp_path, [_case("a")], [])
        endpoint = _Endpoint([json.dumps(_GOOD)])

        _main(judge, monkeypatch, endpoint, "--with", str(with_path), "--without", str(without_path))

        assert endpoint.requests[0].headers["authorization"] == "Bearer quoted-key"

    def test_the_environment_wins_and_the_defaults_apply(self, judge, monkeypatch, tmp_path):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "from-env")
        (tmp_path / ".env").write_text("IGNI_TEST_LLM_API_KEY=from-file\n")
        with_path, without_path = _write_runs(tmp_path, [_case("a")], [])
        endpoint = _Endpoint([json.dumps(_GOOD)])

        _main(judge, monkeypatch, endpoint, "--with", str(with_path), "--without", str(without_path))

        request = endpoint.requests[0]
        assert request.headers["authorization"] == "Bearer from-env"
        assert str(request.url) == "https://api.minimax.io/v1/chat/completions"
        assert endpoint.bodies()[0]["model"] == "MiniMax-M2.7"

    def test_it_prints_a_row_per_case_and_per_mode_totals_and_writes_the_judgements(
        self, judge, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        weak = {
            "located": {"score": 1, "reason": "r"},
            "reused": {"score": 0, "reason": "r"},
            "not_duplicated": {"score": 2, "reason": "r"},
            "hallucinations": [],
            "overall": "FAIL",
        }
        with_path, without_path = _write_runs(tmp_path, [_case("one"), _case("two")], [_case("one")])
        endpoint = _Endpoint([json.dumps(_GOOD), json.dumps(_GOOD), json.dumps(weak)])
        out_path = tmp_path / "nested/deeper/judged.json"

        code = _main(
            judge, monkeypatch, endpoint,
            "--with", str(with_path), "--without", str(without_path), "--out", str(out_path),
        )

        assert code == 0
        out = capsys.readouterr().out
        assert "WITH     totals: A=6/6  B=4/6  C=2/6  hallucinations=2  overall=2/2" in out
        assert "WITHOUT  totals: A=1/3  B=0/3  C=2/3  hallucinations=0  overall=0/1" in out
        assert "PASS (WITH)" in out and "FAIL (WITHOUT)" in out
        saved = json.loads(out_path.read_text())
        assert [(j["case_name"], j["mode"]) for j in saved] == [
            ("one", "WITH"), ("two", "WITH"), ("one", "WITHOUT"),
        ]
        assert saved[0]["located"] == {"score": 3, "reason": "looked"}

    def test_a_run_with_no_cases_prints_no_totals_and_does_not_crash(
        self, judge, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        with_path, without_path = _write_runs(tmp_path, [], [])

        code = _main(judge, monkeypatch, _Endpoint([]), "--with", str(with_path), "--without", str(without_path))

        assert code == 0
        assert "totals" not in capsys.readouterr().out

    def test_a_case_missing_its_optional_fields_is_judged_with_blanks(self, judge, monkeypatch, tmp_path):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        bare = {"case": {"name": "bare", "input": "task"}}
        with_path, without_path = _write_runs(tmp_path, [bare], [])
        endpoint = _Endpoint([json.dumps(_GOOD)])

        assert _main(judge, monkeypatch, endpoint, "--with", str(with_path), "--without", str(without_path)) == 0
        assert len(endpoint.requests) == 1
