"""Account-based media server sign-in.

The point is that nobody should have to dig an X-Plex-Token out of an XML response, so
the tests cover the flow end to end and the two judgement calls inside it: ranking local
addresses first, and storing the server-scoped token rather than the account token.
"""

from __future__ import annotations

from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from core.models import AppSetting, ServiceInstance, ServiceKind, ServiceVariant
from core.signin import (
    DiscoveredServer,
    SignInError,
    client_identifier,
    jellyfin_signin,
    plex_servers,
    poll_plex_pin,
    save_media_server,
    start_plex_signin,
)
from core.tests.factories import make_service

PLEX_RESOURCES_PAYLOAD = [
    {
        "name": "Tower",
        "provides": "server",
        "owned": True,
        "accessToken": "server-scoped-token",
        "connections": [
            {
                "protocol": "https",
                "address": "1.2.3.4",
                "port": 32400,
                "uri": "https://relay.plex.direct:443",
                "local": False,
                "relay": True,
            },
            {
                "protocol": "http",
                "address": "192.168.1.10",
                "port": 32400,
                "uri": "http://192.168.1.10:32400",
                "local": True,
                "relay": False,
            },
            {
                "protocol": "https",
                "address": "2001:db8::1",
                "port": 32400,
                "uri": "https://[2001:db8::1]:32400",
                "local": True,
                "relay": False,
                "IPv6": True,
            },
        ],
    },
    # A player, not a server -- must be ignored.
    {"name": "Living Room TV", "provides": "player", "connections": []},
]


