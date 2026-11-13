"""The Seatbelt profile's text, its command, its probe and its port listing.

``test_an_extension_process_is_confined_to_its_grants.py`` runs a real
process under the profile, which proves its meaning on macOS only. This
file pins, on every host, what the profile says for a given plan, what
``command`` writes and returns, why ``probe`` says the sandbox cannot be
used, and how the listening ports are read from ``lsof``. The two
programs are stood in for by small scripts the tests write, run for real.
"""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

import pytest

from igni_core.extensions.sandbox import seatbelt as seatbelt_module
from igni_core.extensions.sandbox.plan import SandboxPlan
from igni_core.extensions.sandbox.seatbelt import HOME_ROOTS, SANDBOX_EXEC, Seatbelt, _quote

pytestmark = [
    pytest.mark.extension_sandbox,
    pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins are shell scripts"),
]


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def lines_of(plan: SandboxPlan) -> list[str]:
    return Seatbelt.profile(plan).splitlines()


# ── The profile ───────────────────────────────────────────────────────


def test_a_profile_denies_by_default_and_names_the_extension():
    lines = lines_of(SandboxPlan(extension_id="acme.probe"))
    assert lines[0] == "(version 1)"
    assert lines[1] == ";; igni extension sandbox: acme.probe"
    assert lines[2] == "(deny default)"


def test_an_extension_id_cannot_end_its_comment_and_add_rules():
    lines = lines_of(SandboxPlan(extension_id="evil\n(allow default)\r(allow network*)"))
    assert "(allow default)" not in lines
    assert "(allow network*)" not in lines
    assert lines[1].startswith(";; igni extension sandbox: evil")


def test_launchers_and_apple_events_are_refused_outright():
    profile = Seatbelt.profile(SandboxPlan(extension_id="a.b"))
    for tool in ("open", "osascript", "osacompile", "lsappinfo"):
        assert f'(literal "/usr/bin/{tool}")' in profile
    assert "(deny process-exec " in profile
    assert "(deny appleevent-send)" in profile


def test_every_home_directory_is_unreadable_but_for_metadata():
    profile = Seatbelt.profile(SandboxPlan(extension_id="a.b"))
    for root in HOME_ROOTS:
        assert f'(subpath "{root}")' in profile
    assert "/Users" in HOME_ROOTS and "/private/var/folders" in HOME_ROOTS
    deny = next(line for line in profile.splitlines() if line.startswith("(deny file-read*"))
    allow = next(
        line for line in profile.splitlines() if line.startswith("(allow file-read-metadata")
    )
    assert '(subpath "/Users")' in deny and '(subpath "/Users")' in allow


def test_the_plans_read_and_write_paths_are_granted_and_empty_ones_leave_no_rule(tmp_path):
    bare = Seatbelt.profile(SandboxPlan(extension_id="a.b"))
    assert "(allow file-write* " not in bare
    assert "(allow network-bind network-outbound network-inbound" not in bare
    assert ";; igni's own loopback listeners" not in bare
    assert "(deny network-outbound" not in bare
    read, write = tmp_path / "in", tmp_path / "out"
    granted = Seatbelt.profile(SandboxPlan(extension_id="a.b", read=[read], write=[write]))
    assert (
        f'(allow file-read* file-map-executable file-test-existence (subpath "{read}"))' in granted
    )
    assert f'(allow file-write* (subpath "{write}"))' in granted
    # A write path is also where a unix socket may be used.
    assert f'(allow network-bind network-outbound network-inbound (subpath "{write}"))' in granted


def test_the_network_is_this_machines_loopback_and_igni_s_other_ports_are_denied():
    plan = SandboxPlan(extension_id="a.b", denied_ports=[8000, 9001])
    lines = lines_of(plan)
    assert '(allow network-outbound (remote ip "localhost:*"))' in lines
    assert '(allow network-bind network-inbound (local ip "localhost:*"))' in lines
    deny = '(deny network-outbound (remote ip "localhost:8000") (remote ip "localhost:9001"))'
    assert deny in lines
    # The deny comes after the loopback allow it narrows.
    assert lines.index(deny) > lines.index('(allow network-outbound (remote ip "localhost:*"))')


def test_a_path_cannot_break_out_of_the_string_it_is_written_in():
    assert _quote('/a "b" \\c') == '"/a \\"b\\" \\\\c"'
    assert _quote(Path("/plain")) == '"/plain"'
    hostile = Path('/x") (allow default) ("y')
    profile = Seatbelt.profile(SandboxPlan(extension_id="a.b", write=[hostile]))
    assert '(subpath "/x\\") (allow default) (\\"y")' in profile
    assert "\n(allow default)" not in profile


