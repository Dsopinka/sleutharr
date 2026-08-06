"""Discovery tests.

The point of discovery is to supply the two fields nobody can reasonably work out by
hand -- the request manager's serviceId, and the name an *arr calls a download client --
so those get the most cover here. Getting either wrong is silent: no error, just a
confidently wrong diagnosis later.
"""

from __future__ import annotations

from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse

from core.discovery import (
    Candidate,
    apply_candidate,
    discover_from,
    identify,
)
from core.models import ServiceInstance, ServiceKind, ServiceVariant
from core.tests.factories import make_service
from core.tests.test_ingest import mock_client

# A Seerr settings/radarr payload: two instances, one HD one 4K.
SEERR_RADARR = [
    {
        "id": 0,
        "name": "Radarr",
        "hostname": "192.168.1.10",
        "port": 7878,
        "apiKey": "radarr-key",
        "useSsl": False,
        "is4k": False,
        "activeDirectory": "/data/media/movies",
    },
    {
        "id": 1,
        "name": "Radarr 4K",
        "hostname": "192.168.1.10",
        "port": 7879,
        "apiKey": "radarr4k-key",
        "useSsl": False,
        "is4k": True,
        "activeDirectory": "/data/media/movies-4k",
    },
]

SEERR_SONARR = [
    {
        "id": 0,
        "name": "Sonarr",
        "hostname": "192.168.1.10",
        "port": 8989,
        "apiKey": "sonarr-key",
        "useSsl": False,
        "is4k": False,
    }
]

ARR_DOWNLOAD_CLIENTS = [
    {
        "id": 1,
        "name": "SABnzbd",
        "implementation": "Sabnzbd",
        "enable": True,
        "protocol": "usenet",
        "fields": [
            {"name": "host", "value": "192.168.1.10"},
            {"name": "port", "value": 8080},
            {"name": "apiKey", "value": "sab-key"},
            {"name": "useSsl", "value": False},
        ],
    },
    {
        "id": 2,
        "name": "qBit",
        "implementation": "QBittorrent",
        "enable": True,
        "protocol": "torrent",
        "fields": [
            {"name": "host", "value": "192.168.1.10"},
            {"name": "port", "value": 8081},
            {"name": "username", "value": "admin"},
        ],
    },
    {
        "id": 3,
        "name": "Old rTorrent",
        "implementation": "RTorrent",
        "enable": True,
        "fields": [{"name": "host", "value": "192.168.1.10"}],
    },
    {
        "id": 4,
        "name": "Disabled one",
        "implementation": "Sabnzbd",
        "enable": False,
        "fields": [{"name": "host", "value": "192.168.1.99"}],
    },
]

ARR_ROOT_FOLDERS = [{"id": 1, "path": "/data/media/movies", "accessible": True}]


class DiscoverFromSeerrTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Seerr",
            variant=ServiceVariant.SEERR,
            base_url="http://192.168.1.10:5055",
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/settings/radarr"):
            return httpx.Response(200, json=SEERR_RADARR)
        if request.url.path.endswith("/settings/sonarr"):
            return httpx.Response(200, json=SEERR_SONARR)
        return httpx.Response(404, json={})

    def run_discovery(self):
        from core.clients.requestmanager import SeerrClient

        client = SeerrClient(self.seerr)
        with mock_client(client, self.handler), mock.patch(
            "core.discovery.request_manager_client", return_value=client
        ):
            return discover_from(self.seerr)

    def test_finds_every_arr(self):
        result = self.run_discovery()
        names = sorted(c.name for c in result.candidates)
        self.assertEqual(names, ["Radarr", "Radarr 4K", "Sonarr"])

    def test_carries_the_service_id_and_4k_lane(self):
        """These two are the whole reason discovery exists.

        Nobody can guess a serviceId, and getting it wrong routes a request to the wrong
        instance without any error appearing anywhere.
        """
        result = self.run_discovery()
        by_name = {c.name: c for c in result.candidates}

        self.assertEqual(by_name["Radarr"].remote_service_id, 0)
        self.assertFalse(by_name["Radarr"].is_4k)

        self.assertEqual(by_name["Radarr 4K"].remote_service_id, 1)
        self.assertTrue(by_name["Radarr 4K"].is_4k)

    def test_carries_credentials_and_correct_kind(self):
        result = self.run_discovery()
        by_name = {c.name: c for c in result.candidates}
        self.assertEqual(by_name["Radarr"].api_key, "radarr-key")
        self.assertEqual(by_name["Radarr"].kind, ServiceKind.RADARR)
        self.assertEqual(by_name["Sonarr"].kind, ServiceKind.SONARR)
        self.assertEqual(by_name["Radarr"].base_url, "http://192.168.1.10:7878")

    def test_already_configured_services_are_flagged_not_duplicated(self):
        make_service(
            ServiceKind.RADARR, name="Existing", base_url="http://192.168.1.10:7878"
        )
        result = self.run_discovery()
        by_name = {c.name: c for c in result.candidates}
        self.assertFalse(by_name["Radarr"].is_new)
        self.assertTrue(by_name["Radarr 4K"].is_new)

    def test_url_matching_ignores_scheme_and_trailing_slash(self):
        make_service(
            ServiceKind.RADARR, name="Existing", base_url="http://192.168.1.10:7878/"
        )
        result = self.run_discovery()
        self.assertFalse({c.name: c for c in result.candidates}["Radarr"].is_new)

    def test_ombi_says_why_it_cannot_help(self):
        ombi = make_service(
            ServiceKind.REQUEST_MANAGER, name="Ombi", variant=ServiceVariant.OMBI
        )
        result = discover_from(ombi)
        self.assertEqual(result.candidates, [])
        self.assertTrue(any("Ombi" in e for e in result.errors))


