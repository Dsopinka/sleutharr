"""End-to-end ingestion against recorded fixtures.

Uses httpx's built-in MockTransport, so no live calls and no extra dependency. This is
the test that would catch a regression in the join chain itself rather than in one
parser -- e.g. a 4K request being attached to the wrong Radarr instance.
"""

from __future__ import annotations

import json
from unittest import mock

import httpx
from django.test import TestCase

from core.clients.arr import RadarrClient
from core.clients.download import QBittorrentClient
from core.clients.mediaserver import PlexClient
from core.clients.requestmanager import SeerrClient
from core.ingest.arr import sync_arr_entities, sync_arr_history
from core.ingest.download import sync_download_clients
from core.ingest.requests import sync_service_requests
from core.models import (
    EventType,
    MediaAvailability,
    PathMapping,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.tests.factories import load, make_service

RADARR_MOVIE = {
    "id": 412,
    "title": "Dune: Part Two",
    "year": 2024,
    "tmdbId": 693134,
    "titleSlug": "dune-part-two-2024",
    "monitored": True,
    "hasFile": True,
    "movieFileId": 5,
    "qualityProfileId": 4,
    "isAvailable": True,
    "minimumAvailability": "released",
    "added": "2026-07-01T10:00:30Z",
    "lastSearchTime": "2026-07-01T10:01:00Z",
    "path": "/data/media/movies/Dune Part Two (2024)",
    "movieFile": {
        "id": 5,
        "path": (
            "/data/media/movies/Dune Part Two (2024)/"
            "Dune Part Two (2024) WEBDL-1080p.mkv"
        ),
        "quality": {"quality": {"name": "WEBDL-1080p"}},
        "qualityCutoffNotMet": False,
    },
}

RADARR_MOVIE_4K = {**RADARR_MOVIE, "id": 77, "hasFile": False, "qualityProfileId": 7}


def _json(payload, status=200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def mock_client(client, handler):
    """Patch a client's httpx.Client with a MockTransport-backed one."""
    transport = httpx.MockTransport(handler)
    real = httpx.Client(
        base_url=client.base_url,
        headers=client.default_headers(),
        transport=transport,
    )
    return mock.patch.object(type(client), "http", property(lambda self: real))


class SeerrIngestTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Seerr",
            variant=ServiceVariant.SEERR,
            base_url="http://seerr:5055",
        )
        # Two Radarr instances, distinguished by the request manager's serviceId.
        self.radarr_hd = make_service(
            ServiceKind.RADARR,
            name="Radarr HD",
            base_url="http://radarr:7878",
            remote_service_id=0,
            is_4k=False,
        )
        self.radarr_4k = make_service(
            ServiceKind.RADARR,
            name="Radarr 4K",
            base_url="http://radarr4k:7878",
            remote_service_id=1,
            is_4k=True,
        )
        self.payload = load("seerr_requests.json")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/request"):
            if int(request.url.params.get("skip", 0)) > 0:
                return _json({"pageInfo": {"results": 3}, "results": []})
            return _json(self.payload)
        if "/movie/" in path or "/tv/" in path:
            return _json({"title": "Dune: Part Two", "releaseDate": "2024-02-27"})
        return _json({}, status=404)

    def test_requests_ingest_and_route_to_the_right_instance(self):
        client = SeerrClient(self.seerr)
        with mock_client(client, self.handler), mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            sync_service_requests(self.seerr)

        self.assertEqual(TrackedRequest.objects.count(), 3)

        standard = TrackedRequest.objects.get(remote_id=101)
        four_k = TrackedRequest.objects.get(remote_id=102)

        # The crux: same title, same media row, different lanes -> different instances.
        self.assertFalse(standard.is_4k)
        self.assertEqual(standard.arr_service, self.radarr_hd)
        self.assertEqual(standard.arr_entity_id, 412)

        self.assertTrue(four_k.is_4k)
        self.assertEqual(four_k.arr_service, self.radarr_4k)
        self.assertEqual(four_k.arr_entity_id, 77)

    def test_failed_request_records_a_request_failed_event(self):
        client = SeerrClient(self.seerr)
        with mock_client(client, self.handler), mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            sync_service_requests(self.seerr)

        failed = TrackedRequest.objects.get(remote_id=103)
        self.assertIsNone(failed.arr_entity_id)
        self.assertTrue(
            failed.events.filter(event_type=EventType.REQUEST_FAILED).exists()
        )

    def test_reingest_is_idempotent(self):
        client = SeerrClient(self.seerr)
        for _ in range(2):
            with mock_client(client, self.handler), mock.patch(
                "core.ingest.requests.request_manager_client", return_value=client
            ):
                sync_service_requests(self.seerr)

        self.assertEqual(TrackedRequest.objects.count(), 3)
        request = TrackedRequest.objects.get(remote_id=101)
        # One 'requested' event, not two.
        self.assertEqual(
            request.events.filter(event_type=EventType.REQUESTED).count(), 1
        )


class ArrIngestTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(
            ServiceKind.RADARR, name="Radarr", base_url="http://radarr:7878"
        )
        self.request = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=101,
            media_type="movie",
            requested_at="2026-07-01T10:00:00Z",
            tmdb_id=693134,
            arr_service=self.radarr,
            arr_entity_id=412,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/qualityprofile":
            return _json([{"id": 4, "name": "HD-1080p"}])
        if path == "/api/v3/movie/412":
            return _json(RADARR_MOVIE)
        if path == "/api/v3/history/movie":
            return _json(load("radarr_history.json"))
        if path == "/api/v3/queue":
            return _json(load("radarr_queue.json"))
        return _json({}, status=404)

    def test_entity_and_history_ingest(self):
        client = RadarrClient(self.radarr)
        with mock_client(client, self.handler), mock.patch(
            "core.ingest.arr.arr_client", return_value=client
        ):
            sync_arr_entities()
            sync_arr_history()

        self.request.refresh_from_db()
        self.assertEqual(self.request.arr_quality_profile_name, "HD-1080p")
        self.assertTrue(self.request.arr_monitored)
        self.assertTrue(self.request.arr_has_file)

        types = set(self.request.events.values_list("event_type", flat=True))
        self.assertIn(EventType.GRABBED, types)
        self.assertIn(EventType.IMPORTED, types)

        imported = self.request.events.get(event_type=EventType.IMPORTED)
        self.assertIn("Dune Part Two", imported.detail)

    def test_history_ingest_is_idempotent(self):
        client = RadarrClient(self.radarr)
        for _ in range(3):
            with mock_client(client, self.handler), mock.patch(
                "core.ingest.arr.arr_client", return_value=client
            ):
                sync_arr_history()
        self.assertEqual(
            self.request.events.filter(event_type=EventType.GRABBED).count(), 1
        )


class DownloadIngestTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.qbt = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="qBittorrent",
            variant=ServiceVariant.QBITTORRENT,
            base_url="http://qbt:8080",
        )
        self.request = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=101,
            media_type="movie",
            requested_at="2026-07-01T10:00:00Z",
        )
        # An *arr grab event carrying the UPPERCASE download id.
        TimelineEvent.objects.create(
            request=self.request,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.GRABBED,
            occurred_at="2026-07-01T10:04:00Z",
            summary="Grabbed",
            dedupe_key="arr:1:history:9001",
            raw={"downloadId": "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"},
        )

    def test_uppercase_download_id_joins_to_lowercase_torrent_hash(self):
        torrents = load("qbittorrent_torrents.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/torrents/info"):
                wanted = set(request.url.params.get("hashes", "").split("|"))
                return _json([t for t in torrents if t["hash"] in wanted])
            return httpx.Response(200, text="Ok.")

        client = QBittorrentClient(self.qbt)
        client._authenticated = True
        with mock_client(client, handler), mock.patch(
            "core.ingest.download.download_client", return_value=client
        ):
            sync_download_clients()

        samples = self.request.events.filter(event_type=EventType.DOWNLOAD_PROGRESS)
        self.assertEqual(samples.count(), 1)
        self.assertIn("Stalled", samples.first().summary)


