"""Who a call is for: a person, an app, or a space, as the signed-in token names it.

``SignedInPerson`` reads the credentials on every call. A person's token
gives a person (their group from the last listing); an app's or a
space's token gives that kind of principal, with no group and, for a
space, the app and the app's own name for the end user. A token that
says nothing, or says something igni does not know, is a person's.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.db.scope import Tenant
from igni_core.extensions.principal import Principal, PrincipalKind, SignedInPerson
from igni_core.extensions.store import ExtensionStore


def _jwt(claims: dict[str, Any]) -> str:
    def part(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

    return f"{part(b'{}')}.{part(json.dumps(claims).encode())}.sig"


def _person(tmp_path: Path, **claims: Any) -> SignedInPerson:
    path = tmp_path / "credentials.json"
    CredentialsStore(str(path)).save(Credentials.new(_jwt(claims), "who@acme.test"))
    return SignedInPerson(str(path), ExtensionStore(tmp_path / "store"))


def test_an_app_s_token_names_an_app_with_no_group(tmp_path: Path) -> None:
    person = _person(tmp_path, sub="app-1", kind="app", org="org-acme", group="g-legal")

    principal = person()

    assert principal == Principal(kind=PrincipalKind.APP, id="app-1", org="org-acme")
    assert principal is not None and principal.ref == "app:app-1"
    # A group on an app's token is not the app's: the listing's group does not reach it either.
    assert principal.group is None


def test_a_space_s_token_names_its_app_and_the_apps_name_for_the_end_user(
    tmp_path: Path,
) -> None:
    person = _person(
        tmp_path, sub="sp-1", kind="space", org="org-acme", app="app-1", external_id="cust-42"
    )
    store_group = ExtensionStore(tmp_path / "store")
    store_group.listing.record_group("g-finance")

    principal = person()

    assert principal == Principal(
        kind=PrincipalKind.SPACE,
        id="sp-1",
        org="org-acme",
        app="app-1",
        external_id="cust-42",
    )
    assert principal is not None and principal.ref == "space:sp-1"
    assert person.meta() == {
        "igni/principal": {
            "kind": "space",
            "id": "sp-1",
            "org": "org-acme",
            "group": None,
            "session": None,
            "app": "app-1",
            "external_id": "cust-42",
        }
    }


def test_only_a_space_carries_the_app_fields_on_the_wire() -> None:
    app = Principal(kind=PrincipalKind.APP, id="app-1", org="org-acme")

    meta = app.as_meta()["igni/principal"]

    assert "app" not in meta and "external_id" not in meta
    assert meta["kind"] == "app"


def test_a_token_that_says_nothing_about_its_kind_is_a_person(tmp_path: Path) -> None:
    principal = _person(tmp_path, sub="u-ada", org="org-acme")()

    assert principal is not None
    assert principal.kind is PrincipalKind.PERSON and principal.ref == "person:u-ada"


def test_a_kind_igni_does_not_know_is_a_person(tmp_path: Path) -> None:
    principal = _person(tmp_path, sub="u-ada", kind="robot", org="org-acme")()

    assert principal is not None and principal.kind is PrincipalKind.PERSON


def test_a_person_with_a_token_group_and_no_listing_has_the_token_group(tmp_path: Path) -> None:
    principal = _person(tmp_path, sub="u-ada", kind="person", group="g-legal")()

    assert principal is not None and principal.group == "g-legal"


def test_a_session_with_no_user_id_is_nobody(tmp_path: Path) -> None:
    person = _person(tmp_path, org="org-acme")

    assert person() is None
    assert person.meta() is None
    assert person.tenant() is None


def test_the_tenant_is_the_org_and_the_principal_s_ref(tmp_path: Path) -> None:
    assert _person(tmp_path, sub="u-ada", org="org-acme").tenant() == Tenant(
        org="org-acme", producer="person:u-ada"
    )


def test_a_sign_in_that_names_no_org_has_no_tenant(tmp_path: Path) -> None:
    person = _person(tmp_path, sub="u-ada")

    assert person() is not None
    assert person.tenant() is None


def test_signing_out_between_calls_leaves_no_principal_behind(tmp_path: Path) -> None:
    person = _person(tmp_path, sub="u-ada", org="org-acme")
    assert person() is not None

    (tmp_path / "credentials.json").unlink()

    assert person() is None
    assert person.meta() is None
    assert person.tenant() is None