class DiscoverFromArrTests(TestCase):
    def setUp(self):
        self.radarr = make_service(
            ServiceKind.RADARR, name="Radarr", base_url="http://192.168.1.10:7878"
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/downloadclient"):
            return httpx.Response(200, json=ARR_DOWNLOAD_CLIENTS)
        if request.url.path.endswith("/rootfolder"):
            return httpx.Response(200, json=ARR_ROOT_FOLDERS)
        return httpx.Response(404, json={})

    def run_discovery(self):
        from core.clients.arr import RadarrClient

        client = RadarrClient(self.radarr)
        with mock_client(client, self.handler), mock.patch(
            "core.discovery.arr_client", return_value=client
        ):
            return discover_from(self.radarr)

    def test_finds_supported_clients_only(self):
        result = self.run_discovery()
        names = sorted(c.name for c in result.candidates)
        # rTorrent is unsupported and the disabled one is skipped.
        self.assertEqual(names, ["SABnzbd", "qBit"])

    def test_captures_the_name_the_arr_uses(self):
        """This is what queue rows are matched on when two usenet clients exist."""
        result = self.run_discovery()
        sab = next(c for c in result.candidates if c.name == "SABnzbd")
        self.assertEqual(sab.arr_client_name, "SABnzbd")
        self.assertEqual(sab.variant, ServiceVariant.SABNZBD)
        self.assertEqual(sab.api_key, "sab-key")
        self.assertEqual(sab.base_url, "http://192.168.1.10:8080")

    def test_torrent_client_notes_the_missing_password(self):
        result = self.run_discovery()
        qbit = next(c for c in result.candidates if c.name == "qBit")
        self.assertEqual(qbit.username, "admin")
        self.assertIn("Password", qbit.note)

    def test_collects_root_folders(self):
        result = self.run_discovery()
        self.assertEqual(result.root_folders["Radarr"], ["/data/media/movies"])


class ApplyTests(TestCase):
    def test_creates_a_new_service(self):
        candidate = Candidate(
            kind=ServiceKind.RADARR,
            variant=ServiceVariant.NATIVE,
            name="Radarr 4K",
            base_url="http://192.168.1.10:7879",
            api_key="k",
            remote_service_id=1,
            is_4k=True,
        )
        service = apply_candidate(candidate)
        self.assertEqual(service.remote_service_id, 1)
        self.assertTrue(service.is_4k)
        self.assertEqual(ServiceInstance.objects.count(), 1)

    def test_existing_service_keeps_its_credentials(self):
        """Re-running discovery must never clobber a working key with a stale one."""
        existing = make_service(
            ServiceKind.RADARR, name="Mine", base_url="http://192.168.1.10:7878"
        )
        existing.api_key = "the-good-key"
        existing.save()

        apply_candidate(
            Candidate(
                kind=ServiceKind.RADARR,
                variant=ServiceVariant.NATIVE,
                name="Radarr",
                base_url="http://192.168.1.10:7878",
                api_key="a-different-key",
                remote_service_id=0,
                existing_id=existing.pk,
            )
        )
        existing.refresh_from_db()
        self.assertEqual(existing.api_key, "the-good-key")
        self.assertEqual(existing.name, "Mine")
        # But the routing field it could not have known is filled in.
        self.assertEqual(existing.remote_service_id, 0)

    def test_existing_service_gains_the_client_name(self):
        existing = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="SAB",
            variant=ServiceVariant.SABNZBD,
            base_url="http://192.168.1.10:8080",
        )
        apply_candidate(
            Candidate(
                kind=ServiceKind.DOWNLOAD_CLIENT,
                variant=ServiceVariant.SABNZBD,
                name="SABnzbd",
                base_url="http://192.168.1.10:8080",
                arr_client_name="SABnzbd",
                existing_id=existing.pk,
            )
        )
        existing.refresh_from_db()
        self.assertEqual(existing.arr_client_name, "SABnzbd")


