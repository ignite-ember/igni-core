"""The decisions service's edges: its routes, its refusals, its model's loading and its process.

The happy paths live in ``test_an_extension_asks_laya`` and
``test_decisions_run_in_their_own_service``. These pin the rest: what
each route answers and refuses, how the activity clock counts, how
the model loads, retries and keeps stdout clean, what the hub cache
says, and how the service process starts, binds and stops.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from huggingface_hub import constants as hub

from igni_core.core.decisions.laya_wire import LayaQuestions, LayaResult
from igni_core.core.decisions.schemas import (
    DecisionQuestion,
    DecisionRequest,
    DecisionsUnavailable,
    Priority,
)
from igni_core.decisions_server.__main__ import DecisionsServer, ReadyFile
from igni_core.decisions_server.app import Activity, DecisionsApp
from igni_core.decisions_server.model import (
    BUNDLE_REPO,
    PRESEED_HINT,
    HubCache,
    LayaModel,
    PriorityGate,
)

ROOT = Path(__file__).resolve().parents[1]
RESULT = {
    "answers": {"urgent": {"type": "noul", "noul": 0.8}},
    "usage": {},
    "routing": {"model": "english"},
}
QUESTION = {"id": "urgent", "kind": "noul", "question": "Is `text` urgent?"}
DECIDE = {"request": {"contexts": [{"text": "a"}, {"text": "b"}], "questions": [QUESTION]}}
PREDICT = {"state": {"tool_call": "ls"}, "questions": [QUESTION], "model": "typed-decisions"}
AUTH = {"Authorization": "Bearer s3cret"}


class StubModel:
    """A model that answers from a script and records what it was asked."""

    def __init__(self, *, fail: Exception | None = None, loaded: bool = False) -> None:
        self.fail = fail
        self.loaded = loaded
        self.calls: list[Any] = []

    def predict(self, state, questions, model, priority) -> LayaResult:
        self.calls.append(("predict", state, questions.wire(), model, priority))
        if self.fail:
            raise self.fail
        return LayaResult.model_validate(RESULT)

    def decide(self, request: DecisionRequest) -> list[LayaResult]:
        self.calls.append(("decide", len(request.contexts)))
        if self.fail:
            raise self.fail
        return [LayaResult.model_validate(RESULT) for _ in request.contexts]


def client_for(app: DecisionsApp) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app.asgi, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://decisions")


class TestTheRoutes:
    async def test_predict_answers_the_result_from_the_asked_checkpoint_and_priority(self) -> None:
        model = StubModel()
        async with client_for(DecisionsApp(model, "s3cret")) as http:
            reply = await http.post(
                "/v1/predict", json={**PREDICT, "priority": int(Priority.EXTENSION)}, headers=AUTH
            )
        assert reply.status_code == 200
        assert reply.json()["result"]["answers"]["urgent"]["noul"] == 0.8
        assert model.calls == [
            (
                "predict",
                {"tool_call": "ls"},
                LayaQuestions.of([DecisionQuestion(**QUESTION)]).wire(),
                "typed-decisions",
                Priority.EXTENSION,
            )
        ]

    async def test_predict_defaults_to_the_approval_priority(self) -> None:
        model = StubModel()
        async with client_for(DecisionsApp(model, "s3cret")) as http:
            await http.post("/v1/predict", json=PREDICT, headers=AUTH)
        assert model.calls[0][4] is Priority.APPROVAL

    async def test_decide_answers_one_result_per_context(self) -> None:
        async with client_for(DecisionsApp(StubModel(), "s3cret")) as http:
            reply = await http.post("/v1/decide", json=DECIDE, headers=AUTH)
        assert reply.status_code == 200
        assert len(reply.json()["results"]) == 2

    async def test_an_unavailable_model_is_a_503_with_the_reason(self) -> None:
        model = StubModel(fail=DecisionsUnavailable("no weights"))
        async with client_for(DecisionsApp(model, "s3cret")) as http:
            for path, body in (("/v1/predict", PREDICT), ("/v1/decide", DECIDE)):
                reply = await http.post(path, json=body, headers=AUTH)
                assert reply.status_code == 503
                assert reply.json() == {"error": "unavailable", "detail": "no weights"}

    async def test_a_model_error_that_is_not_unavailable_is_a_500_and_the_clock_still_runs(
        self,
    ) -> None:
        app = DecisionsApp(StubModel(fail=RuntimeError("boom")), "s3cret")
        app.activity.last = time.monotonic() - 100
        async with client_for(app) as http:
            reply = await http.post("/v1/decide", json=DECIDE, headers=AUTH)
        assert reply.status_code == 500
        assert app.activity.in_flight == 0
        assert app.activity.idle_for() < 5

    async def test_only_the_listed_methods_and_paths_are_routed(self) -> None:
        async with client_for(DecisionsApp(StubModel(), "s3cret")) as http:
            assert (await http.get("/v1/predict", headers=AUTH)).status_code == 405
            assert (await http.post("/v1/health", headers=AUTH)).status_code == 405
            assert (await http.get("/v1/nothing", headers=AUTH)).status_code == 404


class TestTheSecret:
    async def test_anything_but_exactly_the_bearer_secret_is_a_401(self) -> None:
        async with client_for(DecisionsApp(StubModel(), "s3cret")) as http:
            for headers in (
                {},
                {"Authorization": "Bearer wrong"},
                {"Authorization": "s3cret"},
                {"Authorization": "Basic s3cret"},
                {"Authorization": "bearer s3cret"},
                {"Authorization": "Bearer s3cret "},
                {"Authorization": "Bearer s3cret-and-more"},
            ):
                reply = await http.get("/v1/health", headers=headers)
                assert reply.status_code == 401, headers
                assert reply.json() == {
                    "error": "unauthorized",
                    "detail": "the decisions secret is required",
                }

    async def test_the_secret_is_checked_before_the_body_is_read(self) -> None:
        model = StubModel()
        async with client_for(DecisionsApp(model, "s3cret")) as http:
            reply = await http.post("/v1/predict", content=b"{garbage")
        assert reply.status_code == 401
        assert model.calls == []

    def test_an_empty_secret_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a secret"):
            DecisionsApp(StubModel(), "")


class TestHealth:
    async def test_it_reports_whether_the_model_is_loaded_without_loading_it(self) -> None:
        model = LayaModel()
        async with client_for(DecisionsApp(model, "s3cret")) as http:
            before = await http.get("/v1/health", headers=AUTH)
            model._router = object()  # noqa: SLF001 — a checkpoint now resident
            after = await http.get("/v1/health", headers=AUTH)
        assert before.json() == {"ok": True, "loaded": False}
        assert after.json() == {"ok": True, "loaded": True}


class TestTheActivityClock:
    def test_idle_time_counts_from_the_last_answer_and_is_zero_while_a_call_runs(self) -> None:
        activity = Activity()
        activity.last = time.monotonic() - 30
        assert 29 < activity.idle_for() < 40
        activity.in_flight = 2
        assert activity.idle_for() == 0.0

    async def test_concurrent_calls_are_counted_while_they_run(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class Slow(StubModel):
            def decide(self, request):
                entered.set()
                release.wait(5)
                return super().decide(request)

        app = DecisionsApp(Slow(), "s3cret")
        async with client_for(app) as http:
            import asyncio

            task = asyncio.create_task(http.post("/v1/decide", json=DECIDE, headers=AUTH))
            await asyncio.to_thread(entered.wait, 5)
            assert app.activity.in_flight == 1
            assert app.activity.idle_for() == 0.0
            release.set()
            assert (await task).status_code == 200
        assert app.activity.in_flight == 0


class FakeRouter:
    def __init__(self, *, result: Any = RESULT, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def predict(self, state, questions, model=None) -> Any:
        if self.error:
            raise self.error
        return self.result

    def predict_batch(self, batch) -> list[Any]:
        if self.error:
            raise self.error
        return [self.result for _ in batch]


def loaded(router: Any) -> LayaModel:
    model = LayaModel()
    model._router = router  # noqa: SLF001
    return model


QUESTIONS = LayaQuestions.model_validate(
    {"urgent": {"type": "noul", "instructions": "Is `text` urgent?"}}
)
REQUEST = DecisionRequest.model_validate(DECIDE["request"])


class TestWhatTheModelMakesOfARouterFailure:
    def test_decide_names_laya_s_own_limit_for_a_value_error(self) -> None:
        model = loaded(FakeRouter(error=ValueError("cannot read question q1")))
        with pytest.raises(DecisionsUnavailable, match="Laya could not answer: cannot read"):
            model.decide(REQUEST)

    def test_any_other_error_names_only_its_type(self) -> None:
        for call in (
            lambda m: m.decide(REQUEST),
            lambda m: m.predict({"t": "x"}, QUESTIONS, "english", Priority.APPROVAL),
        ):
            model = loaded(FakeRouter(error=RuntimeError("secret path /root/x")))
            with pytest.raises(DecisionsUnavailable) as caught:
                call(model)
            assert str(caught.value) == (
                "igni's decision model could not be loaded or run (RuntimeError)"
            )

    def test_a_malformed_result_is_unavailable_and_releases_the_gate(self) -> None:
        model = loaded(FakeRouter(result={"answers": "nope"}))
        with pytest.raises(DecisionsUnavailable, match="ValidationError"):
            model.predict({"t": "x"}, QUESTIONS, "english", Priority.APPROVAL)
        with pytest.raises(DecisionsUnavailable):
            model.decide(REQUEST)
        # the gate was released by both raises: a third holder is not stuck
        done = threading.Event()

        def hold() -> None:
            with model.gate.hold(Priority.APPROVAL):
                done.set()

        threading.Thread(target=hold, daemon=True).start()
        assert done.wait(2)

    def test_decide_hands_the_router_one_batch_in_context_order(self) -> None:
        seen: list[Any] = []

        class Spy(FakeRouter):
            def predict_batch(self, batch):
                seen.append(batch)
                return super().predict_batch(batch)

        loaded(Spy()).decide(REQUEST)
        [batch] = seen
        assert [item["state"] for item in batch] == [{"text": "a"}, {"text": "b"}]
        assert batch[0]["questions"] == QUESTIONS.wire()


class TestTheGate:
    def test_equal_priorities_go_in_arrival_order(self) -> None:
        gate = PriorityGate()
        holding, release = threading.Event(), threading.Event()
        order: list[int] = []

        def first() -> None:
            with gate.hold(Priority.EXTENSION):
                holding.set()
                release.wait(5)

        def waiter(n: int) -> None:
            with gate.hold(Priority.EXTENSION):
                order.append(n)

        threads = [threading.Thread(target=first)]
        threads[0].start()
        holding.wait(5)
        for n in range(4):
            t = threading.Thread(target=waiter, args=(n,))
            t.start()
            threads.append(t)
            time.sleep(0.05)
        release.set()
        for t in threads:
            t.join(5)
        assert order == [0, 1, 2, 3]

    def test_a_holder_that_raises_releases_the_gate(self) -> None:
        gate = PriorityGate()
        with pytest.raises(KeyError), gate.hold(Priority.APPROVAL):
            raise KeyError("x")
        done = threading.Event()

        def hold() -> None:
            with gate.hold(Priority.EXTENSION):
                done.set()

        threading.Thread(target=hold, daemon=True).start()
        assert done.wait(2)


class TestLoading:
    def test_a_failed_load_names_only_the_type_and_is_tried_again(self, monkeypatch) -> None:
        model = LayaModel()
        attempts: list[int] = []

        def build() -> Any:
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("disk path /secret")
            return "router"

        monkeypatch.setattr(model, "_build", build)
        with pytest.raises(DecisionsUnavailable) as caught:
            model.load()
        assert str(caught.value) == "igni's decision model could not be loaded (OSError)"
        assert not model.loaded
        assert model.load() == "router"
        assert model.loaded

    def test_a_missing_laya_is_unavailable(self, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "laya", None)  # import raises ImportError
        monkeypatch.delenv("IGNI_AIR_GAPPED", raising=False)
        with pytest.raises(
            DecisionsUnavailable, match="could not be loaded \\(ModuleNotFoundError\\)"
        ):
            LayaModel().load()

    def test_a_decisions_unavailable_from_the_build_passes_through_unwrapped(
        self, monkeypatch
    ) -> None:
        model = LayaModel()
        monkeypatch.setattr(
            model, "_build", lambda: (_ for _ in ()).throw(DecisionsUnavailable("as is"))
        )
        with pytest.raises(DecisionsUnavailable) as caught:
            model.load()
        assert str(caught.value) == "as is"

    def test_threads_loading_at_once_build_it_once(self, monkeypatch) -> None:
        model = LayaModel()
        builds: list[int] = []

        def build() -> Any:
            builds.append(1)
            time.sleep(0.2)
            return "router"

        monkeypatch.setattr(model, "_build", build)
        got: list[Any] = []
        threads = [threading.Thread(target=lambda: got.append(model.load())) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert got == ["router"] * 5
        assert len(builds) == 1


@pytest.fixture
def hub_cache(tmp_path, monkeypatch) -> Path:
    cache = tmp_path / "hub"
    cache.mkdir()
    monkeypatch.setattr(hub, "HF_HUB_CACHE", str(cache))
    monkeypatch.setattr(hub, "HF_HUB_OFFLINE", False)
    monkeypatch.delenv("IGNI_AIR_GAPPED", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    return cache


def seed(cache: Path, repo: str = BUNDLE_REPO, *, snapshot: bool = True) -> None:
    snapshots = cache / ("models--" + repo.replace("/", "--")) / "snapshots"
    snapshots.mkdir(parents=True)
    if snapshot:
        (snapshots / "abc123").mkdir()


def fake_laya(monkeypatch, factory) -> None:
    module = types.ModuleType("laya")
    module.Router = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "laya", module)


class TestTheHubCache:
    def test_a_repo_is_there_only_with_a_snapshot(self, hub_cache) -> None:
        assert not HubCache.has("owner/name")  # nothing at all
        (hub_cache / "models--owner--name").mkdir()
        assert not HubCache.has("owner/name")  # no snapshots directory
        (hub_cache / "models--owner--name" / "snapshots").mkdir()
        assert not HubCache.has("owner/name")  # an empty one
        (hub_cache / "models--owner--name" / "snapshots" / "rev").mkdir()
        assert HubCache.has("owner/name")

    def test_a_slash_in_the_repo_becomes_a_double_dash(self, hub_cache) -> None:
        seed(hub_cache, "a/b")
        assert HubCache.has("a/b")
        assert not HubCache.has("a--c")


class TestLoadingOnline:
    def test_a_cached_bundle_is_tried_offline_first_and_the_switch_is_removed(
        self, hub_cache, monkeypatch
    ) -> None:
        seed(hub_cache)
        seen: list[str | None] = []
        fake_laya(monkeypatch, lambda **kw: seen.append(os.environ.get("HF_HUB_OFFLINE")) or "r")
        assert LayaModel().load() == "r"
        assert seen == ["1"]
        assert "HF_HUB_OFFLINE" not in os.environ

    def test_a_failed_offline_load_falls_back_to_fetching(self, hub_cache, monkeypatch) -> None:
        seed(hub_cache)
        seen: list[str | None] = []

        def router(**kw: Any) -> str:
            seen.append(os.environ.get("HF_HUB_OFFLINE"))
            if len(seen) == 1:
                raise OSError("subfolder missing")
            return "r"

        fake_laya(monkeypatch, router)
        assert LayaModel().load() == "r"
        assert seen == ["1", None]
        assert "HF_HUB_OFFLINE" not in os.environ

    def test_an_uncached_bundle_loads_online_with_max_loaded_three(
        self, hub_cache, monkeypatch
    ) -> None:
        seen: list[Any] = []
        fake_laya(
            monkeypatch,
            lambda **kw: seen.append((os.environ.get("HF_HUB_OFFLINE"), kw)) or "r",
        )
        LayaModel().load()
        assert seen == [(None, {"max_loaded": 3})]

    def test_an_offline_setting_the_user_made_is_left_alone(self, hub_cache, monkeypatch) -> None:
        seed(hub_cache)
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        seen: list[str | None] = []
        fake_laya(monkeypatch, lambda **kw: seen.append(os.environ.get("HF_HUB_OFFLINE")) or "r")
        LayaModel().load()
        assert seen == ["0"]
        assert os.environ["HF_HUB_OFFLINE"] == "0"


class TestLoadingAirGapped:
    def test_an_empty_cache_is_refused_naming_the_cache_and_the_hint(
        self, hub_cache, monkeypatch
    ) -> None:
        monkeypatch.setenv("IGNI_AIR_GAPPED", "1")
        built: list[int] = []
        fake_laya(monkeypatch, lambda **kw: built.append(1))
        with pytest.raises(DecisionsUnavailable) as caught:
            LayaModel().load()
        assert built == []
        assert str(caught.value).endswith(PRESEED_HINT.format(cache=hub_cache))
        assert f"it is not in {hub_cache}" in str(caught.value)

    def test_the_hub_is_switched_offline_for_the_life_of_the_process(
        self, hub_cache, monkeypatch
    ) -> None:
        seed(hub_cache)
        monkeypatch.setenv("IGNI_AIR_GAPPED", "1")
        fake_laya(monkeypatch, lambda **kw: "r")
        assert LayaModel().load() == "r"
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert hub.HF_HUB_OFFLINE is True

    def test_an_incomplete_cache_is_unavailable_and_never_retried_online(
        self, hub_cache, monkeypatch
    ) -> None:
        seed(hub_cache)
        monkeypatch.setenv("IGNI_AIR_GAPPED", "1")
        offline: list[Any] = []

        def router(**kw: Any) -> None:
            offline.append(os.environ.get("HF_HUB_OFFLINE"))
            raise OSError("subfolder missing")

        fake_laya(monkeypatch, router)
        with pytest.raises(DecisionsUnavailable, match=r"incomplete \(OSError\)"):
            LayaModel().load()
        assert offline == ["1"]


class TestLoadingKeepsStdoutClean:
    def test_writes_to_fd_one_during_the_build_never_reach_it_and_all_is_restored(
        self, hub_cache, monkeypatch, tmp_path
    ) -> None:
        # Run in a child so fd 1 can be a file we read back.
        out = tmp_path / "out.txt"
        script = (
            "import os, sys, types\n"
            "from huggingface_hub import constants as hub\n"
            f"hub.HF_HUB_CACHE = {str(hub_cache)!r}\n"
            "os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '0'\n"
            "bars = []\n"
            "def Router(**kw):\n"
            "    os.write(1, b'chatter-fd\\n'); print('chatter-py')\n"
            "    bars.append(os.environ['HF_HUB_DISABLE_PROGRESS_BARS'])\n"
            "    return 'r'\n"
            "m = types.ModuleType('laya'); m.Router = Router; sys.modules['laya'] = m\n"
            "from igni_core.decisions_server.model import LayaModel\n"
            "LayaModel().load()\n"
            "os.write(1, b'after\\n')\n"
            "print(bars, os.environ['HF_HUB_DISABLE_PROGRESS_BARS'], flush=True)\n"
        )
        env = {k: v for k, v in os.environ.items() if k not in ("IGNI_AIR_GAPPED",)}
        with out.open("wb") as fh:
            subprocess.run(
                [sys.executable, "-c", script], stdout=fh, env=env, timeout=60, check=True
            )
        assert out.read_text() == "after\n['1'] 0\n"

    def test_the_progress_bar_variable_is_removed_when_it_was_not_set_and_the_build_raises(
        self, hub_cache, monkeypatch
    ) -> None:
        def boom() -> None:
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            LayaModel._quietly(boom)  # noqa: SLF001
        assert "HF_HUB_DISABLE_PROGRESS_BARS" not in os.environ
        with pytest.raises(RuntimeError):
            LayaModel._quietly(boom)  # noqa: SLF001
        # fd 1 still works after the failed build
        assert os.fstat(1)


class TestTheReadyFile:
    def test_it_holds_the_port_whole_and_replaces_an_earlier_one(self, tmp_path) -> None:
        path = tmp_path / "port"
        path.write_text("old")
        ReadyFile(path).write(8123)
        assert path.read_text() == "8123"
        assert list(tmp_path.iterdir()) == [path]  # the partial file is gone

    def test_a_missing_directory_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            ReadyFile(tmp_path / "missing" / "port").write(1)


class TestTheServerObject:
    def test_stop_asks_uvicorn_to_exit(self) -> None:
        server = DecisionsServer(DecisionsApp(StubModel(), "s"), idle_seconds=0)
        assert not server._server.should_exit  # noqa: SLF001
        server.stop()
        assert server._server.should_exit  # noqa: SLF001

    async def test_with_no_idle_limit_no_watcher_runs_and_an_idle_service_stays_up(self) -> None:
        app = DecisionsApp(StubModel(), "s")
        app.activity.last = time.monotonic() - 10_000
        server = DecisionsServer(app, idle_seconds=0)
        server.IDLE_POLL_S = 0.05
        sock = socket.create_server(("127.0.0.1", 0))
        import asyncio

        task = asyncio.create_task(server._serve(sock))  # noqa: SLF001
        await asyncio.sleep(0.4)
        assert not task.done()
        server.stop()
        await asyncio.wait_for(task, 5)
        sock.close()


def start(tmp_path, *args: str, secret: str | None = "s3cret") -> subprocess.Popen[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "IGNI_DECISIONS_SECRET"}
    if secret is not None:
        env["IGNI_DECISIONS_SECRET"] = secret
    return subprocess.Popen(
        [sys.executable, "-m", "igni_core.decisions_server", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=ROOT,
    )


def wait_for_port(ready: Path, process: subprocess.Popen[bytes]) -> int:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if ready.exists():
            return int(ready.read_text())
        assert process.poll() is None, process.stderr.read().decode() if process.stderr else ""
        time.sleep(0.1)
    raise AssertionError("the service never wrote its ready file")


class TestTheProcess:
    def test_an_empty_secret_is_refused_like_a_missing_one(self, tmp_path) -> None:
        process = start(tmp_path, secret="")
        _, err = process.communicate(timeout=60)
        assert process.returncode != 0
        assert b"IGNI_DECISIONS_SECRET must be set" in err

    def test_it_serves_on_the_asked_host_and_port_and_stops_when_stdin_closes(
        self, tmp_path
    ) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ready = tmp_path / "ready"
        process = start(
            tmp_path, "--host", "127.0.0.1", "--port", str(port), "--ready-file", str(ready),
            "--watch-stdin",
        )  # fmt: skip
        try:
            assert wait_for_port(ready, process) == port
            health = httpx.get(f"http://127.0.0.1:{port}/v1/health", headers=AUTH, timeout=10)
            assert health.json() == {"ok": True, "loaded": False}
            assert httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=10).status_code == 401
            assert process.stdin is not None
            process.stdin.write(b"data the service drains and discards")
            process.stdin.close()
            assert process.wait(30) == 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(10)

    def test_port_zero_reports_the_port_it_bound_and_never_idles_out_at_zero(
        self, tmp_path
    ) -> None:
        ready = tmp_path / "ready"
        process = start(tmp_path, "--ready-file", str(ready), "--idle-seconds", "0")
        try:
            port = wait_for_port(ready, process)
            assert port > 0
            assert json.loads(
                httpx.get(f"http://127.0.0.1:{port}/v1/health", headers=AUTH, timeout=10).text
            )["ok"]
            assert process.poll() is None
        finally:
            process.kill()
            process.wait(10)

    def test_a_port_already_in_use_stops_it_with_an_error_and_no_ready_file(self, tmp_path) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            ready = tmp_path / "ready"
            process = start(
                tmp_path, "--port", str(taken.getsockname()[1]), "--ready-file", str(ready)
            )
            _, err = process.communicate(timeout=60)
        assert process.returncode != 0
        assert b"Address already in use" in err or b"OSError" in err
        assert not ready.exists()


class TestTheNameIsIgni:
    def test_nothing_the_service_says_or_names_carries_the_old_product_name(self) -> None:
        package = ROOT / "src" / "igni_core" / "decisions_server"
        for path in package.glob("*.py"):
            assert "ember" not in path.read_text().lower(), path.name
        assert "ember" not in PRESEED_HINT.lower()
        assert "igni_core.prefetch_models" in PRESEED_HINT
