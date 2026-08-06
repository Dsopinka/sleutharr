"""Sign in to a media server with an account instead of hunting for a token.

Finding a Plex token by hand means opening a library item, choosing "Get Info", viewing
the XML and pulling `X-Plex-Token` out of the URL. Nobody should have to do that. Plex
publishes a PIN-based flow for exactly this -- the same one Sonarr, Overseerr and
Tautulli use -- so we use it: click a button, authorise in Plex's own page, come back to
a list of your servers and pick one.

Jellyfin and Emby have no such flow but do accept a username and password, which is far
easier than telling someone to create an API key in a dashboard they have never opened.

Two things worth knowing about the Plex path:

* it needs outbound internet to plex.tv. Everything else Sleutharr does is LAN-only, so
  this is the one feature that stops working on an air-gapped box, and the UI says so.
* the token we store is the **server's** access token from the resources list, not the
  account token. It is scoped to that one server, so it is the smaller thing to hold.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field

import httpx

from core.models import AppSetting, ServiceKind, ServiceVariant

logger = logging.getLogger(__name__)

PLEX_PINS = "https://plex.tv/api/v2/pins"
PLEX_RESOURCES = "https://plex.tv/api/v2/resources"
PLEX_AUTH_PAGE = "https://app.plex.tv/auth#?"

PRODUCT = "Sleutharr"
CLIENT_ID_SETTING = "plex_client_identifier"

TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class SignInError(Exception):
    """Something went wrong linking an account. Message is fit for the UI."""


def client_identifier() -> str:
    """Stable per-install id.

    Plex ties the PIN, the resulting token and the device list to this, so it has to
    survive restarts -- regenerating it would orphan the authorisation.
    """
    existing = AppSetting.get(CLIENT_ID_SETTING, "")
    if existing:
        return str(existing)
    generated = secrets.token_hex(16)
    AppSetting.set(CLIENT_ID_SETTING, generated)
    return generated


def _plex_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Product": PRODUCT,
        "X-Plex-Version": "2.2.0",
        "X-Plex-Client-Identifier": client_identifier(),
        "X-Plex-Device": "Sleutharr",
        "X-Plex-Platform": "Web",
    }


# ------------------------------------------------------------------------ Plex PIN


@dataclass
class PlexPin:
    pin_id: int
    code: str
    auth_url: str


def start_plex_signin() -> PlexPin:
    """Create a PIN and build the URL the user authorises at."""
    from urllib.parse import urlencode

    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            response = http.post(
                PLEX_PINS, headers=_plex_headers(), data={"strong": "true"}
            )
    except httpx.HTTPError as exc:
        raise SignInError(
            "Could not reach plex.tv. Signing in with Plex needs outbound internet "
            f"access from the Sleutharr container. ({exc})"
        ) from exc

    if response.status_code >= 400:
        raise SignInError(f"plex.tv refused to start sign-in (HTTP {response.status_code}).")

    try:
        data = response.json()
        pin_id = int(data["id"])
        code = str(data["code"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SignInError("plex.tv returned an unexpected response.") from exc

    query = urlencode(
        {
            "clientID": client_identifier(),
            "code": code,
            "context[device][product]": PRODUCT,
        }
    )
    return PlexPin(pin_id=pin_id, code=code, auth_url=PLEX_AUTH_PAGE + query)


def poll_plex_pin(pin_id: int) -> str | None:
    """Return the account token once the user has authorised, else None."""
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            response = http.get(f"{PLEX_PINS}/{pin_id}", headers=_plex_headers())
    except httpx.HTTPError as exc:
        raise SignInError(f"Could not reach plex.tv: {exc}") from exc

    if response.status_code == 404:
        raise SignInError("That sign-in expired. Start again.")
    if response.status_code >= 400:
        raise SignInError(f"plex.tv returned HTTP {response.status_code}.")

    try:
        token = (response.json() or {}).get("authToken")
    except ValueError:
        return None
    return str(token) if token else None


# -------------------------------------------------------------------- Plex servers


@dataclass
class DiscoveredServer:
    name: str
    base_url: str
    token: str
    #: Every usable address, best first, so the UI can offer alternatives.
    alternatives: list[str] = field(default_factory=list)
    owned: bool = True
    note: str = ""


def plex_servers(account_token: str) -> list[DiscoveredServer]:
    """List the Plex servers this account can reach.

    Connections are ranked local-first: Sleutharr and the server are almost always on
    the same LAN, and a local address is faster and keeps traffic off plex.tv's relay.
    """
    headers = {**_plex_headers(), "X-Plex-Token": account_token}
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            response = http.get(
                PLEX_RESOURCES,
                headers=headers,
                params={"includeHttps": 1, "includeRelay": 1},
            )
    except httpx.HTTPError as exc:
        raise SignInError(f"Could not list your Plex servers: {exc}") from exc

    if response.status_code >= 400:
        raise SignInError(f"plex.tv returned HTTP {response.status_code} listing servers.")

    try:
        rows = response.json()
    except ValueError as exc:
        raise SignInError("plex.tv returned an unexpected server list.") from exc

    servers: list[DiscoveredServer] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if "server" not in str(row.get("provides") or ""):
            continue

        connections = [c for c in (row.get("connections") or []) if isinstance(c, dict)]
        ranked = sorted(
            connections,
            key=lambda c: (
                not c.get("local", False),   # local first
                bool(c.get("relay", False)),  # relay last
                bool(c.get("IPv6", False)),   # prefer v4, more likely routable here
            ),
        )
        uris = [str(c.get("uri")) for c in ranked if c.get("uri")]
        if not uris:
            continue

        servers.append(
            DiscoveredServer(
                name=str(row.get("name") or "Plex Media Server"),
                base_url=uris[0],
                # The server-scoped token, not the account token.
                token=str(row.get("accessToken") or account_token),
                alternatives=uris[1:4],
                owned=bool(row.get("owned", True)),
                note="" if row.get("owned", True) else "Shared with you by someone else.",
            )
        )
    return servers


# ---------------------------------------------------------------- Jellyfin / Emby


def jellyfin_signin(
    base_url: str, username: str, password: str, variant: str
) -> DiscoveredServer:
    """Exchange a username and password for an API token."""
    base = (base_url or "").strip().rstrip("/")
    if "://" not in base:
        base = f"http://{base}"
    if not username:
        raise SignInError("Enter your username.")

    product = "Emby" if variant == ServiceVariant.EMBY else "Jellyfin"
    # Both products require this header shape to identify the client.
    auth_header = (
        f'MediaBrowser Client="{PRODUCT}", Device="Sleutharr", '
        f'DeviceId="{client_identifier()}", Version="2.2.0"'
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": auth_header,
        "X-Emby-Authorization": auth_header,
    }

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as http:
            response = http.post(
                f"{base}/Users/AuthenticateByName",
                headers=headers,
                json={"Username": username, "Pw": password},
            )
    except httpx.HTTPError as exc:
        raise SignInError(f"Could not reach {product} at {base}. ({exc})") from exc

    if response.status_code in (401, 403):
        raise SignInError(f"{product} rejected that username or password.")
    if response.status_code >= 400:
        raise SignInError(f"{product} returned HTTP {response.status_code}.")

    try:
        data = response.json() or {}
    except ValueError as exc:
        raise SignInError(f"{product} returned an unexpected response.") from exc

    token = data.get("AccessToken")
    if not token:
        raise SignInError(f"{product} did not return an access token.")

    server_name = ((data.get("SessionInfo") or {}).get("ServerId") or "")
    return DiscoveredServer(
        name=str(data.get("ServerName") or product),
        base_url=base,
        token=str(token),
        note=f"Signed in as {username}." + (f" Server {server_name}" if server_name else ""),
    )


# --------------------------------------------------------------------- persistence


def save_media_server(
    server: DiscoveredServer, variant: str, name: str = ""
) -> "ServiceInstance":
    """Create or update the media server entry from a completed sign-in."""
    from core.models import ServiceInstance

    from core.discovery import _normalise

    existing = None
    for candidate in ServiceInstance.objects.filter(kind=ServiceKind.MEDIA_SERVER):
        if _normalise(candidate.base_url) == _normalise(server.base_url):
            existing = candidate
            break

    if existing:
        existing.api_key = server.token
        existing.variant = variant
        existing.enabled = True
        # A fresh token means whatever was failing before is worth retrying now.
        existing.consecutive_failures = 0
        existing.backoff_until = None
        existing.save()
        return existing

    return ServiceInstance.objects.create(
        kind=ServiceKind.MEDIA_SERVER,
        variant=variant,
        name=(name or server.name)[:100],
        base_url=server.base_url,
        api_key=server.token,
        enabled=True,
    )
