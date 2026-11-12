"""Does igni work against a real server's group configuration?

The unit tests cover each piece against fixtures. This runs the whole
path against a live deployment: fetch the pack with igni's own client,
materialise it, and ask each loader whether it can see what the group
shipped. A file written into a directory nothing scans passes every unit
test and fails here.

    IGNI_API=http://localhost:7777 .venv/bin/python scripts/check_group_config.py

Point it at a group holding one entry of each kind — the checks skip
kinds the group does not ship, so a thin group reports a thin pass.
Exits non-zero if anything failed, so it can gate a release.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from igni_core.backend.workflow_discovery import WorkflowDiscovery  # noqa: E402
from igni_core.core.agents.pool import AgentPool  # noqa: E402
from igni_core.core.auth.portal_client import PortalClient  # noqa: E402
from igni_core.core.config.group_policy.cache import GroupPolicyCache  # noqa: E402
from igni_core.core.config.group_policy.layout import GroupPolicyLayout  # noqa: E402
from igni_core.core.config.settings import Settings  # noqa: E402
from igni_core.core.hooks.loader import HookLoader  # noqa: E402
from igni_core.core.output_styles.loader import discover_output_styles  # noqa: E402
from igni_core.core.skills.loader import SkillPool  # noqa: E402
from igni_core.core.utils.markdown_commands import MarkdownCommand  # noqa: E402
from igni_core.core.utils.rules_index import RulesIndex  # noqa: E402

API = os.environ.get("IGNI_API", "http://localhost:7777")
TOKEN = os.environ.get("IGNI_TOKEN", "local-bypass")

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"  {'ok  ' if ok else 'FAIL'}  {what}")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def main() -> int:
    print(f"\nFetching the pack from {API} with igni's own client")
    pack = await PortalClient(api_url=API).fetch_group_pack(TOKEN)
    if pack is None:
        print("  FAIL  no pack came back — is the server up and the user in a group?")
        return 1

    by_kind: dict[str, list[str]] = {}
    for entry in pack.entries:
        by_kind.setdefault(entry.kind, []).append(entry.entry_name)
    print(f"  group {pack.group_name!r}, {len(pack.entries)} entries")
    for kind, names in sorted(by_kind.items()):
        print(f"    {kind}: {len(names)}")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        (project / ".igni").mkdir(parents=True)
        cache = GroupPolicyCache(GroupPolicyLayout(root=home / "group-policy"))

        print("\nMaterialising")
        cache.materialize(pack)
        for kind in by_kind:
            if kind in ("settings", "plugins"):
                continue
            directory = cache.layout.dir_for(kind)
            check(
                directory.is_dir() and any(directory.iterdir()),
                f"{kind} written to {directory.name}/",
            )

        print("\nLoading, as a session would")

        # Nothing is copied into the project: each loader is handed the
        # group's directory and reads it where it lies.
        if "agents" in by_kind:
            pool = AgentPool(Settings.defaults(), project)
            pool.load_definitions(
                Settings.defaults(), project, group_dir=cache.layout.dir_for("agents")
            )
            loaded = {d.name for d in pool.list_agents()}
            check(
                set(by_kind["agents"]) <= loaded,
                "every agent loads under its own name",
            )

        if "skills" in by_kind:
            skills = SkillPool()
            skills.load_all(project, group_dir=cache.layout.dir_for("skills"))
            loaded = {name for name in by_kind["skills"] if skills.get(name)}
            check(loaded == set(by_kind["skills"]), f"{len(loaded)} skills load")

        if "commands" in by_kind:
            found = MarkdownCommand.discover(
                project, read_claude=False, group_dir=cache.layout.dir_for("commands")
            )
            check(set(by_kind["commands"]) <= set(found), "commands load")

        if "output-styles" in by_kind:
            styles = discover_output_styles(
                project, read_claude=False, group_dir=cache.layout.dir_for("output-styles")
            )
            check(set(by_kind["output-styles"]) <= set(styles), "output styles load")

        if "hooks" in by_kind:
            # The loader also reads this machine's own settings, so
            # "some hook is registered" would pass without the group's.
            # Look for the command the group actually shipped.
            shipped = {}
            for entry in pack.entries:
                if entry.kind != "hooks":
                    continue
                try:
                    shipped[entry.entry_name] = json.loads(entry.content)
                except json.JSONDecodeError as exc:
                    check(
                        False,
                        f"hooks/{entry.entry_name} is not valid JSON ({exc.msg} at line {exc.lineno} column {exc.colno})",
                    )
            wanted = {d.get("command") for d in shipped.values() if isinstance(d, dict)}
            result = HookLoader(
                project, cross_tool_support=False, group_dir=cache.layout.dir_for("hooks")
            ).load()
            registered = {
                h.command
                for event in ("PreToolUse", "PostToolUse", "Stop", "SubagentStop")
                for h in result.registry.for_event(event)
            }
            check(
                wanted <= registered,
                f"the group's own hooks register ({', '.join(sorted(wanted))})",
            )

        if "rules" in by_kind:
            index = RulesIndex(
                project, read_claude_md=False, group_rules_dir=cache.layout.dir_for("rules")
            )
            # The seeded rule is scoped to **/*.py, so touching one is
            # what should surface it.
            matched = index.consume_path(project / "app" / "main.py")
            check(bool(matched), "a path-scoped rule fires on a matching file")

        if "workflows" in by_kind:
            discovery = WorkflowDiscovery.for_project(
                project_dir=project, group_dir=cache.layout.dir_for("workflows")
            )
            names = {p.stem for p in discovery.paths()}
            check(set(by_kind["workflows"]) <= names, "workflows are discovered")

        print("\nSecond materialise (nothing should move)")
        before = _snapshot(home)
        cache.materialize(pack)
        check(_snapshot(home) == before, "re-materialising the same pack is a no-op")
        check(
            not (project / ".igni" / "agents").exists(),
            "and nothing of the group's was written into the project",
        )

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for w in failed:
        print(f"  failed: {w}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
