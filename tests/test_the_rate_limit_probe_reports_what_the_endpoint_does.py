"""``scripts/probe_rate_limits.py`` fires bursts at a chat endpoint and tabulates how it answers.

The probe is a maintainer's tool, so what it must get right is what it
reports: a 429, a throttling message in a 200 body, a timeout and a dead
connection are different rows of the table, the table's percentages and
latencies are computed as printed, and the probe stops climbing once the
endpoint is clearly throttling. Requests go to a real local HTTP server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "probe_rate_limits.py"
_ENV = ("IGNI_TEST_LLM_API_KEY", "IGNI_TEST_LLM_BASE_URL", "IGNI_TEST_LLM_MODEL", "PROBE_SENTINEL")


@pytest.fixture
def probe(monkeypatch):
    for name in _ENV:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    spec = importlib.util.spec_from_file_location("probe_rate_limits_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Endpoint:
    """A local chat endpoint answering every POST with a fixed status and body."""

    def __init__(self, status: int, body: str, delay: float = 0.0):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                outer.requests.append(json.loads(raw))
                outer.headers.append(dict(self.headers))
                if delay:
                    threading.Event().wait(delay)
                data = body.encode()
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint():
    made: list[_Endpoint] = []

    def make(status=200, body="{}", delay=0.0):
        e = _Endpoint(status, body, delay)
        made.append(e)
        return e

    yield make
    for e in made:
        e.close()


def _request(probe, url, timeout=5.0):
    async def go():
        async with httpx.AsyncClient() as client:
            return await probe._one_request(
                client, url + "/chat/completions", {}, {"m": 1}, timeout
            )

    return asyncio.run(go())


class TestClassifyStatusBody:
    @pytest.mark.parametrize(
        "status, body, kind",
        [
            (429, "", "rate"),
            (429, "anything", "rate"),
            (500, "upstream exploded", "other"),
            (503, "Service is THROTTLED", "rate"),
            (400, "Quota exceeded for this key", "rate"),
            (400, "too many requests", "rate"),
            (502, "tps limit", "rate"),
            (401, "bad key", "other"),
            (399, "rate limit", "ok"),
            (200, "rate limit", "ok"),
        ],
    )
    def test_the_status_and_the_body_decide_the_kind(self, probe, status, body, kind):
        assert probe._classify_status_body(status, body) == kind


class TestOneRequest:
    def test_a_plain_success_is_ok(self, probe, endpoint):
        outcome = _request(probe, endpoint(200, '{"choices": []}').url)

        assert outcome.kind == "ok"
        assert outcome.latency_ms > 0

    def test_a_429_is_rate_with_the_body_as_detail(self, probe, endpoint):
        outcome = _request(probe, endpoint(429, "slow down" + "x" * 500).url)

        assert outcome.kind == "rate"
        assert outcome.detail == ("slow down" + "x" * 500)[:200]

    def test_a_server_error_without_a_hint_is_other(self, probe, endpoint):
        assert _request(probe, endpoint(500, "boom").url).kind == "other"

    def test_a_200_with_a_base_resp_throttle_message_is_rate(self, probe, endpoint):
        body = json.dumps({"base_resp": {"status_code": 1002, "status_msg": "rate limit reached"}})

        outcome = _request(probe, endpoint(200, body).url)

        assert (outcome.kind, outcome.detail) == ("rate", "rate limit reached")

    def test_a_200_with_another_base_resp_error_is_other(self, probe, endpoint):
        body = json.dumps({"base_resp": {"status_code": 2013, "status_msg": "invalid params"}})

        outcome = _request(probe, endpoint(200, body).url)

        assert (outcome.kind, outcome.detail) == ("other", "invalid params")

    def test_a_base_resp_status_zero_is_ok(self, probe, endpoint):
        body = json.dumps({"base_resp": {"status_code": 0, "status_msg": "success"}})

        assert _request(probe, endpoint(200, body).url).kind == "ok"

    def test_a_body_that_is_not_json_is_still_ok_on_a_2xx(self, probe, endpoint):
        assert _request(probe, endpoint(200, "plain text, not json").url).kind == "ok"

    def test_a_response_slower_than_the_limit_is_a_timeout(self, probe, endpoint):
        outcome = _request(probe, endpoint(200, "{}", delay=1.5).url, timeout=0.2)

        assert outcome.kind == "timeout"

    def test_a_refused_connection_is_conn(self, probe):
        outcome = _request(probe, "http://127.0.0.1:1/v1")

        assert outcome.kind == "conn"
        assert outcome.detail

    def test_any_other_http_error_is_conn_too(self, probe):
        async def go():
            def refuse(request):
                raise httpx.ProtocolError("bad framing")

            async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
                return await probe._one_request(client, "http://x/v1", {}, {}, 1.0)

        outcome = asyncio.run(go())

        assert outcome.kind == "conn"
        assert "bad framing" in outcome.detail


class TestRunCell:
    def test_it_fires_that_many_identical_one_token_requests(self, probe, endpoint):
        e = endpoint(200, "{}")

        async def go():
            async with httpx.AsyncClient() as client:
                return await probe._run_cell(
                    client,
                    e.url + "/chat/completions",
                    {"Authorization": "Bearer k"},
                    "the-model",
                    "the prompt",
                    5,
                    5.0,
                )

        outcomes = asyncio.run(go())

        assert [o.kind for o in outcomes] == ["ok"] * 5
        assert len(e.requests) == 5
        assert all(r == e.requests[0] for r in e.requests)
        assert e.requests[0] == {
            "model": "the-model",
            "messages": [{"role": "user", "content": "the prompt"}],
            "max_tokens": 1,
            "temperature": 0,
        }
        assert e.headers[0]["Authorization"] == "Bearer k"

    def test_zero_concurrency_fires_nothing(self, probe, endpoint):
        e = endpoint()

        async def go():
            async with httpx.AsyncClient() as client:
                return await probe._run_cell(client, e.url, {}, "m", "p", 0, 1.0)

        assert asyncio.run(go()) == []
        assert e.requests == []


class TestFormatting:
    def test_a_percentage_is_whole_and_right_aligned(self, probe):
        assert probe._fmt_pct(1, 3) == " 33%"
        assert probe._fmt_pct(3, 3) == "100%"
        assert probe._fmt_pct(0, 4) == "  0%"

    def test_a_percentage_of_nothing_is_a_dash(self, probe):
        assert probe._fmt_pct(0, 0) == "  -  "

    def test_no_latencies_is_a_dash(self, probe):
        assert probe._fmt_ms([]) == "    -"

    def test_under_twenty_samples_p99_is_the_slowest(self, probe):
        assert probe._fmt_ms([1000.0, 2000.0, 9000.0]) == "p50=  2.0s p99=  9.0s"

    def test_from_twenty_samples_p99_is_the_99th_percentile(self, probe):
        values = [float(i * 1000) for i in range(1, 21)]  # 1s .. 20s

        assert probe._fmt_ms(values) == "p50= 10.5s p99= 19.0s"


class TestMakePrompt:
    def test_without_tiktoken_the_size_follows_four_characters_a_token(self, probe, monkeypatch):
        monkeypatch.setitem(sys.modules, "tiktoken", None)

        small, large = probe._make_prompt(1000), probe._make_prompt(4000)

        assert abs(len(small) - 4000) < 150
        assert abs(len(large) - 16000) < 150
        assert small.startswith("Read the context below. Reply with the single word READY.")
        assert small.endswith("</context>\n\nReply: ")

    def test_a_prompt_is_the_same_every_time(self, probe, monkeypatch):
        monkeypatch.setitem(sys.modules, "tiktoken", None)

        assert probe._make_prompt(2500) == probe._make_prompt(2500)

    def test_with_tiktoken_the_token_count_lands_on_the_target(self, probe, monkeypatch):
        class Encoding:
            def encode(self, text):
                return text.split()

        fake = types.SimpleNamespace(get_encoding=lambda name: Encoding())
        monkeypatch.setitem(sys.modules, "tiktoken", fake)

        prompt = probe._make_prompt(2000)

        assert 1900 <= len(prompt.split()) <= 2100
        assert prompt == probe._make_prompt(2000)

    def test_a_target_smaller_than_the_framing_still_returns_the_framing(self, probe, monkeypatch):
        monkeypatch.setitem(sys.modules, "tiktoken", None)

        prompt = probe._make_prompt(1)

        assert "READY" in prompt and "<context>" in prompt


class TestLoadDotenv:
    def test_values_are_loaded_but_the_environment_wins(self, probe, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PROBE_SENTINEL", "from-env")
        (tmp_path / ".env").write_text(
            "# a comment = not a setting\n"
            "no equals sign here\n"
            "  # indented comment=1\n"
            "IGNI_TEST_LLM_MODEL = spaced-model \n"
            "PROBE_SENTINEL=from-file\n"
            "IGNI_TEST_LLM_BASE_URL=http://host/v1?a=b\n"
        )

        probe._load_dotenv()

        import os

        assert os.environ["IGNI_TEST_LLM_MODEL"] == "spaced-model"
        assert os.environ["PROBE_SENTINEL"] == "from-env"
        assert os.environ["IGNI_TEST_LLM_BASE_URL"] == "http://host/v1?a=b"
        assert "a comment" not in os.environ

    def test_quoted_values_are_used_without_their_quotes(self, probe, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "IGNI_TEST_LLM_API_KEY=\"double-quoted\"\nIGNI_TEST_LLM_MODEL='single-quoted'\n"
        )

        probe._load_dotenv()

        import os

        assert os.environ["IGNI_TEST_LLM_API_KEY"] == "double-quoted"
        assert os.environ["IGNI_TEST_LLM_MODEL"] == "single-quoted"

    def test_no_env_file_is_no_problem(self, probe, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)

        probe._load_dotenv()


class TestMain:
    def _run(self, probe, monkeypatch, tmp_path, *argv):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["probe_rate_limits.py", *argv])
        return asyncio.run(probe.main())

    def test_without_a_key_it_exits_one_and_says_which_variable(
        self, probe, monkeypatch, tmp_path, capsys
    ):
        code = self._run(probe, monkeypatch, tmp_path)

        assert code == 1
        assert "IGNI_TEST_LLM_API_KEY" in capsys.readouterr().err

    def test_it_prints_a_row_per_cell_against_the_configured_endpoint(
        self, probe, endpoint, monkeypatch, tmp_path, capsys
    ):
        e = endpoint(200, "{}")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "the-key")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", e.url)
        monkeypatch.setenv("IGNI_TEST_LLM_MODEL", "the-model")

        code = self._run(
            probe,
            monkeypatch,
            tmp_path,
            "--sizes",
            "10,",
            "--levels",
            "1,2,",
            "--cooldown",
            "0",
        )

        out = capsys.readouterr().out
        assert code == 0
        assert f"Endpoint: {e.url}" in out and "Model:    the-model" in out
        assert "Sizes:    [10]" in out and "Levels:   [1, 2]" in out
        rows = [line.split() for line in out.splitlines() if line.split()[:1] == ["10"]]
        assert [r[1:4] for r in rows] == [["1", "1", "0"], ["2", "2", "0"]]
        assert len(e.requests) == 3
        assert all(r["model"] == "the-model" for r in e.requests)
        assert e.headers[0]["Authorization"] == "Bearer the-key"

    def test_the_defaults_are_the_openai_endpoint_and_model(
        self, probe, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setattr(probe, "_run_cell", _no_cells)

        self._run(probe, monkeypatch, tmp_path, "--sizes", "5", "--levels", "1", "--cooldown", "0")

        out = capsys.readouterr().out
        assert "Endpoint: https://api.openai.com/v1" in out
        assert "Model:    gpt-4o-mini" in out

    def test_it_stops_climbing_a_size_once_throttling_reaches_the_threshold(
        self, probe, endpoint, monkeypatch, tmp_path, capsys
    ):
        e = endpoint(429, "slow down")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", e.url)

        self._run(
            probe,
            monkeypatch,
            tmp_path,
            "--sizes",
            "10,20",
            "--levels",
            "1,2,4",
            "--cooldown",
            "0",
            "--stop-rl-pct",
            "50",
        )

        out = capsys.readouterr().out
        # conc 1 is never a reason to stop; conc 2 is fully throttled, so 4 is skipped
        # for each size, and the next size starts over at conc 1.
        assert out.count("stop: rate-limited ≥ 50% at conc=2") == 2
        assert len(e.requests) == (1 + 2) * 2
        assert "rate: slow down" in out

    def test_a_threshold_not_reached_keeps_climbing(
        self, probe, endpoint, monkeypatch, tmp_path, capsys
    ):
        e = endpoint(200, "{}")
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setenv("IGNI_TEST_LLM_BASE_URL", e.url)

        self._run(
            probe,
            monkeypatch,
            tmp_path,
            "--sizes",
            "10",
            "--levels",
            "1,2,4",
            "--cooldown",
            "0",
        )

        assert "stop:" not in capsys.readouterr().out
        assert len(e.requests) == 7

    def test_a_size_below_a_thousand_is_labelled_plain_and_above_in_k(
        self, probe, monkeypatch, tmp_path, capsys
    ):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")
        monkeypatch.setattr(probe, "_run_cell", _no_cells)

        self._run(
            probe, monkeypatch, tmp_path, "--sizes", "500,2000", "--levels", "1", "--cooldown", "0"
        )

        labels = [
            line.split()[0]
            for line in capsys.readouterr().out.splitlines()
            if line.strip().endswith(" -") and not line.startswith("-")
        ]
        assert labels == ["500", "2k"]

    def test_a_size_that_is_not_a_number_is_refused(self, probe, monkeypatch, tmp_path):
        monkeypatch.setenv("IGNI_TEST_LLM_API_KEY", "k")

        with pytest.raises(ValueError):
            self._run(probe, monkeypatch, tmp_path, "--sizes", "ten")


async def _no_cells(*_args, **_kwargs):
    return []
