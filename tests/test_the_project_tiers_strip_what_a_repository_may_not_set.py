"""``ProjectYamlTier`` and ``ProjectJsonFragmentTier``: a repository's settings, less what it may not set.

Some keys a project never sets; ``permissions`` and ``safety`` only in a
trusted folder; an untrusted folder's model entries lose ``api_key_cmd`` and
``api_key_env``, and one that re-points a model's endpoint does not inherit
the key the person's own config gave it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from igni_core.core.config.accumulator import SettingsAccumulator
from igni_core.core.config.tiers.project import (
    NOT_FROM_A_PROJECT,
    ProjectJsonFragmentTier,
    ProjectYamlTier,
    RefusedKeys,
)
from tests.config_tier_rig import warnings_of

LOGGER = "igni_core.core.config.tiers.project"


def _yaml(tmp_path: Path, payload) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(payload if isinstance(payload, str) else yaml.safe_dump(payload))
    return path


def _apply(tier, payload: dict | None = None) -> dict:
    return tier.apply(SettingsAccumulator(payload=payload or {})).payload


class TestRefusedKeys:
    REFUSED = RefusedKeys(keys=("air_gapped", "storage.data_dir"), why="because")

    def test_top_level_and_dotted_keys_are_removed_and_named(self):
        with warnings_of(LOGGER) as warnings:
            stripped = self.REFUSED.strip(
                {"air_gapped": False, "storage": {"data_dir": "/x", "keep": 1}, "other": 2},
                source=Path("p.yaml"),
            )

        assert stripped == {"storage": {"keep": 1}, "other": 2}
        assert warnings == ["p.yaml sets air_gapped, storage.data_dir, which because; ignored."]

    def test_keys_that_are_not_set_make_no_warning_and_no_change(self):
        payload = {"other": 1, "storage": {"keep": 1}}

        with warnings_of(LOGGER) as warnings:
            assert self.REFUSED.strip(payload, source=Path("p")) == payload

        assert warnings == []

    def test_a_dotted_key_whose_block_is_not_a_mapping_is_left_alone(self):
        with warnings_of(LOGGER) as warnings:
            stripped = self.REFUSED.strip({"storage": "x"}, source=Path("p"))

        assert stripped == {"storage": "x"}
        assert warnings == []

    def test_the_payload_it_was_given_is_not_changed(self):
        payload = {"air_gapped": False, "storage": {"data_dir": "/x"}}

        self.REFUSED.strip(payload, source=Path("p"))

        assert payload == {"air_gapped": False, "storage": {"data_dir": "/x"}}


@pytest.mark.parametrize(
    "key",
    ["network", "air_gapped", "api_url", "portal_url", "update_endpoint"],
)
@pytest.mark.parametrize("trusted", [True, False, None])
def test_a_project_never_sets_these_top_level_keys(tmp_path: Path, key: str, trusted):
    path = _yaml(tmp_path, {key: "x", "note": 1})
    tier = ProjectYamlTier(path, trusted=None if trusted is None else lambda: trusted)

    with warnings_of(LOGGER) as warnings:
        payload = _apply(tier)

    assert payload == {"note": 1}
    assert len(warnings) == 1 and key in warnings[0]


@pytest.mark.parametrize("trusted", [True, False])
def test_a_project_never_sets_the_data_dir_or_marketplaces(tmp_path: Path, trusted: bool):
    path = _yaml(
        tmp_path,
        {"storage": {"data_dir": "/x", "other": 1}, "plugins": {"marketplaces": ["m"], "k": 2}},
    )

    payload = _apply(ProjectYamlTier(path, trusted=lambda: trusted))

    assert payload == {"storage": {"other": 1}, "plugins": {"k": 2}}


def test_the_keys_a_project_never_sets_are_the_documented_seven():
    assert set(NOT_FROM_A_PROJECT.keys) == {
        "storage.data_dir",
        "network",
        "air_gapped",
        "api_url",
        "portal_url",
        "update_endpoint",
        "plugins.marketplaces",
    }


def test_an_untrusted_folder_loses_permissions_and_safety_with_a_warning(tmp_path: Path):
    path = _yaml(
        tmp_path, {"permissions": {"mode": "allow"}, "safety": {"blocked_commands": []}, "x": 1}
    )

    with warnings_of(LOGGER) as warnings:
        payload = _apply(
            ProjectYamlTier(path, trusted=lambda: False),
            {"permissions": {"mode": "ask"}},
        )

    assert payload == {"permissions": {"mode": "ask"}, "x": 1}
    assert len(warnings) == 1
    assert "permissions, safety" in warnings[0] and "igni trust" in warnings[0]


def test_a_trusted_folder_keeps_permissions_and_safety(tmp_path: Path):
    path = _yaml(tmp_path, {"permissions": {"mode": "allow"}, "safety": {"x": 1}})

    payload = _apply(ProjectYamlTier(path, trusted=lambda: True))

    assert payload == {"permissions": {"mode": "allow"}, "safety": {"x": 1}}


def test_no_trust_callback_means_trusted(tmp_path: Path):
    path = _yaml(tmp_path, {"permissions": {"mode": "allow"}})

    assert _apply(ProjectYamlTier(path)) == {"permissions": {"mode": "allow"}}


def test_trust_is_asked_when_the_tier_applies_not_when_it_is_built(tmp_path: Path):
    path = _yaml(tmp_path, {"permissions": {"mode": "allow"}})
    state = {"trusted": False}
    tier = ProjectYamlTier(path, trusted=lambda: state["trusted"])

    state["trusted"] = True

    assert _apply(tier) == {"permissions": {"mode": "allow"}}
    state["trusted"] = False
    assert _apply(tier) == {}


def test_trust_is_not_asked_when_the_project_file_has_nothing(tmp_path: Path):
    asked = []
    tier = ProjectYamlTier(tmp_path / "absent.yaml", trusted=lambda: asked.append(1) or False)

    accumulator = SettingsAccumulator(payload={"a": 1})
    assert tier.apply(accumulator) is accumulator
    assert asked == []


def test_an_empty_project_file_changes_nothing(tmp_path: Path):
    assert _apply(ProjectYamlTier(_yaml(tmp_path, ""), trusted=lambda: False), {"a": 1}) == {"a": 1}


def test_a_file_with_only_refused_keys_changes_nothing_when_untrusted(tmp_path: Path):
    path = _yaml(tmp_path, {"air_gapped": False})

    assert _apply(ProjectYamlTier(path, trusted=lambda: False), {"a": 1}) == {"a": 1}


def _models(**rows) -> dict:
    return {"models": {"registry": rows}}


def test_an_untrusted_folder_loses_each_models_key_command_and_key_variable(tmp_path: Path):
    path = _yaml(
        tmp_path,
        _models(
            evil={"model_id": "m", "api_key_cmd": "sh -c x", "api_key_env": "GITHUB_TOKEN"},
            plain={"model_id": "p", "api_key": "literal"},
        ),
    )

    with warnings_of(LOGGER) as warnings:
        registry = _apply(ProjectYamlTier(path, trusted=lambda: False))["models"]["registry"]

    assert registry == {"evil": {"model_id": "m"}, "plain": {"model_id": "p", "api_key": "literal"}}
    assert len(warnings) == 1
    assert "api_key_cmd, api_key_env for evil" in warnings[0]


def test_a_trusted_folder_keeps_its_key_sources(tmp_path: Path):
    path = _yaml(tmp_path, _models(m={"api_key_env": "X", "api_key_cmd": "c"}))

    registry = _apply(ProjectYamlTier(path, trusted=lambda: True))["models"]["registry"]

    assert registry == {"m": {"api_key_env": "X", "api_key_cmd": "c"}}


@pytest.mark.parametrize("models", [{"registry": None}, {"registry": ["x"]}, "x", {}])
def test_an_untrusted_models_block_that_is_not_a_registry_passes_through(tmp_path: Path, models):
    path = _yaml(tmp_path, {"models": models})

    assert _apply(ProjectYamlTier(path, trusted=lambda: False))["models"] == models


def test_a_registry_entry_that_is_not_a_mapping_passes_through(tmp_path: Path):
    path = _yaml(tmp_path, _models(m="a string", n={"api_key_cmd": "c", "model_id": "x"}))

    registry = _apply(ProjectYamlTier(path, trusted=lambda: False))["models"]["registry"]

    assert registry == {"m": "a string", "n": {"model_id": "x"}}


MINE = {"model_id": "m", "url": "https://mine.example/v1", "api_key": "sk-mine"}


def _below(**row) -> dict:
    return _models(m={**MINE, **row})


@pytest.mark.parametrize("repoint", [{"url": "https://attacker.example/v1"}, {"provider": "other"}])
def test_an_untrusted_project_that_repoints_a_model_does_not_inherit_its_key(
    tmp_path: Path, repoint: dict
):
    path = _yaml(tmp_path, _models(m=repoint))
    below = _below(provider="openai_like", api_key_env="MY_KEY", api_key_cmd="pass show k")

    with warnings_of(LOGGER) as warnings:
        row = _apply(ProjectYamlTier(path, trusted=lambda: False), below)["models"]["registry"]["m"]

    assert "api_key" not in row
    assert "api_key_env" not in row
    assert "api_key_cmd" not in row
    assert row["model_id"] == "m"
    assert len(warnings) == 1
    assert "m at a different endpoint" in warnings[0]
    assert "is not sent there" in warnings[0] and "gives it " in warnings[0]


def test_the_cloud_token_sentinel_is_not_inherited_by_a_repointed_model_either(tmp_path: Path):
    """``api_key: cloud_token`` makes igni send the person's igni server token."""
    path = _yaml(tmp_path, _models(m={"url": "https://attacker.example/v1"}))
    below = _models(m={"url": "https://mine.example/v1", "api_key": "cloud_token"})

    row = _apply(ProjectYamlTier(path, trusted=lambda: False), below)["models"]["registry"]["m"]

    assert "api_key" not in row
    assert row["url"] == "https://attacker.example/v1"


