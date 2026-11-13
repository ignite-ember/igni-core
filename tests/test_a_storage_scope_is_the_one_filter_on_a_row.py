"""A :class:`StorageScope` says whose rows; :class:`Tenancy` says who is signed in.

The scope is the one place the org, producer and project filter and stamp
are written, and the one string Agno's rows are found by. The tenancy is
asked on every open, never remembered.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from igni_core.core.data_paths import resolve_project_id
from igni_core.core.db.scope import (
    NoTenantError,
    ScopedColumns,
    StorageScope,
    Tenancy,
    Tenant,
    tenancy,
)

ACME = Tenant(org="acme", producer="person:1")


class _Base(DeclarativeBase):
    pass


class Note(ScopedColumns, _Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(default="")


@pytest.fixture
def session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'notes.db'}")
    _Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_the_desktop_scope_is_empty_and_not_shared():
    scope = StorageScope.local()
    assert (scope.org, scope.producer, scope.project, scope.shared) == ("", "", "", False)
    assert scope.stamp() == {"org": "", "producer": "", "project": ""}
    assert scope.agno_user_id == "//"


def test_a_tenants_scope_is_shared_and_names_its_project():
    scope = StorageScope.for_tenant(ACME, "folder")
    assert scope.shared is True
    assert scope.stamp() == {"org": "acme", "producer": "person:1", "project": "folder"}
    assert StorageScope.for_tenant(ACME).project == ""


def test_agno_finds_its_rows_by_the_org_producer_project_string():
    assert StorageScope.for_tenant(ACME, "folder").agno_user_id == "acme/person:1/folder"
    assert StorageScope(org="é", producer="日本", project="").agno_user_id == "é/日本/"


def test_a_scope_and_a_tenant_cannot_be_changed_after_they_are_made():
    scope = StorageScope.for_tenant(ACME, "folder")
    with pytest.raises(ValidationError):
        scope.project = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ACME.org = "other"  # type: ignore[misc]


def test_a_shared_scope_narrows_to_the_folders_project_id(tmp_path: Path):
    folder = tmp_path / "work"
    folder.mkdir()
    scope = StorageScope.for_tenant(ACME)
    narrowed = scope.for_project(folder)
    assert narrowed.project == resolve_project_id(folder)
    assert narrowed.project != ""
    assert (narrowed.org, narrowed.producer, narrowed.shared) == ("acme", "person:1", True)
    assert scope.project == ""
    assert scope.for_project(str(folder)) == narrowed


def test_an_unshared_scope_or_no_folder_is_returned_unchanged(tmp_path: Path):
    local = StorageScope.local()
    assert local.for_project(tmp_path) is local
    shared = StorageScope.for_tenant(ACME, "kept")
    assert shared.for_project(None) is shared


def test_a_folder_that_does_not_exist_still_has_a_project(tmp_path: Path):
    scope = StorageScope.for_tenant(ACME).for_project(tmp_path / "nowhere")
    assert scope.project == resolve_project_id(tmp_path / "nowhere")
    assert scope.project != ""


def test_without_a_project_clears_only_the_project():
    scope = StorageScope.for_tenant(ACME, "folder").without_project()
    assert scope == StorageScope(org="acme", producer="person:1", project="", shared=True)


def test_where_matches_a_row_of_exactly_this_org_producer_and_project(session: Session):
    rows = {
        "mine": ("acme", "person:1", "p"),
        "other project": ("acme", "person:1", "q"),
        "other producer": ("acme", "person:2", "p"),
        "other org": ("globex", "person:1", "p"),
    }
    for body, (org, producer, project) in rows.items():
        session.add(Note(body=body, org=org, producer=producer, project=project))
    session.commit()
    scope = StorageScope(org="acme", producer="person:1", project="p", shared=True)
    assert session.scalars(select(Note.body).where(scope.where(Note))).all() == ["mine"]


def test_tenant_where_ignores_the_project(session: Session):
    for body, producer, project in [
        ("a", "person:1", "p"),
        ("b", "person:1", "q"),
        ("c", "x", "p"),
    ]:
        session.add(Note(body=body, org="acme", producer=producer, project=project))
    session.commit()
    scope = StorageScope.for_tenant(ACME, "p")
    assert sorted(session.scalars(select(Note.body).where(scope.tenant_where(Note)))) == ["a", "b"]


def test_the_desktop_filter_matches_a_row_written_without_the_columns(session: Session):
    session.execute(text("INSERT INTO notes (id, body) VALUES (1, 'plain')"))
    session.commit()
    assert session.scalars(select(Note.body).where(StorageScope.local().where(Note))).all() == [
        "plain"
    ]
    note = session.get(Note, 1)
    assert note is not None
    assert (note.org, note.producer, note.project) == ("", "", "")


def test_a_row_written_through_the_orm_without_a_scope_has_empty_columns(session: Session):
    session.add(Note(id=2, body="orm"))
    session.commit()
    stored = session.execute(text("SELECT org, producer, project FROM notes")).one()
    assert tuple(stored) == ("", "", "")


# ── Tenancy ──────────────────────────────────────────────────────────


def test_nothing_is_asked_where_nothing_is_shared():
    asked: list[str] = []
    local_tenancy = Tenancy()
    local_tenancy.use(lambda: asked.append("asked") or ACME)
    assert local_tenancy.scope(shared=False) == StorageScope.local()
    assert asked == []


def test_a_shared_database_is_scoped_to_whoever_is_signed_in():
    local_tenancy = Tenancy()
    local_tenancy.use(lambda: ACME)
    assert local_tenancy.scope(shared=True) == StorageScope.for_tenant(ACME)


def test_a_shared_database_with_nobody_signed_in_is_refused():
    fresh = Tenancy()
    assert fresh.tenant() is None
    with pytest.raises(NoTenantError, match="IGNI_DATABASE_URL"):
        fresh.scope(shared=True)
    fresh.use(lambda: None)
    with pytest.raises(NoTenantError):
        fresh.scope(shared=True)


def test_clearing_the_source_returns_the_tenant_to_none():
    local_tenancy = Tenancy()
    local_tenancy.use(lambda: ACME)
    assert local_tenancy.tenant() == ACME
    local_tenancy.use(None)
    assert local_tenancy.tenant() is None
    with pytest.raises(NoTenantError):
        local_tenancy.scope(shared=True)


def test_signing_in_and_out_changes_the_answer_on_every_ask():
    signed_in: list[Tenant | None] = [ACME]
    local_tenancy = Tenancy()
    local_tenancy.use(lambda: signed_in[0])
    assert local_tenancy.scope(shared=True).producer == "person:1"
    signed_in[0] = Tenant(org="acme", producer="person:2")
    assert local_tenancy.scope(shared=True).producer == "person:2"
    signed_in[0] = None
    with pytest.raises(NoTenantError):
        local_tenancy.scope(shared=True)


def test_a_source_that_fails_fails_the_ask_and_leaves_the_tenancy_usable():
    local_tenancy = Tenancy()

    def broken() -> Tenant:
        raise RuntimeError("the sign-in service is down")

    local_tenancy.use(broken)
    with pytest.raises(RuntimeError, match="sign-in service"):
        local_tenancy.tenant()
    local_tenancy.use(lambda: ACME)
    assert local_tenancy.tenant() == ACME


def test_a_source_replaced_while_others_ask_never_breaks_an_ask():
    local_tenancy = Tenancy()
    local_tenancy.use(lambda: ACME)
    seen: list[Tenant | None] = []
    stop = threading.Event()

    def ask() -> None:
        while not stop.is_set():
            seen.append(local_tenancy.tenant())

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for _ in range(200):
        local_tenancy.use(None)
        local_tenancy.use(lambda: ACME)
    stop.set()
    for thread in threads:
        thread.join()
    assert set(seen) <= {None, ACME}


def test_the_process_has_one_tenancy_the_backend_tells():
    assert isinstance(tenancy, Tenancy)
    tenancy.use(lambda: ACME)
    assert tenancy.scope(shared=True).org == "acme"
