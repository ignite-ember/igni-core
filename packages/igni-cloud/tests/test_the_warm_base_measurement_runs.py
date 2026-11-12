"""``scripts/measure_warm_base.py`` starts a warm base's igni, binds it and times what happens.

``docs/CLOUD.md`` §9 describes the measurement. It plays the node agent,
so it has to write a ``producer.env`` the guest accepts: when the file
lacked ``IGNI_PRODUCER_INCARNATION`` the guest kept waiting for a producer
and the script retried ``get_status`` for ever without printing anything.

The script patches process-wide classes and ends with ``os._exit``, so
the end-to-end tests run it as a subprocess; its pieces are tested
directly.
"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "measure_warm_base.py"


@pytest.fixture(scope="module")
def measure():
    spec = importlib.util.spec_from_file_location("measure_warm_base_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _run(tmp_path, *argv):
    env = {k: v for k, v in os.environ.items() if not k.startswith("IGNI_")}
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )


class TestTheMeasurementEndToEnd:
    @pytest.mark.parametrize("flag, warm", [("--no-warm", False), (None, True)])
    def test_it_binds_answers_runs_a_turn_and_reports(self, tmp_path, flag, warm):
        out = tmp_path / "result.json"

        proc = _run(tmp_path, "--out", str(out), *([flag] if flag else []))

        assert proc.returncode == 0, proc.stderr[-2000:]
        result = json.loads(out.read_text())
        assert set(result) == {
            "warm",
            "database",
            "start_to_ready_s",
            "rss_at_ready_mb",
            "bind_latency_ms",
            "bind_to_first_answer_s",
            "first_turn_s",
            "rss_after_turn_mb",
            "last_event",
            "imported_after_ready",
        }
        assert result["warm"] is warm
        assert result["database"] == "sqlite"
        assert result["last_event"] == "stream_end"
        assert result["rss_at_ready_mb"] > 0 and result["rss_after_turn_mb"] > 0
        assert result["bind_latency_ms"] >= 0
        assert result["bind_to_first_answer_s"] > 0
        assert result["first_turn_s"] > 0
        assert isinstance(result["imported_after_ready"], list)

    def test_the_summary_on_stderr_counts_the_modules_instead_of_listing_them(self, tmp_path):
        out = tmp_path / "result.json"

        proc = _run(tmp_path, "--out", str(out), "--no-warm")

        full = json.loads(out.read_text())
        summary = json.loads(proc.stderr.strip().splitlines()[-1])
        assert summary["imported_after_ready"] == len(full["imported_after_ready"])
        assert summary == {**full, "imported_after_ready": len(full["imported_after_ready"])}

    def test_a_ready_file_that_never_comes_ends_the_run_with_a_message(self, tmp_path):
        proc = _run(tmp_path, "--out", str(tmp_path / "r.json"), "--ready-timeout", "0.001")

        assert proc.returncode == 1
        assert "error: igni wrote no ready file" in proc.stderr
        assert "--ready-timeout" in proc.stderr
        assert not (tmp_path / "r.json").exists()

    def test_the_output_file_is_required(self, tmp_path):
        proc = _run(tmp_path)

        assert proc.returncode == 2
        assert "--out" in proc.stderr


class TestTheFakeModel:
    def test_every_post_is_answered_with_a_streamed_hi_and_done(self, measure):
        server = ThreadingHTTPServer(("127.0.0.1", 0), measure.FakeModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/v1/chat/completions", body=b'{"stream": true}')
            response = conn.getresponse()
            body = response.read().decode()
        finally:
            server.shutdown()
            server.server_close()

        assert response.status == 200
        assert response.getheader("content-type") == "text/event-stream"
        events = [line[6:] for line in body.split("\n\n") if line]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": "hi"}
        assert chunks[0]["choices"][0]["finish_reason"] is None
        assert chunks[1]["choices"][0]["finish_reason"] == "stop"
        assert all(c["object"] == "chat.completion.chunk" for c in chunks)


class TestTheNodeAgentsPieces:
    def test_the_token_names_the_user_and_org_and_lives_an_hour(self, measure):
        import time

        header, claims, signature = measure.WarmBaseMeasurement.token().split(".")

        def decode(part):
            return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))

        assert decode(header) == {}
        decoded = decode(claims)
        assert (decoded["sub"], decoded["org"]) == ("u-ada", "org-acme")
        assert 3500 < decoded["exp"] - time.time() <= 3600
        assert signature == "sig"

    def test_the_rss_is_this_processes_in_megabytes(self, measure):
        rss = measure.WarmBaseMeasurement.rss_mb()

        assert 5 < rss < 100_000
        assert rss == round(rss, 1)

    def test_the_first_answer_is_the_first_reply_without_an_error(self, measure):
        import asyncio

        class Window:
            """Refuses twice, interleaves a message for nobody, then answers."""

            def __init__(self):
                self.sent = []
                self.pending = []

            async def send(self, raw):
                request = json.loads(raw)
                self.sent.append(request)
                n = len(self.sent)
                if n == 2:
                    self.pending.append(json.dumps({"id": "unrelated", "error": "x"}))
                reply = {"id": request["id"]}
                if n < 3:
                    reply["error"] = "not bound yet"
                self.pending.append(json.dumps(reply))

            async def recv(self):
                return self.pending.pop(0)

        window = Window()

        asyncio.run(measure.WarmBaseMeasurement._first_answer(window))

        assert [r["id"] for r in window.sent] == ["r1", "r2", "r3"]
        assert all(r["method"] == "get_status" and r["type"] == "rpc_request" for r in window.sent)


class TestTheWaitsHaveTheirOwnTimeout:
    def test_an_igni_that_only_ever_refuses_ends_the_retry_with_a_message(self, measure):
        import asyncio

        class Refuser:
            def __init__(self):
                self.pending = []

            async def send(self, raw):
                self.pending.append(json.dumps({"id": json.loads(raw)["id"], "error": "no"}))

            async def recv(self):
                return self.pending.pop(0)

        with pytest.raises(measure.MeasurementTimeout, match="get_status within 0.2s"):
            asyncio.run(measure.WarmBaseMeasurement._first_answer(Refuser(), 0.2))

    def test_an_igni_that_never_replies_ends_the_wait_with_a_message(self, measure):
        import asyncio

        class Silent:
            async def send(self, raw):
                pass

            async def recv(self):
                await asyncio.sleep(3600)

        with pytest.raises(measure.MeasurementTimeout, match="did not reply to get_status"):
            asyncio.run(measure.WarmBaseMeasurement._first_answer(Silent(), 0.2))
