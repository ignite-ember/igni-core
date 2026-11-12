"""``BrowserOpener``: best effort, never raises."""

from __future__ import annotations

import logging
import webbrowser

from igni_core.backend.browser_opener import BrowserOpener


def test_the_url_reaches_the_browser_unchanged(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    BrowserOpener.open("https://example.test/a?b=ü")

    assert opened == ["https://example.test/a?b=ü"]


def test_a_browser_that_raises_is_logged_at_info_and_swallowed(monkeypatch, caplog):
    def _boom(url):
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "open", _boom)

    with caplog.at_level(logging.INFO):
        BrowserOpener.open("https://example.test")

    [record] = [r for r in caplog.records if "could not open browser" in r.message]
    assert record.levelno == logging.INFO
    assert "https://example.test" in record.message
    assert "no runnable browser" in record.message


def test_no_browser_available_is_ignored(monkeypatch):
    monkeypatch.setattr(webbrowser, "open", lambda url: False)

    assert BrowserOpener.open("") is None