def mock_httpx(handler):
    """Patch httpx.Client so module-level calls route through a MockTransport."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    return mock.patch("httpx.Client", side_effect=factory)


class ClientIdentifierTests(TestCase):
    def test_is_stable_across_calls(self):
        """Plex ties the PIN and token to this; regenerating orphans the authorisation."""
        first = client_identifier()
        self.assertEqual(first, client_identifier())
        self.assertEqual(AppSetting.get("plex_client_identifier"), first)


class PlexPinTests(TestCase):
    def test_start_builds_the_auth_url(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/v2/pins")
            self.assertIn("X-Plex-Client-Identifier", request.headers)
            return httpx.Response(201, json={"id": 42, "code": "ABCD"})

        with mock_httpx(handler):
            pin = start_plex_signin()

        self.assertEqual(pin.pin_id, 42)
        self.assertIn("code=ABCD", pin.auth_url)
        self.assertIn("clientID=", pin.auth_url)
        self.assertTrue(pin.auth_url.startswith("https://app.plex.tv/auth"))

    def test_poll_returns_none_until_authorised(self):
        with mock_httpx(lambda r: httpx.Response(200, json={"authToken": None})):
            self.assertIsNone(poll_plex_pin(42))

    def test_poll_returns_the_token_once_authorised(self):
        with mock_httpx(lambda r: httpx.Response(200, json={"authToken": "acct-token"})):
            self.assertEqual(poll_plex_pin(42), "acct-token")

    def test_expired_pin_is_explained(self):
        with mock_httpx(lambda r: httpx.Response(404, json={})):
            with self.assertRaises(SignInError) as caught:
                poll_plex_pin(42)
        self.assertIn("expired", str(caught.exception))

    def test_no_internet_says_so(self):
        """Everything else Sleutharr does is LAN-only, so this failure needs explaining."""
        def handler(request):
            raise httpx.ConnectError("no route to host")

        with mock_httpx(handler):
            with self.assertRaises(SignInError) as caught:
                start_plex_signin()
        self.assertIn("outbound internet", str(caught.exception))


class PlexServerListTests(TestCase):
    def _servers(self):
        with mock_httpx(lambda r: httpx.Response(200, json=PLEX_RESOURCES_PAYLOAD)):
            return plex_servers("acct-token")

    def test_only_servers_are_listed(self):
        servers = self._servers()
        self.assertEqual([s.name for s in servers], ["Tower"])

    def test_local_address_is_preferred_over_relay(self):
        """Sleutharr and the server are nearly always on the same LAN."""
        servers = self._servers()
        self.assertEqual(servers[0].base_url, "http://192.168.1.10:32400")
        self.assertIn("https://relay.plex.direct:443", servers[0].alternatives)

    def test_ipv4_local_beats_ipv6_local(self):
        servers = self._servers()
        self.assertNotIn("2001:db8", servers[0].base_url)

    def test_stores_the_server_scoped_token_not_the_account_token(self):
        """Smaller blast radius: it only works against that one server."""
        servers = self._servers()
        self.assertEqual(servers[0].token, "server-scoped-token")
        self.assertNotEqual(servers[0].token, "acct-token")


class JellyfinSignInTests(TestCase):
    def test_exchanges_credentials_for_a_token(self):
        def handler(request):
            self.assertEqual(request.url.path, "/Users/AuthenticateByName")
            self.assertIn("MediaBrowser", request.headers.get("Authorization", ""))
            return httpx.Response(
                200, json={"AccessToken": "jf-token", "ServerName": "media"}
            )

        with mock_httpx(handler):
            server = jellyfin_signin(
                "192.168.1.10:8096", "dave", "hunter2", ServiceVariant.JELLYFIN
            )
        self.assertEqual(server.token, "jf-token")
        self.assertEqual(server.base_url, "http://192.168.1.10:8096")

    def test_bad_password_is_reported_clearly(self):
        with mock_httpx(lambda r: httpx.Response(401, json={})):
            with self.assertRaises(SignInError) as caught:
                jellyfin_signin("h:8096", "dave", "wrong", ServiceVariant.JELLYFIN)
        self.assertIn("rejected", str(caught.exception))

    def test_emby_is_named_in_its_own_errors(self):
        with mock_httpx(lambda r: httpx.Response(401, json={})):
            with self.assertRaises(SignInError) as caught:
                jellyfin_signin("h:8096", "dave", "x", ServiceVariant.EMBY)
        self.assertIn("Emby", str(caught.exception))

    def test_missing_username_is_caught_before_any_request(self):
        with self.assertRaises(SignInError):
            jellyfin_signin("h:8096", "", "x", ServiceVariant.JELLYFIN)


class SaveTests(TestCase):
    def test_creates_the_service(self):
        service = save_media_server(
            DiscoveredServer(name="Tower", base_url="http://192.168.1.10:32400", token="t"),
            ServiceVariant.PLEX,
        )
        self.assertEqual(service.kind, ServiceKind.MEDIA_SERVER)
        self.assertEqual(service.api_key, "t")
        self.assertEqual(ServiceInstance.objects.count(), 1)

    def test_signing_in_again_replaces_the_token_and_clears_backoff(self):
        existing = make_service(
            ServiceKind.MEDIA_SERVER,
            name="Plex",
            variant=ServiceVariant.PLEX,
            base_url="http://192.168.1.10:32400",
        )
        existing.api_key = "stale"
        existing.consecutive_failures = 5
        existing.save()

        save_media_server(
            DiscoveredServer(name="Tower", base_url="http://192.168.1.10:32400/", token="fresh"),
            ServiceVariant.PLEX,
        )
        existing.refresh_from_db()
        self.assertEqual(existing.api_key, "fresh")
        # A new token is reason to retry immediately, not to keep sulking.
        self.assertEqual(existing.consecutive_failures, 0)
        self.assertEqual(ServiceInstance.objects.count(), 1)


class SignInViewTests(TestCase):
    def test_endpoints_are_post_only(self):
        for name in ("plex_start", "plex_poll", "media_server_save"):
            with self.subTest(view=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 405)

    def test_poll_without_a_pin_is_handled(self):
        response = self.client.post(reverse("plex_poll"), {})
        self.assertFalse(response.json()["ok"])

    def test_save_without_a_server_is_handled(self):
        response = self.client.post(
            reverse("media_server_save"), {"variant": ServiceVariant.PLEX}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick a server first")

    def test_plex_start_surfaces_network_failure(self):
        def handler(request):
            raise httpx.ConnectError("nope")

        with mock_httpx(handler):
            response = self.client.post(reverse("plex_start"))
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("internet", payload["detail"])
