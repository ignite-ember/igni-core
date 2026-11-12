"""``scripts/measure_resume.py``: what it measures, how it summarises, what it writes.

The tool needs Linux, root and a kernel with soft-dirty bits to run for
real. These tests drive what is its own: the fake model's stream, the
``/proc`` readers (against a synthetic ``/proc`` tree, and on Linux the
live test process), the page comparisons, the managed settings, the
summaries and the command line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from click.testing import CliRunner

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "measure_resume.py"
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("measure_resume_main", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def proc(mod, monkeypatch, tmp_path):
    """A synthetic /proc: the script's ``Path("/proc/...")`` reads land under ``tmp_path``."""
    real = Path

    def redirect(*parts):
        path = real(*parts)
        text = str(path)
        if text == "/proc" or text.startswith("/proc/"):
            return real(str(tmp_path / "proc") + text[len("/proc") :])
        return path

    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(mod, "Path", redirect)
    monkeypatch.setattr(
        mod,
        "open",
        lambda name, *a, **k: open(redirect(name), *a, **k),  # noqa: SIM115 - handed back to the caller
        raising=False,
    )

    def put(pid, name, text):
        d = tmp_path / "proc" / str(pid)
        d.mkdir(exist_ok=True)
        (d / name).write_text(text)

    return put


def _stat(comm, ppid, minflt, utime, stime):
    # pid (comm) state ppid ... fields after ")" counted from state at 0.
    fields = ["S", str(ppid)] + ["0"] * 5 + [str(minflt)] + ["0"] * 3 + [str(utime), str(stime)]
    return f"1 ({comm}) " + " ".join(fields) + " 0 0\n"


# --- the fake model ---------------------------------------------------------


def _serve(mod, reply_kb):
    mod.FakeModel.REPLY_KB = reply_kb
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.FakeModel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
        data=b"",
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - a literal http URL
            return response.headers["content-type"], response.read().decode()
    finally:
        mod.FakeModel.REPLY_KB = 0
        server.shutdown()


def _events(body):
    lines = [x[len("data: ") :] for x in body.split("\n\n") if x]
    assert lines[-1] == "[DONE]"
    return [json.loads(x) for x in lines[:-1]]


def test_the_fake_model_streams_hi_then_stop_then_done(mod):
    content_type, body = _serve(mod, 0)
    events = _events(body)
    assert content_type == "text/event-stream"
    assert [e["choices"][0]["delta"].get("content") for e in events] == ["hi", None]
    assert [e["choices"][0]["finish_reason"] for e in events] == [None, "stop"]


def test_the_fake_model_sends_one_kilobyte_chunk_per_reply_kb(mod):
    _, body = _serve(mod, 3)
    events = _events(body)
    chunks = [e["choices"][0]["delta"]["content"] for e in events[:-1]]
    assert len(chunks) == 3 and all(len(c) == 1000 for c in chunks)
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


# --- /proc readers -----------------------------------------------------------


def test_faults_and_cpu_survive_a_command_name_with_parentheses_and_spaces(mod, proc):
    proc(7, "stat", _stat("my (odd) name", 1, 42, 300, 200))
    pages = mod.ProcessPages(7)
    assert pages.faults() == 42
    assert pages.cpu_seconds() == 500 / os.sysconf("SC_CLK_TCK")


def test_rss_and_referenced_come_from_smaps_rollup_and_are_zero_when_absent(mod, proc):
    proc(7, "smaps_rollup", "Rss:   1200 kB\nReferenced:   300 kB\n")
    proc(8, "smaps_rollup", "Pss:   5 kB\n")
    assert (mod.ProcessPages(7).rss_kb(), mod.ProcessPages(7).referenced_kb()) == (1200, 300)
    assert (mod.ProcessPages(8).rss_kb(), mod.ProcessPages(8).referenced_kb()) == (0, 0)


def test_referenced_is_summed_per_mapping_label_and_cut_to_the_top(mod, proc):
    head = "{:x}-{:x} rw-p 00000000 00:00 0 {}"
    proc(
        7,
        "smaps",
        "\n".join(
            [
                head.format(0x1000, 0x2000, ""),
                "Referenced:     10 kB",
                head.format(0x3000, 0x4000, "[heap]"),
                "Referenced:     50 kB",
                head.format(0x5000, 0x6000, ""),
                "Referenced:     15 kB",
                head.format(0x7000, 0x8000, "/venv/lib/site-packages/numpy/core/x.so"),
                "Referenced:      5 kB",
            ]
        ),
    )
    pages = mod.ProcessPages(7)
    assert pages.referenced_by_mapping() == {
        "[heap]": 50,
        "[anon]": 25,
        "site-packages/numpy": 5,
    }
    assert pages.referenced_by_mapping(top=1) == {"[heap]": 50}


