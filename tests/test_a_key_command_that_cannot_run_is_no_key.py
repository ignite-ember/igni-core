"""A key command that cannot run is a key not found, not a crash.

``api_key_cmd`` is the person's (or a trusted project's) own command. A
program that is not installed, or a command line that cannot be split
into words, used to raise out of key resolution, which runs while the
session is being built, so one bad line of config took the backend down
instead of reaching the sentence ``why_no_api_key`` is there to say.
"""

from __future__ import annotations

from igni_core.core.config.model_entry import ModelRegistryEntry, ResolvedApiKey


def _entry(**fields) -> ModelRegistryEntry:
    fields.setdefault("model_id", "m")
    return ModelRegistryEntry.model_validate(fields)


class TestTheKeyCommand:
    def test_a_missing_program_is_no_key_not_a_crash(self):
        entry = _entry(api_key_cmd="/nonexistent/igni-key-helper --get")

        assert entry.resolve_api_key() is None

    def test_a_command_that_cannot_be_split_is_no_key_not_a_crash(self):
        entry = _entry(api_key_cmd="echo 'unterminated")

        assert entry.resolve_api_key() is None

    def test_a_command_that_cannot_run_still_lets_the_gateway_token_apply(self):
        entry = _entry(url="https://api.ignite-ember.sh/v1", api_key_cmd="/nonexistent/helper")

        assert entry.resolve_key("tok") == ResolvedApiKey(value="tok", cloud=True)
