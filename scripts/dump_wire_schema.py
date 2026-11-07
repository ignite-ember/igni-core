"""Write the FE-facing wire schema igni-core publishes, docs/wire-schema.json.

Covers (a) every pydantic protocol message and (b) the ad-hoc dict
payloads returned by RPCs the web client consumes. igni-app copies it
beside the web client, whose suite asserts the field names it reads
against that copy, so a BE rename
breaks a test instead of silently blanking the UI (the DiffRow /
is_ephemeral / loop_status / p.content bug class).

Regenerate after protocol changes:
    uv run python scripts/dump_wire_schema.py

The reflection itself lives in
:mod:`ember_code.protocol.wire_schema`, so a test can run it and
compare against the committed file. It used to live here, where
nothing could check it — and it silently produced an empty schema for
however long it took anyone to look.
"""

from ember_code.protocol.wire_schema import write


def main() -> None:
    path, schema = write()
    print(f"wrote {path} ({len(schema['messages'])} messages, {len(schema['rpc'])} rpc payloads)")


if __name__ == "__main__":
    main()
