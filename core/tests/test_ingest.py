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


class ModelAttributeGuardTests(TestCase):
    """Django accepts assignment to any attribute, field or not, and silently drops it.

    That turned a field rename into invisible data loss: `tracked.plex_rating_key = ...`
    kept working after the field became `media_server_item_id`, so the Plex rating key
    was never stored and the id-based media-server join quietly had nothing to work with.
    Nothing failed; the feature just stopped existing.
    """

    def test_every_attribute_the_upsert_sets_is_a_real_field(self):
        import inspect
        import re

        from core.ingest import requests as ingest_requests
        from core.models import TrackedRequest

        source = inspect.getsource(ingest_requests._upsert)
        assigned = set(re.findall(r"^\s*tracked\.([a-z_][a-z0-9_]*)\s*=", source, re.M))
        fields = {f.name for f in TrackedRequest._meta.get_fields()}
        # Foreign keys can be assigned by object or by _id.
        fields |= {f"{f.name}_id" for f in TrackedRequest._meta.get_fields()}

        unknown = sorted(assigned - fields)
        self.assertEqual(
            unknown,
            [],
            f"These are assigned in _upsert but are not model fields, so their values "
            f"are silently discarded: {unknown}",
        )

    def test_rating_key_actually_round_trips(self):
        from core.clients.requestmanager import SeerrClient
        from core.models import ServiceInstance

        seerr = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Seerr",
            variant=ServiceVariant.SEERR,
            base_url="http://seerr:5055",
        )
        make_service(ServiceKind.RADARR, name="Radarr", remote_service_id=0)
        payload = load("seerr_requests.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/request"):
                if int(request.url.params.get("skip", 0)) > 0:
                    return _json({"pageInfo": {"results": 3}, "results": []})
                return _json(payload)
            return _json({"title": "x", "releaseDate": "2024-01-01"})

        client = SeerrClient(seerr)
        with mock_client(client, handler), mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            sync_service_requests(seerr)

        standard = TrackedRequest.objects.get(remote_id=101)
        # The fixture carries ratingKey "20481" on the non-4K lane.
        self.assertEqual(standard.media_server_item_id, "20481")


