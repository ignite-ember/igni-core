"""A plugin reload must not undo the user's runtime choices.

``reload_plugins`` re-runs ``build_plugins_and_styles``, which
(a) reset ``_active_output_style`` to the config default and (b) built a
brand-new ``ToolEventHookFactory`` — and the factory is where the live
``PermissionEvaluator`` is cached. A replacement with an empty cache
rebuilt the evaluator from ``settings.permissions.mode``, so a user who
had put the session into plan mode — i.e. told the product not to touch
their tree — was silently returned to ``default`` by installing a
plugin. The FE's mode badge reads the same evaluator, so it flipped too,
with no message.

Both are user decisions made after boot; re-reading the config is only
right for the values the user has not overridden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from igni_core.core.config.permission_eval import PermissionMode
from igni_core.core.paths import CONFIG_DIR
from igni_core.core.session.parts.plugins import PluginBuilder
from tests.session_factory import build_session


@pytest.fixture
def session(tmp_path: Path):
    return build_session(tmp_path)


def test_permission_mode_survives_a_plugin_reload(session) -> None:
    session.mode.set_permission_mode(PermissionMode.PLAN.value)
    assert session.permission_mode_value == "plan"

    session.reload_plugins()

    # Read through the same accessor the FE's mode badge is built from.
    assert session.permission_mode_value == "plan", (
        "a plugin install put the session back into default mode — the user "
        "had told it not to touch their tree"
    )
    # And the evaluator every freshly-built ToolEventHook is handed —
    # i.e. the one the next tool call is judged by — agrees.
    assert session.tool_hook_factory.permission_evaluator.mode is PermissionMode.PLAN


def _write_styles(tmp_path: Path) -> Path:
    """Two styles, so the boot default and the user's pick differ."""
    styles = tmp_path / "proj" / CONFIG_DIR / "output-styles"
    styles.mkdir(parents=True, exist_ok=True)
    (styles / "default.md").write_text("---\nname: default\n---\n\nSay it plainly.\n")
    (styles / "terse.md").write_text("---\nname: terse\n---\n\nBe brief.\n")
    return styles


def test_output_style_survives_a_plugin_reload(session, tmp_path: Path) -> None:
    _write_styles(tmp_path)
    # Re-discover so the freshly written styles are known to the session.
    PluginBuilder(session).build_plugins_and_styles()
    assert session.output_style.active == "default"

    assert "Error" not in session.mode.set_output_style("terse")
    assert session.output_style.active == "terse"

    session.reload_plugins()

    assert session.output_style.active == "terse"
    # The style's body is re-applied from the name at build time, so
    # the rebuilt team must still carry it.
    assert any(isinstance(i, str) and "Be brief." in i for i in session.main_team.instructions), (
        "the style name survived but its body did not reach the rebuilt agent"
    )


def test_a_removed_style_falls_back_instead_of_sticking(session, tmp_path: Path) -> None:
    # The control on the fix: preserving the user's choice must not
    # pin a style that no longer exists (an uninstalled plugin's).
    styles = _write_styles(tmp_path)
    PluginBuilder(session).build_plugins_and_styles()
    session.mode.set_output_style("terse")

    (styles / "terse.md").unlink()
    session.reload_plugins()

    assert session.output_style.active == "default"