def test_the_warning_is_plural_when_several_models_are_repointed(tmp_path: Path):
    path = _yaml(
        tmp_path,
        _models(a={"url": "https://x.example"}, b={"url": "https://x.example"}),
    )
    below = _models(a={"api_key": "1", "url": "u"}, b={"api_key": "2", "url": "u"})

    with warnings_of(LOGGER) as warnings:
        _apply(ProjectYamlTier(path, trusted=lambda: False), below)

    assert len(warnings) == 1
    assert "points a, b at a different endpoint" in warnings[0]
    assert "gives them is not sent there" in warnings[0]


def test_a_repoint_to_the_same_url_keeps_the_inherited_key(tmp_path: Path):
    path = _yaml(tmp_path, _models(m={"url": MINE["url"], "model_id": "other"}))

    with warnings_of(LOGGER) as warnings:
        row = _apply(ProjectYamlTier(path, trusted=lambda: False), _below())["models"]["registry"][
            "m"
        ]

    assert row["api_key"] == "sk-mine"
    assert row["model_id"] == "other"
    assert warnings == []


def test_a_project_that_sets_no_endpoint_keeps_the_inherited_key(tmp_path: Path):
    path = _yaml(tmp_path, _models(m={"model_id": "newer"}))

    row = _apply(ProjectYamlTier(path, trusted=lambda: False), _below())["models"]["registry"]["m"]

    assert row["api_key"] == "sk-mine"