class DeletionReconcileTests(TestCase):
    """A request deleted in the request manager has to disappear here too.

    The normal sync stops as soon as it recognises records, so it can never notice an
    absence. Reported from a live instance: a request removed from Seerr -- which also
    removed it from Radarr -- was still listed days later.
    """

    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Seerr",
            variant=ServiceVariant.SEERR,
            base_url="http://seerr:5055",
        )
        make_service(ServiceKind.RADARR, name="Radarr", remote_service_id=0)
        self.payload = load("seerr_requests.json")

    def _handler(self, results):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/request"):
                if int(request.url.params.get("skip", 0)) > 0:
                    return _json({"pageInfo": {"results": len(results)}, "results": []})
                return _json({"pageInfo": {"results": len(results)}, "results": results})
            return _json({"title": "x", "releaseDate": "2024-01-01"})

        return handler

    def _sync(self, results, **kwargs):
        from core.clients.requestmanager import SeerrClient

        client = SeerrClient(self.seerr)
        with mock_client(client, self._handler(results)), mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            return sync_service_requests(self.seerr, **kwargs)

    def test_request_removed_upstream_is_deleted_here(self):
        self._sync(self.payload["results"])
        self.assertEqual(TrackedRequest.objects.count(), 3)

        # The user deletes request 102 in Seerr.
        remaining = [r for r in self.payload["results"] if r["id"] != 102]
        self._sync(remaining, force_reconcile=True)

        ids = set(TrackedRequest.objects.values_list("remote_id", flat=True))
        self.assertEqual(ids, {101, 103})

    def test_its_events_and_diagnosis_go_with_it(self):
        self._sync(self.payload["results"])
        tracked = TrackedRequest.objects.get(remote_id=102)
        TimelineEvent.objects.create(
            request=tracked,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.GRABBED,
            occurred_at="2026-07-02T18:30:00Z",
            summary="Grabbed",
            dedupe_key="x",
        )
        remaining = [r for r in self.payload["results"] if r["id"] != 102]
        self._sync(remaining, force_reconcile=True)
        self.assertFalse(TimelineEvent.objects.filter(request_id=tracked.pk).exists())

    def test_a_normal_sync_does_not_delete(self):
        """Only a full walk can distinguish "gone" from "stopped looking"."""
        self._sync(self.payload["results"], force_reconcile=True)
        remaining = [r for r in self.payload["results"] if r["id"] != 102]

        from core.models import IngestCursor

        # Reconcile has just run, so the next ordinary sync must leave things alone.
        cursor = IngestCursor.objects.get(service=self.seerr, scope="requests")
        self.assertIsNotNone(cursor.last_reconcile)
        self._sync(remaining)
        self.assertTrue(TrackedRequest.objects.filter(remote_id=102).exists())

    def test_a_failed_walk_never_deletes(self):
        """Deleting on a partial list would wipe real data on a transient outage."""
        from core.clients.base import ServiceError
        from core.clients.requestmanager import SeerrClient

        self._sync(self.payload["results"])
        self.assertEqual(TrackedRequest.objects.count(), 3)

        def failing(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={})

        client = SeerrClient(self.seerr)
        with mock_client(client, failing), mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            with self.assertRaises(ServiceError):
                sync_service_requests(self.seerr, force_reconcile=True)

        self.assertEqual(TrackedRequest.objects.count(), 3)

    def test_deletions_are_noticed_even_with_history_past_the_cutoff(self):
        """Reported from a live instance: a request removed in Seerr stayed for months.

        The walk runs newest-first and stops at the backfill cutoff, which is correct --
        older requests are out of scope. But stopping there was treated as an incomplete
        walk, and only a complete one is trusted to notice an absence. So the moment a
        user had a single request older than `backfill_days`, every walk ended at the
        cutoff, and reconciliation never ran again for as long as that request existed.

        Reaching the cutoff *is* a complete walk of the window that matters: everything
        newer has been seen, and everything newer is exactly what `_remove_deleted`
        judges. Only the "25 untouched records, we have caught up" stop is a genuine
        early exit, and that one is already skipped while reconciling.
        """
        from django.utils import timezone as tz

        ancient = {
            **self.payload["results"][0],
            "id": 900,
            "createdAt": (tz.now() - tz.timedelta(days=400)).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        with_history = [*self.payload["results"], ancient]

        self._sync(with_history)
        self.assertEqual(TrackedRequest.objects.count(), 3, "the old one is out of scope")

        # The user deletes 102 upstream.
        remaining = [r for r in with_history if r["id"] != 102]
        self._sync(remaining, force_reconcile=True)

        ids = set(TrackedRequest.objects.values_list("remote_id", flat=True))
        self.assertEqual(
            ids,
            {101, 103},
            "a request deleted upstream survived because older history stopped the walk",
        )

    def test_a_walk_that_saw_nothing_at_all_never_deletes(self):
        """The last line of defence, and deliberately product-agnostic.

        Everything else here rests on a broken walk raising. A client that returns an
        empty list instead of raising defeats all of it, and the result is silent,
        permanent data loss -- so an empty census is refused outright rather than
        trusted.

        The cost is real and accepted: a user who genuinely deletes every request keeps
        seeing them here until one is re-added or they clear them by hand. That is the
        recoverable direction of the trade.
        """
        self._sync(self.payload["results"])
        self.assertEqual(TrackedRequest.objects.count(), 3)

        self._sync([], force_reconcile=True)

        self.assertEqual(
            TrackedRequest.objects.count(),
            3,
            "an empty walk was trusted as proof the user deleted everything",
        )

    def test_requests_older_than_the_cutoff_are_left_alone(self):
        """They were never walked, so their absence from the list means nothing."""
        from django.utils import timezone as tz

        self._sync(self.payload["results"])
        old = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=999,
            media_type="movie",
            requested_at=tz.now() - tz.timedelta(days=400),
        )
        self._sync(self.payload["results"], force_reconcile=True)
        self.assertTrue(TrackedRequest.objects.filter(pk=old.pk).exists())