# ── The command ───────────────────────────────────────────────────────


def test_the_command_writes_the_profile_into_the_run_dir_and_wraps_the_argv(tmp_path):
    plan = SandboxPlan(extension_id="acme.probe", run_dir=tmp_path)
    program, args = Seatbelt.command(plan, "/bin/echo", ["hi", "there"])
    assert program == SANDBOX_EXEC == "/usr/bin/sandbox-exec"
    assert args == ["-f", str(tmp_path / "profile.sb"), "/bin/echo", "hi", "there"]
    assert (tmp_path / "profile.sb").read_text() == Seatbelt.profile(plan)


# ── Found, and probe ──────────────────────────────────────────────────


def test_it_is_found_only_when_the_binary_is_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(tmp_path / "missing"))
    assert not Seatbelt.found()
    (tmp_path / "dir").mkdir()
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(tmp_path / "dir"))
    assert not Seatbelt.found()
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(script(tmp_path / "se", "exit 0\n")))
    assert Seatbelt.found()


async def test_probe_says_nothing_when_a_trivial_command_can_be_confined(tmp_path, monkeypatch):
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(script(tmp_path / "se", "exit 0\n")))
    assert await Seatbelt.probe() is None


async def test_probe_names_a_missing_binary(tmp_path, monkeypatch):
    missing = str(tmp_path / "missing")
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", missing)
    assert await Seatbelt.probe() == f"{missing} is not on this machine"


async def test_probe_gives_the_binarys_own_words_when_it_refuses(tmp_path, monkeypatch):
    refusing = script(
        tmp_path / "se", "echo 'sandbox_apply: Operation not permitted' >&2\nexit 71\n"
    )
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(refusing))
    assert await Seatbelt.probe() == (f"{refusing} failed: sandbox_apply: Operation not permitted")


async def test_probe_says_when_the_binary_could_not_run_at_all(tmp_path, monkeypatch):
    unrunnable = tmp_path / "se"
    unrunnable.write_text("not a program\n")
    unrunnable.chmod(0o755)
    monkeypatch.setattr(seatbelt_module, "SANDBOX_EXEC", str(unrunnable))
    reason = await Seatbelt.probe()
    assert reason is not None and reason.startswith(f"{unrunnable} could not run: ")


# ── The ports igni listens on ─────────────────────────────────────────


def fake_lsof(tmp_path: Path, monkeypatch, body: str) -> Path:
    lsof = script(tmp_path / "lsof", body)
    real = shutil.which
    monkeypatch.setattr(
        seatbelt_module.shutil,
        "which",
        lambda name, *a, **k: str(lsof) if name == "lsof" else real(name, *a, **k),
    )
    return lsof


async def test_listening_ports_are_read_from_lsofs_field_output(tmp_path, monkeypatch):
    fake_lsof(
        tmp_path,
        monkeypatch,
        'echo "$@" > "$0.args"\n'
        "printf 'p123\\nf9\\nn127.0.0.1:8000\\nn*:8080\\nn[::1]:9001\\nn[::]:9001\\n'\n",
    )
    assert await Seatbelt.listening_ports(123) == {8000, 8080, 9001}
    # It asked for that pid's listening TCP sockets, by number.
    assert (tmp_path / "lsof.args").read_text().split() == [
        "-nP",
        "-a",
        "-p",
        "123",
        "-iTCP",
        "-sTCP:LISTEN",
        "-Fn",
    ]


async def test_listening_ports_skip_lines_that_name_no_port(tmp_path, monkeypatch):
    fake_lsof(
        tmp_path,
        monkeypatch,
        "printf 'n127.0.0.1:abc\\nnnocolon\\nfn:77\\nn::1234\\nn*:5\\n'\n",
    )
    assert await Seatbelt.listening_ports(1) == {1234, 5}


async def test_listening_ports_default_to_igni_s_own_pid(tmp_path, monkeypatch):
    import os

    fake_lsof(tmp_path, monkeypatch, 'echo "$@" > "$0.args"\n')
    assert await Seatbelt.listening_ports() == set()
    assert str(os.getpid()) in (tmp_path / "lsof.args").read_text().split()


async def test_when_lsof_cannot_run_no_port_is_denied_and_it_is_said(tmp_path, monkeypatch, caplog):
    broken = tmp_path / "lsof"
    broken.write_text("not a program\n")
    broken.chmod(0o755)
    monkeypatch.setattr(seatbelt_module.shutil, "which", lambda name, *a, **k: str(broken))
    assert await Seatbelt.listening_ports(1) == set()
    assert "lsof could not list igni's own ports; they stay reachable" in caplog.text
