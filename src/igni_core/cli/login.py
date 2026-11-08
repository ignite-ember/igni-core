"""``igni login`` — sign in to igni server from a terminal.

The app signs in with ``/login``; a terminal had nothing, so ``igni ext
sync`` and ``igni -m`` runs could only use a sign-in the app had made.

* ``igni login`` runs the same flow as ``/login`` —
  :class:`~igni_core.backend.browser_sign_in.BrowserSignIn`, the
  portal's ``/cli-auth`` page and a callback on this machine — and
  prints the address to open, for a machine with no browser of its own.
  There is no session to hand the credentials to; the next one reads
  them, and the group pack, off disk.
* ``igni login --token-stdin`` keeps an access token handed to it on
  stdin, once igni server has accepted it (``/v1/portal/me``): for a
  script or CI job that already holds one. Never as an argument, which
  would leave it in the shell's history and the process list.
"""

from __future__ import annotations

import asyncio
import sys

import click

from igni_core.backend.browser_sign_in import BrowserSignIn
from igni_core.backend.group_pack_hydrator import GroupPackHydrator
from igni_core.core.auth.credentials import Credentials, CredentialsStore
from igni_core.core.auth.portal_client import PortalClient
from igni_core.core.config.endpoint import ApiUrlNotConfigured, require_api_url
from igni_core.core.config.settings_loader import SettingsLoader


@click.command("login")
@click.option(
    "--token-stdin",
    is_flag=True,
    help="Read an access token from stdin and keep it, once igni server accepts it.",
)
@click.option(
    "--timeout",
    type=float,
    default=300.0,
    show_default=True,
    help="Seconds to wait for the browser sign-in.",
)
def login(token_stdin: bool, timeout: float) -> None:
    """Sign in to igni server.

    Opens the portal's sign-in page and waits for it to call back to
    this machine; the address is printed too, for a machine with no
    browser. With --token-stdin, keeps an access token read from stdin
    instead (e.g. `printf %s "$TOKEN" | igni login --token-stdin`).
    """
    settings = SettingsLoader.load()
    try:
        api_url = require_api_url(settings.api_url, operation="Signing in")
    except ApiUrlNotConfigured as exc:
        raise click.ClickException(str(exc)) from exc
    portal = PortalClient(api_url=api_url, portal_url=settings.portal_url or None)
    store = CredentialsStore(settings.auth.credentials_file)

    if token_stdin:
        token = sys.stdin.read().strip()
        if not token:
            raise click.ClickException("no token on stdin")
        email = asyncio.run(_keep(portal, store, token))
    else:
        sign_in = BrowserSignIn(portal, store, GroupPackHydrator(settings, portal))
        try:
            result = asyncio.run(asyncio.wait_for(sign_in.login(on_status=click.echo), timeout))
        except TimeoutError as exc:
            raise click.ClickException(f"nobody signed in within {timeout:g}s") from exc
        if not result.ok:
            raise click.ClickException(f"signing in failed: {result.error}")
        email = result.email or ""
    click.echo(f"signed in to {api_url} as {email or 'an unnamed account'}")


async def _keep(portal: PortalClient, store: CredentialsStore, token: str) -> str:
    """Keep ``token`` once igni server accepts it; the account's email."""
    checked = await portal.validate_token(token)
    if not checked.ok:
        why = f"HTTP {checked.status_code}" if checked.status_code else (checked.error or "")
        raise click.ClickException(
            f"igni server did not accept the token ({why or checked.reason})"
        )
    email = checked.user.email if checked.user else ""
    store.save(Credentials.for_token(token, email))
    return email


__all__ = ["login"]