class PlexPathIndexTests(TestCase):
    def setUp(self):
        self.plex = make_service(
            ServiceKind.MEDIA_SERVER, name="Plex", base_url="http://plex:32400"
        )

    def test_index_and_mapping_resolve_an_arr_path(self):
        metadata = load("plex_metadata.json")

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/library/sections":
                return _json(
                    {
                        "MediaContainer": {
                            "Directory": [{"key": "1", "type": "movie", "title": "Movies"}]
                        }
                    }
                )
            if path == "/library/sections/1/all":
                if int(request.headers.get("X-Plex-Container-Start", 0)) > 0:
                    return _json({"MediaContainer": {"totalSize": 1, "Metadata": []}})
                return _json(
                    {
                        "MediaContainer": {
                            "totalSize": 1,
                            "Metadata": metadata["MediaContainer"]["Metadata"],
                        }
                    }
                )
            return _json({}, status=404)

        client = PlexClient(self.plex)
        with mock_client(client, handler):
            index = client.build_path_index()

        self.assertEqual(len(index), 1)
        self.assertIn(
            "/movies/dune part two (2024)/dune part two (2024) webdl-1080p.mkv", index
        )

        from core.clients.mediaserver import match_paths

        mapping = PathMapping(
            source_prefix="/data/media/movies", target_prefix="/movies"
        )
        result = match_paths(
            [
                "/data/media/movies/Dune Part Two (2024)/"
                "Dune Part Two (2024) WEBDL-1080p.mkv"
            ],
            index,
            [mapping],
        )
        self.assertTrue(result.found)
