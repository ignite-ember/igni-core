"""Tests for prompts/__init__.py — prompt template loading."""

import pytest

from igni_core.core.prompts import PROMPTS_DIR, PromptFragments, PromptLoader


@pytest.fixture
def loader() -> PromptLoader:
    return PromptLoader(PromptFragments())


class TestLoadPrompt:
    def test_loads_main_agent_prompt(self, loader):
        result = loader.load("main_agent")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_raises_on_missing_prompt(self, loader):
        with pytest.raises(FileNotFoundError):
            loader.load("nonexistent_prompt_xyz")

    def test_prompts_dir_exists(self):
        assert PROMPTS_DIR.exists()
        assert PROMPTS_DIR.is_dir()

    def test_result_is_stripped(self, loader):
        result = loader.load("main_agent")
        assert result == result.strip()