def test_labels_collapse_anon_keep_brackets_and_name_the_package(mod):
    label = mod.ProcessPages._label
    assert label("") == "[anon]"
    assert label("   ") == "[anon]"
    assert label("[stack]") == "[stack]"
    assert label("/a/site-packages/pydantic_core/_x.so") == "site-packages/pydantic_core"
    assert label("/usr/lib/libc.so.6") == "/usr/lib/libc.so.6"
    assert label("/tmp/café/ü.so") == "/tmp/café/ü.so"


def test_process_tree_lists_descendants_only_and_a_vanished_pid_has_no_rss(mod, proc):
    proc(10, "stat", _stat("igni", 1, 0, 0, 0))
    proc(11, "stat", _stat("child (x)", 10, 0, 0, 0))
    proc(12, "stat", _stat("grandchild", 11, 0, 0, 0))
    proc(13, "stat", _stat("stranger", 1, 0, 0, 0))
    proc(10, "status", "Name:\tigni\nVmRSS:\t   100 kB\n")
    proc(11, "status", "VmRSS:\t 20 kB\n")
    proc(12, "status", "VmRSS:\t 3 kB\n")
    proc(13, "status", "VmRSS:\t 9999 kB\n")
    tree = mod.ProcessTree(10)
    assert sorted(tree.pids()) == [10, 11, 12]
    assert tree.sample() == (100, 123)
    assert mod.ProcessTree.rss_kb(999) == 0


@linux_only
def test_the_live_process_has_pages_that_change_when_it_writes(mod):
    pages = mod.ProcessPages(os.getpid())
    before = pages.contents()
    assert before and all(isinstance(v[1], int) for v in before.values())
    assert pages.rss_kb() > 0
    keep = bytearray(os.urandom(1 << 20))
    after = pages.contents()
    total, by_mapping = mod.ProcessPages.changed(before, after)
    assert total >= 200 and by_mapping
    assert keep


# --- page comparison ---------------------------------------------------------


def test_compare_separates_base_pages_written_from_new_pages(mod):
    kb = mod.ProcessPages.PAGE // 1024
    before = {0x1000: ("[heap]", 1), 0x2000: ("[heap]", 2), 0x3000: ("libc", 3)}
    after = {
        0x1000: ("[heap]", 1),  # unchanged
        0x2000: ("[heap]", 99),  # a base page written
        0x3000: ("libc", 3),
        0x4000: ("[anon]", 5),  # new
        0x5000: ("[anon]", 6),  # new
    }
    got = mod.ProcessPages.compare(before, after)
    assert got == {
        "changed_kb": 3 * kb,
        "base_pages_written_kb": 1 * kb,
        "new_pages_kb": 2 * kb,
        "base_pages_written_by_mapping": {"[heap]": kb},
        "new_pages_by_mapping": {"[anon]": 2 * kb},
    }


def test_compare_of_identical_pages_reports_nothing_and_ignores_vanished_pages(mod):
    pages = {0x1000: ("[heap]", 1)}
    zero = mod.ProcessPages.compare(pages, pages)
    assert zero["changed_kb"] == 0 and zero["new_pages_by_mapping"] == {}
    assert mod.ProcessPages.compare(pages, {})["changed_kb"] == 0