class IdentifyTests(TestCase):
    """Identification is what removes the two dropdowns from the add form."""

    def _identify_with(self, routes: dict):
        def handler(request: httpx.Request) -> httpx.Response:
            # Query strings are matched separately; SABnzbd's probe is /api?mode=version
            # so the path alone is the right key.
            payload = routes.get(request.url.path)
            if payload is None:
                return httpx.Response(404, text="")
            if isinstance(payload, str):
                return httpx.Response(200, text=payload)
            return httpx.Response(200, json=payload)

        transport = httpx.MockTransport(handler)
        real_client = httpx.Client

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        with mock.patch("httpx.Client", side_effect=fake_client):
            return identify("http://192.168.1.10:7878", api_key="k")

    def test_tells_sonarr_from_radarr_by_app_name(self):
        """They share the endpoint, so the port is no help -- people remap those."""
        result = self._identify_with(
            {"/api/v3/system/status": {"appName": "Sonarr", "version": "4.0.19"}}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], ServiceKind.SONARR)
        self.assertEqual(result["version"], "4.0.19")

        result = self._identify_with(
            {"/api/v3/system/status": {"appName": "Radarr", "version": "6.3.0"}}
        )
        self.assertEqual(result["kind"], ServiceKind.RADARR)

    def test_identifies_seerr(self):
        result = self._identify_with({"/api/v1/status": {"version": "3.4.1"}})
        self.assertEqual(result["kind"], ServiceKind.REQUEST_MANAGER)
        self.assertEqual(result["variant"], ServiceVariant.SEERR)

    def test_identifies_plex(self):
        result = self._identify_with(
            {"/identity": {"MediaContainer": {"version": "1.41.3"}}}
        )
        self.assertEqual(result["kind"], ServiceKind.MEDIA_SERVER)
        self.assertEqual(result["variant"], ServiceVariant.PLEX)

    def test_tells_emby_from_jellyfin(self):
        result = self._identify_with(
            {"/System/Info/Public": {"Version": "4.8", "ProductName": "Emby Server"}}
        )
        self.assertEqual(result["variant"], ServiceVariant.EMBY)

        result = self._identify_with(
            {"/System/Info/Public": {"Version": "10.10.3", "ServerName": "media"}}
        )
        self.assertEqual(result["variant"], ServiceVariant.JELLYFIN)

    def test_identifies_sabnzbd(self):
        result = self._identify_with({"/api": {"version": "4.4.1"}})
        self.assertEqual(result["kind"], ServiceKind.DOWNLOAD_CLIENT)
        self.assertEqual(result["variant"], ServiceVariant.SABNZBD)

    def test_unrecognised_says_so_helpfully(self):
        result = self._identify_with({})
        self.assertFalse(result["ok"])
        self.assertIn("API key", result["detail"])

    def test_empty_url_is_handled(self):
        self.assertFalse(identify("")["ok"])


class DiscoveryViewTests(TestCase):
    def test_discover_is_post_only(self):
        self.assertEqual(self.client.get(reverse("discover")).status_code, 405)
        self.assertEqual(self.client.get(reverse("discover_apply")).status_code, 405)

    def test_discover_with_no_services_renders(self):
        response = self.client.post(reverse("discover"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing new found")

    def test_apply_with_nothing_selected(self):
        response = self.client.post(reverse("discover_apply"), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing was selected")

    def test_identify_endpoint_returns_json(self):
        response = self.client.post(
            reverse("identify_service"), {"base_url": "http://127.0.0.1:1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