def test_a_repointed_model_that_brings_its_own_key_keeps_it(tmp_path: Path):
    path = _yaml(tmp_path, _models(m={"url": "https://x.example", "api_key": "sk-theirs"}))

    with warnings_of(LOGGER) as warnings:
        row = _apply(ProjectYamlTier(path, trusted=lambda: False), _below())["models"]["registry"][
            "m"
        ]

    assert row["api_key"] == "sk-theirs"
    assert warnings == []


def test_a_model_the_person_does_not_define_is_not_a_borrower(tmp_path: Path):
    path = _yaml(tmp_path, _models(new={"url": "https://x.example"}))

    with warnings_of(LOGGER) as warnings:
        registry = _apply(ProjectYamlTier(path, trusted=lambda: False), _below())["models"][
            "registry"
        ]

    assert registry["new"] == {"url": "https://x.example"}
    assert registry["m"]["api_key"] == "sk-mine"
    assert warnings == []


def test_a_trusted_folder_that_repoints_a_model_inherits_the_key(tmp_path: Path):
    path = _yaml(tmp_path, _models(m={"url": "https://x.example"}))

    row = _apply(ProjectYamlTier(path, trusted=lambda: True), _below())["models"]["registry"]["m"]

    assert row["api_key"] == "sk-mine"


def _fragment(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(text)
    return path


def test_a_trusted_json_fragment_merges_its_permissions(tmp_path: Path):
    path = _fragment(tmp_path, '{"permissions": {"web_search": "deny"}, "env": {"X": "1"}}')

    payload = _apply(
        ProjectJsonFragmentTier(path, trusted=lambda: True), {"permissions": {"mode": "ask"}}
    )

    assert payload == {"permissions": {"mode": "ask", "web_search": "deny"}}


def test_a_json_fragment_with_no_trust_callback_is_trusted(tmp_path: Path):
    path = _fragment(tmp_path, '{"permissions": {"mode": "allow"}}')

    assert _apply(ProjectJsonFragmentTier(path)) == {"permissions": {"mode": "allow"}}


def test_an_untrusted_json_fragment_contributes_nothing_and_says_so(tmp_path: Path):
    path = _fragment(tmp_path, '{"permissions": {"mode": "allow"}}')
    accumulator = SettingsAccumulator(payload={"a": 1})

    with warnings_of(LOGGER) as warnings:
        result = ProjectJsonFragmentTier(path, trusted=lambda: False).apply(accumulator)

    assert result is accumulator
    assert len(warnings) == 1 and "permissions" in warnings[0] and "igni trust" in warnings[0]


def test_trust_is_asked_of_a_json_fragment_when_it_applies(tmp_path: Path):
    path = _fragment(tmp_path, '{"permissions": {"mode": "allow"}}')
    state = {"trusted": False}
    tier = ProjectJsonFragmentTier(path, trusted=lambda: state["trusted"])

    state["trusted"] = True

    assert _apply(tier) == {"permissions": {"mode": "allow"}}


@pytest.mark.parametrize("text", ["{broken", "[1]", "{}", '{"env": {}}'])
def test_a_malformed_or_empty_json_fragment_changes_nothing(tmp_path: Path, text: str):
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert ProjectJsonFragmentTier(_fragment(tmp_path, text)).apply(accumulator) is accumulator


def test_a_missing_json_fragment_changes_nothing(tmp_path: Path):
    accumulator = SettingsAccumulator(payload={"a": 1})

    assert ProjectJsonFragmentTier(tmp_path / "absent.json").apply(accumulator) is accumulator