def test_compare_keeps_only_the_top_mappings(mod):
    after = {i * 0x1000: (f"m{i % 3}", 1) for i in range(1, 10)}
    got = mod.ProcessPages.compare({}, after, top=2)
    assert len(got["new_pages_by_mapping"]) == 2
    assert got["new_pages_kb"] == 9 * (mod.ProcessPages.PAGE // 1024)


def test_changed_counts_new_or_differing_pages_in_kb_by_mapping(mod):
    kb = mod.ProcessPages.PAGE // 1024
    before = {1: ("a", 1), 2: ("a", 2)}
    after = {1: ("a", 1), 2: ("a", 3), 3: ("b", 4)}
    total, by_mapping = mod.ProcessPages.changed(before, after)
    assert total == 2
    assert by_mapping == {"a": kb, "b": kb}


def test_save_writes_address_checksum_and_label_in_hex(mod, tmp_path):
    path = tmp_path / "base.pages"
    mod.ProcessPages.save({0x7F000: ("[heap]", 0xBEEF), 0x80000: ("[anon]", 1)}, path)
    assert path.read_text() == "7f000 beef [heap]\n80000 1 [anon]\n"


def test_soft_dirty_counts_present_dirty_pages_and_skips_vsyscall(mod, proc, tmp_path):
    page = mod.ProcessPages.PAGE
    present, dirty = 1 << 63, 1 << 55
    entries = [present | dirty, present, dirty, present | dirty]  # pages 0..3
    # Page index 0 is address 0: lay the maps out at address 0 and 4 pages.
    proc(
        7,
        "maps",
        f"{0:x}-{4 * page:x} rw-p 00000000 00:00 0 [heap]\n"
        f"{0:x}-{4 * page:x} r-xp 00000000 00:00 0 [vsyscall]\n",
    )
    (tmp_path / "proc" / "7" / "pagemap").write_bytes(struct.pack("<4Q", *entries))
    total, by_mapping = mod.ProcessPages(7).soft_dirty()
    assert total == 2
    assert by_mapping == {"[heap]": 2 * page // 1024}


def test_contents_checksums_only_present_pages_of_writable_private_mappings(mod, proc, tmp_path):
    page = mod.ProcessPages.PAGE
    memory = b"A" * page + b"B" * page
    proc(
        7,
        "maps",
        f"{0:x}-{2 * page:x} rw-p 00000000 00:00 0 [heap]\n"
        f"{2 * page:x}-{4 * page:x} r--p 00000000 00:00 0 /lib/ro.so\n"
        f"{4 * page:x}-{6 * page:x} rw-s 00000000 00:00 0 /dev/shm/x\n"
        f"{6 * page:x}-{8 * page:x} rw-p 00000000 00:00 0 [vvar]\n",
    )
    entries = [1 << 63, 0] + [1 << 63] * 6  # the heap's second page is not present
    (tmp_path / "proc" / "7" / "pagemap").write_bytes(struct.pack("<8Q", *entries))
    (tmp_path / "proc" / "7" / "mem").write_bytes(memory)
    import zlib

    got = mod.ProcessPages(7).contents()
    assert got == {0: ("[heap]", zlib.crc32(b"A" * page))}


# --- fault trace -------------------------------------------------------------


def test_classify_buckets_touched_pages_into_base_own_and_file_or_shared(mod):
    base = {0x1000: ("h", 1), 0x2000: ("h", 2)}
    now = {0x1000: ("h", 1), 0x2000: ("h", 22), 0x3000: ("h", 3)}
    touched = {0x1000, 0x2000, 0x3000, 0x9000}
    assert mod.FaultTrace.classify(touched, base, now) == {
        "base": 1,
        "own": 2,  # a changed base page and a page the base never had
        "file_or_shared": 1,
    }
    assert mod.FaultTrace.classify(set(), base, now) == {}


def test_faulted_pages_are_the_page_aligned_addresses_in_the_trace(mod, tmp_path, monkeypatch):
    page = mod.ProcessPages.PAGE
    (tmp_path / "trace").write_text(
        f"igni-1 [000] .... 1.0: igni_probe_fault: (handle_mm_fault+0x4/0x1) addr=0x{page * 5 + 17:x}\n"
        f"igni-1 [000] .... 1.1: igni_probe_fault: (handle_mm_fault+0x4/0x1) addr=0x{page * 5 + 99:x}\n"
        f"igni-1 [000] .... 1.2: igni_probe_fault: (handle_mm_fault+0x4/0x1) addr=0x{page * 9:x}\n"
        "unrelated line\n"
    )
    monkeypatch.setattr(mod.FaultTrace, "TRACING", tmp_path)
    assert mod.FaultTrace(1).pages() == {page * 5, page * 9}


def test_every_supported_architecture_names_the_address_register(mod):
    assert set(mod.FaultTrace.REGISTER) == {"aarch64", "arm64", "x86_64"}


# --- the model and its settings ---------------------------------------------


def test_a_real_model_is_named_by_url_and_id_and_its_key_is_never_inlined(mod):
    choice = mod.ModelChoice(
        fake_url="http://fake/v1",
        url="https://llm.example/v1",
        model_id="big",
        key_env="PROBE_KEY",
        context_window=1000,
    )
    settings = choice.managed_settings()
    entry = settings["models"]["registry"]["probe"]
    assert settings["models"]["default"] == "probe"
    assert entry == {
        "provider": "openai_like",
        "model_id": "big",
        "url": "https://llm.example/v1",
        "api_key_env": "PROBE_KEY",
        "context_window": 1000,
    }
    assert "api_key" not in entry


def test_a_real_model_without_a_key_variable_has_none(mod):
    choice = mod.ModelChoice(fake_url="f", url="u", model_id="m")
    assert choice.managed_settings()["models"]["registry"]["probe"]["api_key_env"] is None


@pytest.mark.parametrize("kwargs", [{}, {"url": "u"}, {"model_id": "m"}])
def test_without_both_url_and_id_the_fake_model_is_used(mod, kwargs):
    choice = mod.ModelChoice(fake_url="http://fake/v1", **kwargs)
    assert not choice.real
    entry = choice.managed_settings()["models"]["registry"]["probe"]
    assert entry["model_id"] == "fake" and entry["url"] == "http://fake/v1"


def test_the_run_may_edit_and_run_commands_but_not_push(mod):
    permissions = mod.ModelChoice(fake_url="f").managed_settings()["permissions"]
    assert permissions == {
        "file_write": "allow",
        "shell_execute": "allow",
        "git_push": "deny",
        "git_destructive": "deny",
    }


# --- the client --------------------------------------------------------------


class _Socket:
    def __init__(self, replies):
        self.sent = []
        self._replies = [json.dumps(r) for r in replies]

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return self._replies.pop(0)


async def test_rpc_skips_other_messages_and_returns_the_reply_with_its_id(mod):
    socket = _Socket(
        [
            {"type": "welcome"},
            {"type": "rpc_response", "id": "someone-else"},
            {"type": "rpc_response", "id": "probe-1", "result": {"ok": 1}},
        ]
    )
    reply = await mod.Window(socket).rpc("get_status", {"a": 1})
    assert reply["result"] == {"ok": 1}
    assert socket.sent == [
        {"type": "rpc_request", "id": "probe-1", "method": "get_status", "args": {"a": 1}}
    ]


async def test_each_rpc_has_its_own_id_and_no_args_are_an_empty_object(mod):
    socket = _Socket([{"id": "probe-1"}, {"id": "probe-2"}])
    window = mod.Window(socket)
    await window.rpc("a")
    await window.rpc("b")
    assert [m["id"] for m in socket.sent] == ["probe-1", "probe-2"]
    assert socket.sent[0]["args"] == {}


async def test_a_turn_ends_on_stream_end_or_run_error(mod):
    done = _Socket([{"type": "text"}, {"type": "stream_end"}])
    assert await mod.Window(done).turn("hello") == "stream_end"
    assert done.sent[0]["type"] == "user_message" and done.sent[0]["text"] == "hello"
    failed = _Socket([{"type": "run_error"}])
    assert await mod.Window(failed).turn("hello") == "run_error"


# --- the probe's generation counter -----------------------------------------


def _probe(mod, restored):
    return mod.ResumeProbe(
        pid=1,
        url="ws://x",
        restored=restored,
        simulate=True,
        connect_after_ms=0,
        turns=1,
        before_suspend=False,
        clear_referenced=False,
    )


def test_the_next_generation_is_one_after_the_last_or_one_when_unreadable(mod, tmp_path):
    restored = tmp_path / "restored"
    probe = _probe(mod, restored)
    assert probe._next_generation() == 1  # absent
    restored.write_text("not json")
    assert probe._next_generation() == 1
    restored.write_text(json.dumps({"other": 1}))
    assert probe._next_generation() == 1
    restored.write_text(json.dumps({"generation": 4}))
    assert probe._next_generation() == 5


# --- summaries ---------------------------------------------------------------


def _resume(n, **extra):
    base = {
        "handshake_ms": 10 * n,
        "first_rpc_ms": 20 * n,
        "first_db_rpc_ms": 30 * n,
        "faults_to_handshake": n,
        "faults_to_first_rpc": 2 * n,
        "faults_first_rpc_to_db": 3 * n,
        "referenced_kb": 100 * n,
        "cpu_ms": 5 * n,
    }
    return base | extra


def test_the_summary_gives_p50_and_max_per_metric(mod):
    got = mod.Summary.of([_resume(1), _resume(5), _resume(3)])
    assert got["n"] == 3
    assert got["handshake_ms"] == {"p50": 30, "max": 50}
    assert got["referenced_kb"] == {"p50": 300, "max": 500}
    assert "touched_to_welcome" not in got


def test_the_summary_takes_the_median_of_each_touched_kind(mod):
    got = mod.Summary.of(
        [
            _resume(1, touched_to_welcome={"base": 10, "own": 2}),
            _resume(2, touched_to_welcome={"base": 30}),
            _resume(3, touched_to_welcome={"base": 20, "own": 4}),
        ]
    )
    assert got["touched_to_welcome"] == {"base": 20, "own": 2}


def test_steps_gives_per_step_p50_and_names_a_repeated_step_by_number(mod):
    first = {
        "steps": [
            {"step": "load", "at_ms": 10, "minor_faults": 5, "cpu_ms": 1},
            {"step": "rpc", "at_ms": 20, "minor_faults": 6, "cpu_ms": 2},
            {"step": "rpc", "at_ms": 30, "minor_faults": 7, "cpu_ms": 3},
        ]
    }
    second = {
        "steps": [
            {"step": "load", "at_ms": 30, "minor_faults": 9, "cpu_ms": 3},
            {"step": "rpc", "at_ms": 40, "minor_faults": 8, "cpu_ms": 4},
        ]
    }
    got = mod.Summary.steps([first, second])
    assert got["load"] == {"at_ms": 20, "minor_faults": 7, "cpu_ms": 2}
    assert got["rpc"]["at_ms"] == 30
    assert got["rpc #2"] == {"at_ms": 30, "minor_faults": 7, "cpu_ms": 3}


def test_steps_leave_out_keys_that_are_missing_or_none_and_handle_no_timelines(mod):
    got = mod.Summary.steps([{"steps": [{"step": "load", "at_ms": 10, "minor_faults": None}]}, {}])
    assert got == {"load": {"at_ms": 10}}
    assert mod.Summary.steps([]) == {}


def test_timelines_are_read_from_resume_timeline_lines_and_the_rest_skipped(mod, tmp_path):
    log = tmp_path / "igni.log"
    log.write_text(
        "\n".join(
            [
                "starting up",
                json.dumps({"msg": "resume timeline", "timeline": {"steps": [1]}}),
                "resume timeline {not json",
                json.dumps({"msg": "resume timeline", "timeline": "not a dict"}),
                json.dumps({"msg": "other", "timeline": {"steps": [2]}}),
                json.dumps({"msg": "resume timeline", "timeline": {"steps": [3]}}),
                "",
            ]
        )
    )
    assert mod.Summary.timelines(log) == [{"steps": [1]}, {"steps": [3]}]
    log.write_bytes(b"resume timeline \xff\xfe\n")
    assert mod.Summary.timelines(log) == []


# --- the command line --------------------------------------------------------


def test_out_is_required(mod):
    result = CliRunner().invoke(mod.main, [])
    assert result.exit_code == 2 and "--out" in result.output


def test_a_pid_needs_a_websocket_url_and_a_restored_file(mod, tmp_path):
    result = CliRunner().invoke(mod.main, ["--out", str(tmp_path / "r.json"), "--pid", "1"])
    assert isinstance(result.exception, AssertionError)
    assert "--ws and --restored" in str(result.exception)
    assert not (tmp_path / "r.json").exists()


def test_the_report_is_written_whole_and_the_brief_leaves_out_the_runs(mod, monkeypatch, tmp_path):
    class Stub:
        def __init__(self, **kwargs):
            self.simulate = kwargs["simulate"]
            self.trace = None

        async def session(self):
            pass

        async def idle(self, seconds):
            return {"seconds": seconds}

        async def resume(self):
            return _resume(1)

    monkeypatch.setattr(mod, "ResumeProbe", Stub)
    out = tmp_path / "r.json"
    result = CliRunner().invoke(
        mod.main,
        [
            "--out", str(out), "--pid", "1", "--ws", "ws://x", "--restored", str(tmp_path / "rs"),
            "--idle", "7", "--settle", "0", "--resumes", "1",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["idle"] == {"seconds": 7.0}
    assert report["resumes"] == [_resume(1)]
    assert report["resume_summary"]["n"] == 1
    assert "igni_timelines" not in report  # no igni of its own: no log to read
    brief = json.loads(result.output)
    assert "resumes" not in brief and "igni_timelines" not in brief
    assert brief["resume_summary"]["n"] == 1


def test_without_simulate_only_the_idle_window_is_measured(mod, monkeypatch, tmp_path):
    class Stub:
        def __init__(self, **kwargs):
            self.simulate = kwargs["simulate"]

        async def session(self):
            pass

        async def idle(self, seconds):
            return {"seconds": seconds}

    monkeypatch.setattr(mod, "ResumeProbe", Stub)
    out = tmp_path / "r.json"
    result = CliRunner().invoke(
        mod.main,
        [
            "--out", str(out), "--pid", "1", "--ws", "ws://x", "--restored", str(tmp_path / "rs"),
            "--settle", "0", "--no-simulate",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert set(json.loads(out.read_text())) == {"idle"}
