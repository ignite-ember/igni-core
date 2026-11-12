"""A real HTTP server that answers like igni server's group-pack endpoint.

The group-check scripts take an address in ``IGNI_API`` and speak to it
with igni's own client, so the honest way to run them is against a socket.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

AGENT = "---\nname: group-reviewer\ndescription: Reviews for the group\n---\nReview body.\n"
SKILL = "---\nname: group-skill\ndescription: A skill the group ships\n---\nDo the thing.\n"
COMMAND = "---\ndescription: A group command\n---\nSay hello to $ARGUMENTS.\n"
STYLE = "---\nname: group-style\ndescription: A group style\n---\nBe terse.\n"
RULE = '---\ndescription: py rule\npaths:\n  - "**/*.py"\n---\nUse type hints.\n'
WORKFLOW = "name: group-flow\nsteps: []\n"


def entry(kind: str, name: str, content: str, content_type: str = "markdown") -> dict:
    return {
        "kind": kind,
        "entry_name": name,
        "content": content,
        "content_type": content_type,
        "enabled": True,
        "source_url": None,
        "source_ref": None,
        "source_subdir": None,
    }


def hook_entry(command: str = "group-guard.sh") -> dict:
    declaration = {"event": "PreToolUse", "type": "command", "command": command}
    return entry("hooks", "guard", json.dumps(declaration), "json")


def full_entries() -> list[dict]:
    return [
        entry("agents", "group-reviewer", AGENT),
        entry("skills", "group-skill", SKILL),
        entry("commands", "group-hello", COMMAND),
        entry("output-styles", "group-style", STYLE),
        entry("rules", "group-py-rule", RULE),
        entry("workflows", "group-flow", WORKFLOW, "yaml"),
        entry("mcps", "group-mcp", '{"command": "gh-mcp", "args": [], "env": {}}', "json"),
        hook_entry(),
    ]


class PackServer:
    """Where the server listens, and the bearer tokens it was shown."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.authorizations: list[str] = []


@contextmanager
def group_pack_server(entries: list[dict] | None) -> Iterator[PackServer]:
    """Serve ``entries`` as the caller's group pack; ``None`` means no group (404)."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.headers.get("Authorization", ""))
            if entries is None or not self.path.endswith("/group/pack"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "group_id": "g-1",
                    "group_name": "Engineering",
                    "fetched_at": "2026-07-28T10:00:00+00:00",
                    "entries": entries,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pack_server = PackServer(f"http://127.0.0.1:{server.server_address[1]}")
        pack_server.authorizations = seen
        yield pack_server
    finally:
        server.shutdown()
        server.server_close()
