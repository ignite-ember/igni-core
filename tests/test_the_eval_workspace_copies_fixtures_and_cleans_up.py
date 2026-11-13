"""The eval work dir: fixtures copied in, the setup hook run, everything removed after."""

import sys
import textwrap
from pathlib import Path

import pytest

from igni_core.core.evals.schemas import FixtureSpec
from igni_core.core.evals.workspace import EvalWorkspace


@pytest.fixture
def roots(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    return fixtures, project


def workspace(roots, fixtures=None, setup_module=None):
    return EvalWorkspace(fixtures, roots[0], setup_module, roots[1])


@pytest.fixture
def clean_modules():
    before = set(sys.modules)
    path = list(sys.path)
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]
    sys.path[:] = path


class TestFixtures:
    async def test_no_fixtures_gives_an_empty_fresh_dir(self, roots):
        for fixtures in (None, []):
            ws = workspace(roots, fixtures)
            work = await ws.setup()
            try:
                assert work.is_dir()
                assert work.name.startswith("igni-eval-")
                assert list(work.iterdir()) == []
            finally:
                await ws.teardown()

    async def test_a_file_is_copied_to_a_nested_target(self, roots):
        (roots[0] / "a.txt").write_text("hello")
        ws = workspace(roots, [FixtureSpec(source="a.txt", target="deep/er/b.txt")])
        async with ws as work:
            assert (work / "deep/er/b.txt").read_text() == "hello"

    async def test_a_directory_is_merged_into_an_existing_target(self, roots):
        (roots[0] / "d").mkdir()
        (roots[0] / "d/new.txt").write_text("new")
        (roots[0] / "one.txt").write_text("one")
        ws = workspace(
            roots,
            [
                FixtureSpec(source="one.txt", target="t/one.txt"),
                FixtureSpec(source="d", target="t"),
            ],
        )
        async with ws as work:
            assert (work / "t/one.txt").read_text() == "one"
            assert (work / "t/new.txt").read_text() == "new"

    async def test_a_missing_source_or_empty_target_is_skipped(self, roots):
        (roots[0] / "a.txt").write_text("x")
        ws = workspace(
            roots,
            [
                FixtureSpec(source="nope.txt", target="n.txt"),
                FixtureSpec(source="a.txt", target=""),
            ],
        )
        async with ws as work:
            assert list(work.iterdir()) == []


class TestSetupModule:
    def write_module(self, project, body, name="suite_hook"):
        (project / f"{name}.py").write_text(textwrap.dedent(body))
        return name

    async def test_a_sync_hook_gets_work_dir_and_project_dir(self, roots, clean_modules):
        name = self.write_module(
            roots[1],
            """
            def setup(work_dir, project_dir):
                (work_dir / "seen.txt").write_text(str(project_dir))
            """,
            "hook_sync",
        )
        async with workspace(roots, setup_module=name) as work:
            assert (work / "seen.txt").read_text() == str(roots[1])

    async def test_an_async_hook_is_awaited(self, roots, clean_modules):
        name = self.write_module(
            roots[1],
            """
            async def setup(work_dir, project_dir):
                (work_dir / "async.txt").write_text("done")
            """,
            "hook_async",
        )
        async with workspace(roots, setup_module=name) as work:
            assert (work / "async.txt").read_text() == "done"

    async def test_the_project_dir_is_on_sys_path_only_while_open(self, roots, clean_modules):
        name = self.write_module(roots[1], "def setup(w, p):\n    pass\n", "hook_path")
        ws = workspace(roots, setup_module=name)
        assert str(roots[1]) not in sys.path
        await ws.setup()
        assert sys.path[0] == str(roots[1])
        await ws.teardown()
        assert str(roots[1]) not in sys.path

    async def test_an_entry_already_on_sys_path_is_left_alone(self, roots, clean_modules):
        name = self.write_module(roots[1], "def setup(w, p):\n    pass\n", "hook_present")
        sys.path.append(str(roots[1]))
        count = sys.path.count(str(roots[1]))
        async with workspace(roots, setup_module=name):
            assert sys.path.count(str(roots[1])) == count
        assert sys.path.count(str(roots[1])) == count

    async def test_a_module_without_setup_is_refused_and_nothing_is_left(
        self, roots, clean_modules
    ):
        name = self.write_module(roots[1], "X = 1\n", "hook_none")
        ws = workspace(roots, setup_module=name)
        with pytest.raises(RuntimeError, match="has no ``setup`` callable"):
            await ws.setup()
        assert str(roots[1]) not in sys.path

    async def test_a_failed_setup_removes_the_temp_dir(self, roots, clean_modules):
        name = self.write_module(
            roots[1],
            """
            def setup(work_dir, project_dir):
                (project_dir / "work_dir_was.txt").write_text(str(work_dir))
                raise ValueError("bad seed")
            """,
            "hook_raises",
        )
        ws = workspace(roots, setup_module=name)
        with pytest.raises(ValueError, match="bad seed"):
            await ws.setup()
        leaked = Path((roots[1] / "work_dir_was.txt").read_text())
        assert not leaked.exists()
        assert str(roots[1]) not in sys.path

    async def test_an_import_error_propagates(self, roots, clean_modules):
        ws = workspace(roots, setup_module="no_such_hook_module")
        with pytest.raises(ModuleNotFoundError):
            await ws.setup()


class TestTeardown:
    async def test_it_deletes_the_dir_and_is_idempotent(self, roots):
        ws = workspace(roots)
        work = await ws.setup()
        (work / "f").write_text("x")
        await ws.teardown()
        assert not work.exists()
        await ws.teardown()

    async def test_teardown_before_setup_is_harmless(self, roots):
        await workspace(roots).teardown()

    async def test_the_context_manager_cleans_up_when_the_body_raises(self, roots):
        ws = workspace(roots)
        with pytest.raises(KeyError):
            async with ws as work:
                seen = work
                raise KeyError("x")
        assert not seen.exists()
