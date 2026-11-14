"""The launcher asks one interface for a sandbox and names none of them.

A platform's mechanism is replaced here by one that records what it is
asked, on a platform name no real mechanism serves, so the launcher's own
behaviour is pinned on every host: what the process's environment says,
what is left behind when something fails, and what teardown says when a
part of it will not go. The real mechanisms are pinned by their own
tests, on the hosts that have them.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import pytest

from igni_core.core.config.settings import Settings
from igni_core.core.mcp.config import MCPServerConfig, MCPTransport
from igni_core.core.network_policy import NetworkPolicy
from igni_core.extensions.manifest import ExtensionManifest
from igni_core.extensions.sandbox import (
    SANDBOX_ENV,
    ExtensionSandbox,
    MechanismName,
    MechanismRun,
    SandboxMechanism,
    SandboxRefused,
    SandboxSettings,
)
from igni_core.extensions.sandbox import launcher as sandbox_launcher
from igni_core.extensions.sandbox import mechanism as sandbox_mechanism
from igni_core.extensions.sandbox.proxy import LoopbackRelay

pytestmark = pytest.mark.extension_sandbox

PLATFORM = "testos"


def manifest(extension_id: str = "acme.probe", **permissions) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": extension_id,
            "version": "1.0.0",
            "contract": ">=1.0 <2",
            "name": "Probe",
            "trust": "unsigned",
            "dev": {"command": ["python", "probe.py"]},
            "permissions": permissions,
        }
    )


def stdio_config(**kw) -> MCPServerConfig:
    return MCPServerConfig(
        name="ext:acme.probe",
        type=MCPTransport.stdio,
        command=kw.pop("command", "/bin/echo"),
        args=kw.pop("args", ["hello"]),
        env=kw.pop("env", {}),
        cwd=kw.pop("cwd", ""),
        source="extension",
    )


class Recorder:
    """What the stand-in mechanism was asked, and how it is told to fail."""

    def __init__(self) -> None:
        self.probes = 0
        self.prepared: list[tuple[list[Path], str, list[str]]] = []
        self.released: list[MechanismRun] = []
        self.fail_prepare: Exception | None = None
        self.fail_release: Exception | None = None
        self.env: dict[str, str] = {}
        self.proxies: list = []
        self.stopped_relays: list[int] = []


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    rec = Recorder()

    class Stand(SandboxMechanism):
        name = MechanismName.BUBBLEWRAP

        @classmethod
        def serves(cls, platform: str) -> bool:
            return platform == PLATFORM

        async def probe(self) -> str | None:
            rec.probes += 1
            return None

        async def prepare(self, plan, proxy, *, command, args, cwd):
            rec.proxies.append(proxy)
            port = await proxy.start_tcp()
            rec.prepared.append((list(plan.write), command, list(args)))
            if rec.fail_prepare is not None:
                raise rec.fail_prepare
            relay = LoopbackRelay(41001, plan.run_dir / "relay.sock")

            async def stop() -> None:
                rec.stopped_relays.append(relay.port)

            relay.stop = stop
            return MechanismRun(
                command="wrapper",
                args=["--", command, *args],
                proxy_port=port,
                relays=[relay],
                env=dict(rec.env),
            )

        async def release(self, prepared: MechanismRun) -> None:
            rec.released.append(prepared)
            if rec.fail_release is not None:
                raise rec.fail_release

    monkeypatch.setattr(sandbox_mechanism, "MECHANISMS", (Stand,))
    return rec


def sandbox_for(tmp_path: Path, **kw) -> ExtensionSandbox:
    kw.setdefault("settings", SandboxSettings())
    return ExtensionSandbox(platform=PLATFORM, run_root=tmp_path / "runs", **kw)


async def confine(sandbox: ExtensionSandbox, tmp_path: Path, m=None, config=None, **kw):
    (tmp_path / "runs").mkdir(exist_ok=True)
    args = dict(
        bundle=tmp_path,
        data_dir=None,
        project_dir=None,
        service_url=None,
        development=False,
    )
    args.update(kw)
    return await sandbox.confine(m or manifest(), config or stdio_config(), **args)


async def test_a_confined_config_runs_the_wrapper_with_the_proxy_and_the_run_s_own_directories(
    recorder, tmp_path
):
    sandbox = sandbox_for(tmp_path)
    recorder.env = {"APPDATA": "from-the-platform", "HOME": "from-the-platform"}
    try:
        out = await confine(sandbox, tmp_path, config=stdio_config(env={"KEEP": "1"}))
        status = sandbox.status("acme.probe")
        assert status is not None and status.describe() == "sandboxed (bubblewrap)"
        assert status.confined and status.container is None
        plan = status.plan
        assert out.command == "wrapper" and out.args == ["--", "/bin/echo", "hello"]
        env = out.env or {}
        assert env["KEEP"] == "1"
        assert env[SANDBOX_ENV] == "bubblewrap"
        proxy = f"http://igni:{status.proxy._token}@127.0.0.1:{status.proxy.port}"
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            assert env[name] == proxy and env[name.lower()] == proxy
        assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(plan.temp_dir)
        assert env["JAVA_TOOL_OPTIONS"] == f"-Djava.io.tmpdir={plan.temp_dir}"
        # What the sandbox sets wins over what the platform says; the rest stays.
        assert env["HOME"] == str(plan.home_dir)
        assert env["APPDATA"] == "from-the-platform"
        assert plan.home_dir.is_dir()
    finally:
        await sandbox.close()


async def test_an_existing_java_option_is_kept_and_the_temp_directory_follows_it(
    recorder, tmp_path
):
    sandbox = sandbox_for(tmp_path)
    try:
        out = await confine(
            sandbox, tmp_path, config=stdio_config(env={"JAVA_TOOL_OPTIONS": "-Xmx1g"})
        )
        tmp = sandbox.status("acme.probe").plan.temp_dir
        assert (out.env or {})["JAVA_TOOL_OPTIONS"] == f"-Xmx1g -Djava.io.tmpdir={tmp}"
    finally:
        await sandbox.close()


async def test_a_config_without_env_or_args_is_confined_all_the_same(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    config = stdio_config(args=[])
    config = config.model_copy(update={"env": None, "args": None})
    try:
        out = await confine(sandbox, tmp_path, config=config)
        assert out.args == ["--", "/bin/echo"]
        assert (out.env or {})[SANDBOX_ENV] == "bubblewrap"
    finally:
        await sandbox.close()


async def test_the_run_directory_is_named_for_the_extension_cut_to_forty_characters(
    recorder, tmp_path
):
    sandbox = sandbox_for(tmp_path)
    long_id = "acme." + "x" * 60
    try:
        await confine(sandbox, tmp_path, m=manifest(long_id))
        run_dir = sandbox.status(long_id).plan.run_dir
        assert run_dir.parent == (tmp_path / "runs").resolve()
        assert run_dir.name.startswith("igni-ext-" + long_id[:40] + "-")
        assert long_id[:41] not in run_dir.name
    finally:
        await sandbox.close()


async def test_a_config_that_is_not_a_process_is_left_alone_and_recorded_nowhere(
    recorder, tmp_path
):
    sandbox = sandbox_for(tmp_path, settings=SandboxSettings(required=True))
    endpoint = MCPServerConfig(
        name="ext:x", type=MCPTransport.sse, url="https://x.test/sse", source="extension"
    )
    assert await confine(sandbox, tmp_path, config=endpoint) is endpoint
    empty = stdio_config(command="")
    assert await confine(sandbox, tmp_path, config=empty) is empty
    assert sandbox.status("acme.probe") is None
    assert recorder.probes == 0 and recorder.prepared == []


async def test_a_sandbox_switched_off_returns_the_config_and_names_the_tier(
    recorder, tmp_path, caplog
):
    sandbox = sandbox_for(tmp_path, settings=SandboxSettings(enabled=False, disabled_by="user"))
    config = stdio_config()
    with caplog.at_level(logging.WARNING):
        assert await confine(sandbox, tmp_path, config=config) == config
    status = sandbox.status("acme.probe")
    assert status is not None and status.deliberate and not status.confined
    assert status.describe() == "not sandboxed: extensions.sandbox is false in the user settings"
    assert "runs UNSANDBOXED: extensions.sandbox is false (user settings)" in caplog.text
    assert recorder.probes == 0 and recorder.prepared == []


async def test_a_machine_without_a_sandbox_is_not_a_deliberate_choice(tmp_path, caplog):
    sandbox = ExtensionSandbox(settings=SandboxSettings(), platform="sunos5")
    await sandbox.confine(
        manifest(),
        stdio_config(),
        bundle=tmp_path,
        data_dir=None,
        project_dir=None,
        service_url=None,
        development=False,
    )
    assert not sandbox.status("acme.probe").deliberate


async def test_a_failed_prepare_leaves_no_proxy_no_run_directory_and_no_record(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    recorder.fail_prepare = RuntimeError("the mechanism broke")
    with pytest.raises(RuntimeError, match="the mechanism broke"):
        await confine(sandbox, tmp_path)
    assert sandbox.status("acme.probe") is None
    assert list((tmp_path / "runs").iterdir()) == []
    # The proxy it had started is not listening any more.
    port = recorder.proxies[0].port
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    # Nothing was prepared, so there is nothing for the mechanism to undo.
    assert recorder.released == []


async def test_release_stops_the_proxy_lets_the_mechanism_go_and_removes_the_run(
    recorder, tmp_path
):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    status = sandbox.status("acme.probe")
    run_dir, port, prepared = status.plan.run_dir, status.proxy.port, status.prepared
    (run_dir / "tmp" / "left-over.txt").write_text("x")
    await sandbox.release("acme.probe")
    assert recorder.released == [prepared]
    assert recorder.stopped_relays == [41001]
    assert not run_dir.exists()
    assert sandbox.status("acme.probe") is None
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    # Twice, and for one never confined, is nothing.
    await sandbox.release("acme.probe")
    await sandbox.release("acme.unknown")
    assert recorder.released == [prepared]


async def test_confining_the_same_extension_again_releases_the_earlier_run(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    first = sandbox.status("acme.probe")
    first_dir, first_port = first.plan.run_dir, first.proxy.port
    try:
        await confine(sandbox, tmp_path)
        second = sandbox.status("acme.probe")
        assert second is not first and second.plan.run_dir != first_dir
        assert recorder.released == [first.prepared]
        assert not first_dir.exists() and second.plan.run_dir.exists()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", first_port)
    finally:
        await sandbox.close()


async def test_close_releases_every_run(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path, m=manifest("acme.one"))
    await confine(sandbox, tmp_path, m=manifest("acme.two"))
    dirs = [sandbox.status(i).plan.run_dir for i in ("acme.one", "acme.two")]
    await sandbox.close()
    assert len(recorder.released) == 2
    assert not any(d.exists() for d in dirs)
    assert sandbox.status("acme.one") is None and sandbox.status("acme.two") is None


async def test_a_proxy_that_will_not_stop_is_said_and_the_rest_is_still_undone(
    recorder, tmp_path, caplog
):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    status = sandbox.status("acme.probe")
    real_stop = status.proxy.stop

    async def broken() -> None:
        await real_stop()
        raise RuntimeError("proxy stuck")

    status.proxy.stop = broken
    run_dir = status.plan.run_dir
    with caplog.at_level(logging.WARNING):
        await sandbox.release("acme.probe")
    assert "the egress proxy for acme.probe did not stop: proxy stuck" in caplog.text
    assert len(recorder.released) == 1 and not run_dir.exists()


async def test_a_mechanism_that_will_not_let_go_is_said_and_the_run_directory_still_goes(
    recorder, tmp_path, caplog
):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    run_dir = sandbox.status("acme.probe").plan.run_dir
    recorder.fail_release = RuntimeError("grants stuck")
    with caplog.at_level(logging.WARNING):
        await sandbox.release("acme.probe")
    assert "the sandbox for acme.probe did not let go: grants stuck" in caplog.text
    assert not run_dir.exists()


async def test_a_run_directory_that_will_not_be_removed_is_said_not_raised(
    recorder, tmp_path, caplog, monkeypatch
):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    run_dir = sandbox.status("acme.probe").plan.run_dir

    def refuse(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(sandbox_launcher.shutil, "rmtree", refuse)
    with caplog.at_level(logging.WARNING):
        await sandbox.release("acme.probe")
    assert f"the run directory {run_dir} was not removed" in caplog.text
    monkeypatch.undo()
    sandbox_launcher._remove_run_dir(run_dir)


async def test_a_run_directory_already_gone_is_not_worth_a_warning(recorder, tmp_path, caplog):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    run_dir = sandbox.status("acme.probe").plan.run_dir
    shutil.rmtree(run_dir)
    with caplog.at_level(logging.WARNING):
        await sandbox.release("acme.probe")
    assert "was not removed" not in caplog.text


async def test_a_release_before_any_probe_has_no_mechanism_to_ask(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    await confine(sandbox, tmp_path)
    fresh = sandbox_for(tmp_path)
    fresh._runs.update(sandbox._runs)
    sandbox._runs.clear()
    await fresh.release("acme.probe")
    assert recorder.released == []
    assert fresh.status("acme.probe") is None


async def test_the_platform_is_probed_once(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path)
    try:
        await confine(sandbox, tmp_path)
        await confine(sandbox, tmp_path)
        assert (await sandbox.availability()).name is MechanismName.BUBBLEWRAP
        assert recorder.probes == 1
    finally:
        await sandbox.close()


async def test_hosts_an_air_gap_withholds_and_a_plain_http_service_are_said(
    recorder, tmp_path, caplog
):
    network = NetworkPolicy(air_gapped=True, internal_hosts=["*.acme.internal"])
    sandbox = sandbox_for(tmp_path, network=network)
    m = manifest(egress=["api.github.com", "git.acme.internal"])
    try:
        with caplog.at_level(logging.WARNING):
            await confine(sandbox, tmp_path, m=m, service_url="http://svc.acme.internal:8201")
        assert "api.github.com withheld from its sandbox" in caplog.text
        assert "its service http://svc.acme.internal:8201 is plain http" in caplog.text
        assert sandbox.status("acme.probe").plan.hosts.entries == ["git.acme.internal:443"]
    finally:
        await sandbox.close()


async def test_a_machine_with_a_working_sandbox_never_refuses(recorder, tmp_path):
    sandbox = sandbox_for(tmp_path, settings=SandboxSettings(required=True))
    try:
        out = await confine(sandbox, tmp_path)
        assert out.command == "wrapper"
    finally:
        await sandbox.close()


async def test_a_refusal_is_a_permission_denied_naming_why(tmp_path):
    from igni_core.extensions.permissions import PermissionDenied

    sandbox = ExtensionSandbox(settings=SandboxSettings(required=True), platform="sunos5")
    with pytest.raises(SandboxRefused, match="acme.probe was not started") as caught:
        await confine(sandbox, tmp_path)
    assert isinstance(caught.value, PermissionDenied)
    assert sandbox.status("acme.probe") is None


def test_a_failure_to_read_the_group_policy_is_not_hidden_by_the_sandbox(monkeypatch):
    """Only the failures the settings loader names are read as "no pack";
    anything else is a bug, and ``for_settings`` lets it out rather than
    run the extensions without the organisation's ``require_sandbox``."""

    class Fetcher:
        @classmethod
        def for_settings(cls, settings):
            def fetch():
                raise ValueError("pack broke")

            return fetch

    monkeypatch.setattr(sandbox_launcher, "CachedSettingsFetcher", Fetcher)
    with pytest.raises(ValueError, match="pack broke"):
        ExtensionSandbox.for_settings(Settings())


def test_the_air_gap_comes_from_the_loaded_settings(monkeypatch):
    class Fetcher:
        @classmethod
        def for_settings(cls, settings):
            return lambda: None

    monkeypatch.setattr(sandbox_launcher, "CachedSettingsFetcher", Fetcher)
    seen = []
    marker = NetworkPolicy(air_gapped=True, internal_hosts=["*.acme.internal"])

    def from_settings(settings):
        seen.append(settings)
        return marker

    monkeypatch.setattr(NetworkPolicy, "from_settings", staticmethod(from_settings))
    settings = Settings()
    sandbox = ExtensionSandbox.for_settings(settings)
    assert seen == [settings] and sandbox._network is marker
