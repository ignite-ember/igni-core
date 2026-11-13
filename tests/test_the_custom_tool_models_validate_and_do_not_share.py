"""The typed diagnostics the custom-tool loader returns.

A caller shows these to explain why a tool the person expected is missing, so
a reason outside the three the loader produces must be refused rather than
carried, two results must never share a list, and a path given as text is a
path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.tools import Toolkit
from pydantic import ValidationError

from igni_core.core.tools.custom_loader_schemas import (
    DiscoveryResult,
    FailedFile,
    LoadedFile,
    PluginToolDir,
    SkippedFile,
    ToolSource,
)


@pytest.mark.parametrize("reason", ["underscore_prefix", "no_functions", "not_a_directory"])
def test_each_documented_skip_reason_is_accepted(reason):
    assert SkippedFile(path=Path("/t/x.py"), reason=reason).reason == reason


@pytest.mark.parametrize("reason", ["", "syntax_error", "UNDERSCORE_PREFIX", "failed"])
def test_a_skip_reason_outside_the_three_is_refused(reason):
    with pytest.raises(ValidationError):
        SkippedFile(path=Path("/t/x.py"), reason=reason)  # type: ignore[arg-type]


def test_a_new_result_is_empty_in_all_four_lists():
    result = DiscoveryResult()

    assert (result.toolkits, result.loaded, result.skipped, result.failed) == ([], [], [], [])


def test_two_results_never_share_a_list():
    first, second = DiscoveryResult(), DiscoveryResult()

    first.toolkits.append(Toolkit(name="t"))
    first.loaded.append(LoadedFile(path=Path("/a.py"), toolkit_name="custom_a", function_count=1))
    first.skipped.append(SkippedFile(path=Path("/b.py"), reason="no_functions"))
    first.failed.append(FailedFile(path=Path("/c.py"), error="x", error_type="ValueError"))

    assert (second.toolkits, second.loaded, second.skipped, second.failed) == ([], [], [], [])


def test_a_result_holds_agno_toolkits_as_they_are():
    toolkit = Toolkit(name="custom_x")

    result = DiscoveryResult(toolkits=[toolkit])

    assert result.toolkits[0] is toolkit


def test_a_path_given_as_text_becomes_a_path():
    assert ToolSource(name_prefix="custom", tools_dir="/t/tools").tools_dir == Path("/t/tools")
    assert PluginToolDir(plugin_name="acme", tools_dir="/p/tools").tools_dir == Path("/p/tools")
    assert LoadedFile(path="/t/a.py", toolkit_name="custom_a", function_count=2).path == Path(
        "/t/a.py"
    )
    assert FailedFile(path="/t/b.py", error="boom", error_type="ImportError").path == Path(
        "/t/b.py"
    )


def test_a_plugin_s_name_and_directory_travel_together():
    plugin = PluginToolDir(plugin_name="acme", tools_dir=Path("/p/tools"))

    assert (plugin.plugin_name, plugin.tools_dir) == ("acme", Path("/p/tools"))
    with pytest.raises(ValidationError):
        PluginToolDir(plugin_name="acme")  # type: ignore[call-arg]
