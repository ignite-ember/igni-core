"""First run in a folder igni does not trust writes no settings it then ignores.

The starter ``.igni/settings.local.json`` carried a ``permissions`` block,
and a project may set permissions only once its folder is trusted — so
the next start warned that the file igni had just written was ignored.
"""

from __future__ import annotations

import pytest

from igni_core.core.init.project_initializer import ProjectInitializer
from igni_core.core.workspace_trust import WorkspaceTrust

pytestmark = pytest.mark.workspace_trust


def test_an_untrusted_folder_gets_no_permissions_file(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    ProjectInitializer(project_dir=project, trust=WorkspaceTrust.shared()).run()
    assert not (project / ".igni" / "settings.local.json").exists() or (
        "permissions" not in (project / ".igni" / "settings.local.json").read_text()
    )


def test_a_trusted_folder_still_gets_its_starter_permissions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    WorkspaceTrust.shared().trust(project, remember=False)
    ProjectInitializer(project_dir=project, trust=WorkspaceTrust.shared()).run()
    assert "permissions" in (project / ".igni" / "settings.local.json").read_text()