class OmbiFailedWalkTests(TestCase):
    """The same protection as `test_a_failed_walk_never_deletes`, on Ombi.

    Ombi reaches the deletion logic by a different route, and used to defeat it. Its
    `iter_requests` walks two endpoints and caught the error from each one so that a
    failure of one did not lose the other. The effect was that a walk which read nothing
    at all still ended as a *successful, complete* walk -- and a complete walk that saw
    no requests is precisely the evidence `_remove_deleted` acts on.

    Found against a live Ombi: with no API key, or a rotated one, both endpoints answer
    401. Within one reconcile window every tracked request and its whole timeline was
    deleted, which is the one failure in this application that cannot be undone by
    fixing the config afterwards.
    """

    def setUp(self):
        self.ombi = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Ombi",
            variant=ServiceVariant.OMBI,
            base_url="http://ombi:3579",
        )
        self.rows = [
            {
                "id": 1,
                "title": "Arrival",
                "theMovieDbId": 329865,
                "requestedDate": "2026-07-01T10:00:00Z",
                "approved": True,
                "available": False,
            }
        ]

    def _client(self, handler):
        from core.clients.requestmanager import OmbiClient

        client = OmbiClient(self.ombi)
        return client, mock_client(client, handler)

    def _sync(self, handler, **kwargs):
        client, patched = self._client(handler)
        with patched, mock.patch(
            "core.ingest.requests.request_manager_client", return_value=client
        ):
            return sync_service_requests(self.ombi, **kwargs)

    def _ok(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Request/movie"):
            return _json(self.rows)
        return _json([])

    def test_a_seeded_request_is_tracked(self):
        self._sync(self._ok)
        self.assertEqual(TrackedRequest.objects.count(), 1)

    def test_an_unauthorised_walk_never_deletes(self):
        """401 on both endpoints is not evidence that the user deleted everything."""
        from core.clients.base import ServiceError

        self._sync(self._ok)
        self.assertEqual(TrackedRequest.objects.count(), 1)

        def unauthorised(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Invalid API Key")

        with self.assertRaises(ServiceError):
            self._sync(unauthorised, force_reconcile=True)

        self.assertEqual(
            TrackedRequest.objects.count(),
            1,
            "a rejected API key deleted the user's tracked requests",
        )

    def test_one_broken_endpoint_never_deletes_the_other_media_type(self):
        """The subtler half: a partial read is still not a complete walk.

        If only /Request/tv fails, the walk yields every movie and no TV -- which reads
        as "the user deleted all their TV requests" and is indistinguishable from it
        unless the failure is allowed to surface.
        """
        from core.clients.base import ServiceError

        # The real /Request/tv shape: the parent is the show, and the request itself --
        # date, state, user, seasons -- is on the child.
        tv_row = {
            "id": 2,
            "title": "Severance",
            "tvDbId": 371980,
            "childRequests": [
                {
                    "id": 91,
                    "parentRequestId": 2,
                    "requestedDate": "2026-07-02T10:00:00Z",
                    "approved": True,
                    "available": False,
                    "requestedUser": {"userName": "sleuth"},
                    "seasonRequests": [{"seasonNumber": 1, "episodes": []}],
                }
            ],
        }

        def both(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/Request/movie"):
                return _json(self.rows)
            return _json([tv_row])

        self._sync(both)
        self.assertEqual(TrackedRequest.objects.count(), 2)

        def tv_broken(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/Request/movie"):
                return _json(self.rows)
            return httpx.Response(500, text="boom")

        with self.assertRaises(ServiceError):
            self._sync(tv_broken, force_reconcile=True)

        self.assertEqual(
            TrackedRequest.objects.count(),
            2,
            "one failing endpoint deleted the other media type's requests",
        )
