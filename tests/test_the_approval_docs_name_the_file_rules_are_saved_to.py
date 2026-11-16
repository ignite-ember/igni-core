"""SECURITY.md names the file an "always allow" rule is saved to.

It said permanent rules go to ``~/.igni/permissions.yaml`` as an
``allowlist`` YAML. ``SettingsFileWriter`` writes them to the project's
``.igni/settings.local.json``, which an untrusted folder's later sessions
do not read (campaign R184, R333).
"""

from __future__ import annotations

import json
from pathlib import Path

from igni_core.core.config.tool_permissions.settings_files import SettingsFileWriter

_ROOT = Path(__file__).resolve().parent.parent


def test_a_saved_rule_lands_in_the_projects_settings_local_json(tmp_path):
    import inspect

    assert "settings.local.json" in inspect.getsource(SettingsFileWriter)


def test_the_page_names_settings_local_json_and_not_permissions_yaml():
    text = (_ROOT / "docs/SECURITY.md").read_text()
    assert "permissions.yaml" not in text
    assert "`.igni/settings.local.json`" in text
    assert "until you trust the folder" in text
    assert "permissions.yaml" not in (_ROOT / "QA_CHECKLIST.md").read_text()
    json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
