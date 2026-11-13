"""Finding igni's decisions service: a node's, or one started as a child.

The child is a stand-in program that does what the real service does at
start (writes its port to the ready file and stays up), so these pin
the endpoint's own life: the environment's choice, the start, the
restart, and each way a start fails.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from igni_core.core.decisions.endpoints import (
    DecisionsEndpoints,
    LocalDecisions,
    RemoteDecisions,
)
from igni_core.core.decisions.schemas import DecisionsUnavailable

SERVICE = """#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--ready-file" ]; then ready="$2"; fi
  shift
done
echo "${PORT:-4242}" > "$ready"
exec sleep 60
"""


def program(tmp_path: Path, body: str) -> str:
    path = tmp_path / "python"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def local(monkeypatch, tmp_path):
    """A LocalDecisions whose interpreter is the stand-in service."""
    monkeypatch.setattr(sys, "executable", program(tmp_path, SERVICE))
    local = LocalDecisions()
    try:
        yield local
    finally:
        local.lost()
        if local._home is not None:  # noqa: SLF001
            import shutil

            shutil.rmtree(local._home, ignore_errors=True)  # noqa: SLF001


class TestWhichEndpointTheEnvironmentChooses:
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_url_is_no_url(self, monkeypatch, blank: str) -> None:
        monkeypatch.setenv("IGNI_DECISIONS_URL", blank)
        assert isinstance(DecisionsEndpoints.configured(), LocalDecisions)

    def test_a_url_is_the_node_s_service_with_its_slashes_dropped(self, monkeypatch) -> None:
        monkeypatch.setenv("IGNI_DECISIONS_URL", "  http://decisions.node:8730//  ")
        monkeypatch.setenv("IGNI_DECISIONS_TOKEN", "t")
        endpoint = DecisionsEndpoints.configured()
        assert isinstance(endpoint, RemoteDecisions)
        assert endpoint.connection().base_url == "http://decisions.node:8730"

    def test_without_a_token_the_bearer_is_empty(self, monkeypatch) -> None:
        monkeypatch.setenv("IGNI_DECISIONS_URL", "http://d")
        monkeypatch.delenv("IGNI_DECISIONS_TOKEN", raising=False)
        assert DecisionsEndpoints.configured().connection().token == ""

    def test_a_node_s_service_is_never_lost_and_is_the_same_connection(self, monkeypatch) -> None:
        monkeypatch.setenv("IGNI_DECISIONS_URL", "http://d")
        endpoint = DecisionsEndpoints.configured()
        before = endpoint.connection()
        endpoint.lost()
        assert endpoint.connection() is before
        assert before.transport is None


class TestTheLocalServiceStart:
    def test_it_starts_on_first_use_on_loopback_with_a_per_run_secret(self, local) -> None:
        connection = local.connection()
        assert connection.base_url == "http://127.0.0.1:4242"
        assert len(connection.token) >= 32
        assert LocalDecisions().connection  # a second endpoint has its own secret
        assert LocalDecisions()._secret != local._secret  # noqa: SLF001

    def test_the_child_gets_the_secret_and_the_idle_limit(
        self, local, tmp_path, monkeypatch
    ) -> None:
        record = tmp_path / "record"
        monkeypatch.setattr(
            sys,
            "executable",
            program(
                tmp_path,
                f'#!/bin/sh\necho "$IGNI_DECISIONS_SECRET $@" > {record}\n'
                + SERVICE.split("\n", 1)[1],
            ),
        )
        connection = local.connection()
        deadline_args = record.read_text().split()
        assert deadline_args[0] == connection.token
        assert "--idle-seconds" in deadline_args
        assert deadline_args[deadline_args.index("--idle-seconds") + 1] == "900"
        assert deadline_args[deadline_args.index("--port") + 1] == "0"
        assert "--watch-stdin" in deadline_args

    def test_a_live_service_is_reused(self, local) -> None:
        first = local.connection()
        process = local._process  # noqa: SLF001
        assert local.connection().base_url == first.base_url
        assert local._process is process  # noqa: SLF001

    def test_a_service_that_has_exited_is_started_again(self, local) -> None:
        local.connection()
        first: Any = local._process  # noqa: SLF001
        first.kill()
        first.wait(10)
        local.connection()
        assert local._process is not first  # noqa: SLF001
        assert local._process.poll() is None  # noqa: SLF001

    def test_lost_ends_the_child_so_the_next_call_starts_a_new_one(self, local) -> None:
        local.connection()
        first: Any = local._process  # noqa: SLF001
        local.lost()
        assert first.wait(10) is not None
        local.connection()
        assert local._process is not first  # noqa: SLF001

    def test_lost_with_nothing_started_or_already_gone_does_nothing(self, local) -> None:
        local.lost()
        local.connection()
        process: Any = local._process  # noqa: SLF001
        process.kill()
        process.wait(10)
        local.lost()
        assert local._process is None  # noqa: SLF001

    def test_a_restart_removes_the_last_service_s_port_file_and_log(self, local) -> None:
        local.connection()
        old_home = local._home  # noqa: SLF001
        assert old_home is not None
        assert (old_home / "port").exists()
        local.lost()
        local.connection()
        assert local._home != old_home  # noqa: SLF001
        assert not old_home.exists()

    def test_calls_from_several_threads_start_one_child(self, local, tmp_path, monkeypatch) -> None:
        count = tmp_path / "starts"
        monkeypatch.setattr(
            sys,
            "executable",
            program(
                tmp_path, f"#!/bin/sh\necho x >> {count}\nsleep 0.2\n" + SERVICE.split("\n", 1)[1]
            ),
        )
        urls: list[str] = []
        threads = [
            threading.Thread(target=lambda: urls.append(local.connection().base_url))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert urls == ["http://127.0.0.1:4242"] * 6
        assert count.read_text().split() == ["x"]


class TestAServiceThatWillNotStart:
    def test_an_interpreter_that_cannot_be_run_is_unavailable(
        self, local, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(sys, "executable", str(tmp_path / "missing"))
        with pytest.raises(DecisionsUnavailable, match="would not start"):
            local.connection()
        assert local._process is None  # noqa: SLF001

    def test_a_child_that_exits_as_it_starts_points_at_its_log_and_the_extra(
        self, local, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(
            sys,
            "executable",
            program(tmp_path, "#!/bin/sh\necho 'No module named torch'\nexit 3\n"),
        )
        with pytest.raises(DecisionsUnavailable) as raised:
            local.connection()
        message = str(raised.value)
        assert "exited as it started" in message
        assert "igni-core[decisions]" in message
        log = Path(message.split("see ")[1].split(" (")[0])
        assert log.name == "service.log"
        assert "No module named torch" in log.read_text()

    def test_a_child_that_never_listens_is_killed_and_unavailable(
        self, local, monkeypatch, tmp_path
    ) -> None:
        pidfile = tmp_path / "pid"
        monkeypatch.setattr(
            sys, "executable", program(tmp_path, f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 60\n")
        )
        monkeypatch.setattr(LocalDecisions, "START_S", 0.3)
        with pytest.raises(DecisionsUnavailable, match="did not start in time"):
            local.connection()
        pid = int(pidfile.read_text())
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
        assert state == "", f"the killed child is still in the process table ({state})"

    def test_a_failed_start_is_tried_again_by_the_next_call(
        self, local, monkeypatch, tmp_path
    ) -> None:
        good = sys.executable
        monkeypatch.setattr(sys, "executable", str(tmp_path / "missing"))
        with pytest.raises(DecisionsUnavailable):
            local.connection()
        monkeypatch.setattr(sys, "executable", good)
        assert local.connection().base_url == "http://127.0.0.1:4242"
