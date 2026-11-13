"""What ``igni_core.core.auth`` offers, and the page a browser sign-in ends on."""

from __future__ import annotations

import importlib
from html.parser import HTMLParser

import pytest

import igni_core.core.auth as auth
from igni_core.core.auth._success_page import SUCCESS_PAGE

SURFACE = {
    "CallbackServer": "igni_core.core.auth.callback_server",
    "CloudCredentials": "igni_core.core.auth.credentials",
    "Credentials": "igni_core.core.auth.credentials",
    "CredentialsStore": "igni_core.core.auth.credentials",
    "EmptyCloudCredentials": "igni_core.core.auth.credentials",
    "JwtClaims": "igni_core.core.auth.schemas",
    "LoadCredentialsResult": "igni_core.core.auth.schemas",
    "LoginResult": "igni_core.core.auth.schemas",
    "PortalClient": "igni_core.core.auth.portal_client",
    "UserInfo": "igni_core.core.auth.schemas",
    "ValidateResult": "igni_core.core.auth.schemas",
}


def test_the_package_offers_exactly_these_names():
    assert sorted(auth.__all__) == sorted(SURFACE)


@pytest.mark.parametrize("name,module", sorted(SURFACE.items()))
def test_each_name_is_the_class_its_module_defines(name, module):
    assert getattr(auth, name) is getattr(importlib.import_module(module), name)


def test_the_credentials_result_resolves_its_forward_reference():
    result = auth.LoadCredentialsResult(ok=True, creds=auth.Credentials(access_token="t"))
    assert result.creds is not None and result.creds.access_token == "t"


class _Outline(HTMLParser):
    VOID = {"meta", "path", "link", "br", "img"}

    def __init__(self) -> None:
        super().__init__()
        self.open: list[str] = []
        self.unbalanced = False
        self.text: dict[str, str] = {}
        self.attrs: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag, attrs):
        self.attrs.extend(attrs)
        if tag not in self.VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if not self.open or self.open.pop() != tag:
            self.unbalanced = True

    def handle_data(self, data):
        if self.open and data.strip():
            self.text[self.open[-1]] = self.text.get(self.open[-1], "") + data.strip()


class TestTheSuccessPage:
    def parsed(self) -> _Outline:
        outline = _Outline()
        outline.feed(SUCCESS_PAGE)
        outline.close()
        return outline

    def test_it_is_a_whole_html_document_with_balanced_tags(self):
        outline = self.parsed()
        assert SUCCESS_PAGE.startswith("<!DOCTYPE html>")
        assert SUCCESS_PAGE.rstrip().endswith("</html>")
        assert not outline.unbalanced and outline.open == []

    def test_it_says_the_cli_is_authenticated_and_the_window_may_close(self):
        text = self.parsed().text
        assert text["title"] == "CLI Authenticated"
        assert text["h2"] == "CLI Authenticated"
        assert "Return to your terminal" in text["p"]
        assert "You can close this window" in text["div"]

    def test_it_is_utf8_and_declares_it(self):
        assert '<meta charset="UTF-8">' in SUCCESS_PAGE
        assert SUCCESS_PAGE.encode("utf-8").decode("utf-8") == SUCCESS_PAGE

    def test_it_fetches_nothing_and_carries_no_credential(self):
        attrs = dict(self.parsed().attrs)
        assert "src" not in attrs and "href" not in attrs
        assert "http://" not in SUCCESS_PAGE and "https://" not in SUCCESS_PAGE
        for word in ("token", "Bearer", "code="):
            assert word not in SUCCESS_PAGE
