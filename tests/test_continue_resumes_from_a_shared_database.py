"""``igni -c`` resumes from a shared database, in the signed-in person's scope.

The resume lookup opened ``IGNI_DATABASE_URL``'s database before anyone told
storage tenancy who was signed in, so the tenant guard refused it every time
(``NoTenantError``) and the CLI hid that behind "Could not look up last
session." before starting a new one (campaign R025). The lookup now reads
the database in the signed-in person's scope, as the session does, and with
nobody signed in it says why it could not look.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from agno.session.agent import AgentSession

from igni_core.cli.headless_run import HeadlessRun
from igni_core.cli.invocation import CliInvocation
from igni_core.cli.options import CliOptions
from igni_core.cli.resume import ResumeLookup, ResumeResolver
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.config.settings import Settings
from igni_core.core.db.database import Database
from igni_core.core.db.providers import SharedStorages
from igni_core.core.db.scope import Tenant, tenancy
from igni_core.core.db.urls import DatabaseUrl
from igni_core.core.memory.manager import StorageManager

ME = Tenant(org="org-acme", producer="person:u-1")
SOMEONE_ELSE = Tenant(org="org-acme", producer="person:u-2")


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


@pytest.fixture
def settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: DatabaseUrl
) -> Settings:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    result = Settings()
    result.storage.data_dir = tmp_path / "data"
    result.storage.database_url = database_url.url
    result.auth.credentials_file = str(tmp_path / "credentials.json")
    # What ``igni migrate`` does first: igni's own tables, then the
    # dialect's (PostgreSQL: Agno's schema and its row-level security).
    Database(database_url)
    SharedStorages.for_url(database_url).prepare(database_url)
    return result


def _sign_in(settings: Settings) -> None:
    token = _jwt({"sub": "u-1", "org": "org-acme"})
    CredentialsStore(settings.auth.credentials_file).save(Credentials.new(token, "who@acme.test"))


async def _seed(settings: Settings, tenant: Tenant, *sessions: tuple[str, int]) -> None:
    tenancy.use(lambda: tenant)
    try:
        db = StorageManager.build_db(settings)
        for session_id, updated_at in sessions:
            await db.upsert_session(
                AgentSession(session_id=session_id, created_at=updated_at, updated_at=updated_at),
                deserialize=True,
            )
    finally:
        tenancy.use(None)


def _continue(settings: Settings) -> CliInvocation:
    invocation = CliInvocation(
        CliOptions(continue_session=True), settings, HeadlessRun(drain_timeout_seconds=0.05)
    )
    invocation.resolve_resume_id()
    return invocation


@pytest.mark.asyncio
async def test_continue_resumes_the_newest_session(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    now = int(time.time())
    await _seed(settings, ME, ("old", now - 100), ("newest", now))
    _sign_in(settings)

    invocation = await asyncio.to_thread(_continue, settings)

    assert invocation._resume_session_id == "newest"
    assert capsys.readouterr().err == "Resuming last session: newest\n"


@pytest.mark.asyncio
async def test_someone_else_s_newer_session_is_not_the_one_resumed(
    settings: Settings, database_url: DatabaseUrl
) -> None:
    """Where the storage keeps rows to their scope (igni-cloud's PostgreSQL),
    the newest session of the org is not the one resumed: the newest of the
    person signed in is."""
    if database_url.is_sqlite:
        pytest.skip("the test SQLite file is unscoped storage; scoping is PostgreSQL's")
    now = int(time.time())
    await _seed(settings, ME, ("mine", now - 50))
    await _seed(settings, SOMEONE_ELSE, ("theirs", now))
    _sign_in(settings)

    invocation = await asyncio.to_thread(_continue, settings)

    assert invocation._resume_session_id == "mine"


def test_the_lookup_opens_the_database_as_the_person_signed_in(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which rows a shared database hands back is its storage's to enforce
    (igni-cloud's PostgreSQL fixes Agno's ``user_id`` to the scope; the
    test SQLite file is unscoped): the lookup's part is opening it as the
    person signed in, as the session does."""
    opened_as: list[Tenant | None] = []
    build_db = StorageManager.build_db.__func__  # type: ignore[attr-defined]

    def spying(cls: type[StorageManager], *args: Any, **kwargs: Any) -> Any:
        opened_as.append(tenancy.tenant())
        return build_db(cls, *args, **kwargs)

    monkeypatch.setattr(StorageManager, "build_db", classmethod(spying))
    _sign_in(settings)

    _continue(settings)

    assert opened_as == [ME]


@pytest.mark.asyncio
async def test_an_explicit_session_id_is_checked_in_the_signed_in_scope(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unread, the check stayed silent about a typo'd id; read, it says so."""
    await _seed(settings, ME, ("mine", int(time.time())))
    _sign_in(settings)
    invocation = CliInvocation(
        CliOptions(session_id="typo"), settings, HeadlessRun(drain_timeout_seconds=0.05)
    )

    await asyncio.to_thread(invocation.resolve_resume_id)

    assert invocation._resume_session_id == "typo"
    assert capsys.readouterr().err == (
        "No session 'typo' in this project; starting a new one under that id.\n"
    )


def test_nobody_signed_in_says_why_it_could_not_look(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    invocation = _continue(settings)

    assert invocation._resume_session_id is None
    assert capsys.readouterr().err == (
        "Could not look up last session: IGNI_DATABASE_URL names a shared database, "
        "and nobody is signed in to scope its rows to. "
        "Sign in with `igni login`, or unset IGNI_DATABASE_URL.\n"
    )


def test_the_resolver_marks_the_refusal_as_nobody_signed_in(settings: Settings) -> None:
    lookup = ResumeResolver(settings).latest_id()

    assert lookup.no_tenant is True
    assert lookup.session_id is None
    assert lookup.error is not None and "nobody is signed in" in lookup.error


def test_any_other_failure_is_not_taken_for_nobody_signed_in(settings: Settings) -> None:
    settings.storage.database_url = "mysql://nobody@localhost/none"
    tenancy.use(lambda: ME)

    lookup = ResumeResolver(settings).latest_id()

    assert lookup.error and lookup.no_tenant is False
    assert lookup != ResumeLookup()
