"""The full path-mismatch lifecycle: broken mapping -> user fixes it -> verdict clears.

This is the loop the PATH_MISMATCH diagnosis lives inside, and both ends of it have
failed in ways unit tests on either half alone would miss.
"""

from __future__ import annotations

from unittest import mock

import httpx
from django.test import TestCase
from django.utils import timezone

from core.clients.mediaserver import PlexClient
from core.ingest.mediaserver import sync_media_servers
from core.models import (
    EventType,
    PathMapping,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.rules.engine import diagnose_request
from core.tests.factories import load, make_service
from core.tests.test_ingest import mock_client

ARR_PATH = (
    "/data/media/movies/Dune Part Two (2024)/Dune Part Two (2024) WEBDL-1080p.mkv"
)


class PlexPathMismatchLifecycleTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.plex = make_service(
            ServiceKind.MEDIA_SERVER, name="Plex", base_url="http://plex:32400"
        )
        self.request_ = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=101,
            title="Dune: Part Two",
            media_type="movie",
            requested_at=timezone.now() - timezone.timedelta(days=12),
            arr_service=self.radarr,
            arr_entity_id=412,
            arr_has_file=True,
            media_server_item_id="20481",
        )
        TimelineEvent.objects.create(
            request=self.request_,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.IMPORTED,
            occurred_at=timezone.now() - timezone.timedelta(days=11),
            summary="Imported",
            dedupe_key="arr:1:history:9002",
            raw={"data": {"importedPath": ARR_PATH}},
        )
        self.metadata = load("plex_metadata.json")

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [{"key": "1", "type": "movie", "title": "Movies"}]
                    }
                },
            )
        if path == "/library/sections/1/all":
            if int(request.headers.get("X-Plex-Container-Start", 0)) > 0:
                return httpx.Response(
                    200, json={"MediaContainer": {"totalSize": 1, "Metadata": []}}
                )
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "totalSize": 1,
                        "Metadata": self.metadata["MediaContainer"]["Metadata"],
                    }
                },
            )
        if path == "/library/metadata/20481":
            return httpx.Response(200, json=self.metadata)
        return httpx.Response(404, json={})

    def _sync(self):
        client = PlexClient(self.plex)
        with mock_client(client, self.handler), mock.patch(
            "core.ingest.mediaserver.media_server_client", return_value=client
        ):
            sync_media_servers()

    def test_mismatch_then_fix_then_clear(self):
        # --- 1. No mapping configured. Plex has the file at /movies/..., the *arr
        #        reports /data/media/movies/... -- the ratingKey resolves, the path does
        #        not. That is a mapping fault, not a missing file.
        self._sync()
        self.request_.refresh_from_db()

        self.assertTrue(
            self.request_.events.filter(
                dedupe_key__endswith=":path_mismatch"
            ).exists()
        )
        diagnosis = diagnose_request(self.request_)
        self.assertEqual(diagnosis.code, "PATH_MISMATCH")
        # The ratingKey join succeeded, so media_server_found is True -- and the diagnosis must
        # still fire despite that.
        self.assertTrue(self.request_.media_server_found)

        # --- 2. The user adds the mapping the timeline suggested.
        PathMapping.objects.create(
            source_prefix="/data/media/movies", target_prefix="/movies"
        )
        self._sync()
        self.request_.refresh_from_db()

        # --- 3. The stale mismatch event must be gone, not merely superseded, or the
        #        verdict would persist for a problem that no longer exists.
        self.assertFalse(
            self.request_.events.filter(
                dedupe_key__endswith=":path_mismatch"
            ).exists()
        )
        self.assertTrue(self.request_.media_server_found)
        self.assertEqual(self.request_.media_server_matched_path, "/movies/Dune Part Two (2024)/Dune Part Two (2024) WEBDL-1080p.mkv")
        self.assertIsNone(diagnose_request(self.request_))


class EmptyLibraryProvesNothingTests(TestCase):
    """A library with nothing in it cannot testify that a file is missing from it.

    Verified against a live Jellyfin 10.11.11: a fully started, authenticated server
    with no libraries configured answers the library query with HTTP 200 and
    `{"Items": [], "TotalRecordCount": 0}`. Nothing about that response is an error, so
    the poll records a success, the service looks healthy, and every request checked
    against the resulting empty index reads as "imported, but not in your library".

    That is the Finding 13 failure mode arriving through a *successful* read rather than
    a failed one, which is why reachability alone was not enough to catch it.
    """

    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.jellyfin = make_service(
            ServiceKind.MEDIA_SERVER,
            name="Jellyfin",
            variant=ServiceVariant.JELLYFIN,
            base_url="http://jellyfin:8096",
        )
        self.request_ = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=101,
            title="Dune: Part Two",
            media_type="movie",
            tmdb_id=693134,
            requested_at=timezone.now() - timezone.timedelta(days=12),
            arr_service=self.radarr,
            arr_entity_id=412,
            arr_has_file=True,
        )
        TimelineEvent.objects.create(
            request=self.request_,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.IMPORTED,
            occurred_at=timezone.now() - timezone.timedelta(days=11),
            summary="Imported",
            dedupe_key="arr:1:history:9002",
            raw={"data": {"importedPath": ARR_PATH}},
        )

    def _sync(self, handler):
        from core.clients.mediaserver import JellyfinClient

        client = JellyfinClient(self.jellyfin)
        with mock_client(client, handler), mock.patch(
            "core.ingest.mediaserver.media_server_client", return_value=client
        ):
            sync_media_servers()

    @staticmethod
    def _empty(request: httpx.Request) -> httpx.Response:
        """Exactly what a live Jellyfin with no libraries returns."""
        return httpx.Response(
            200, json={"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
        )

    def test_no_missing_verdict_from_an_empty_library(self):
        self._sync(self._empty)
        self.request_.refresh_from_db()

        self.assertFalse(
            self.request_.events.filter(dedupe_key__endswith=":missing").exists(),
            "an empty library was recorded as the file being missing",
        )

        diagnosis = diagnose_request(self.request_)
        code = diagnosis.code if diagnosis else None
        self.assertNotEqual(
            code,
            "NOT_IN_MEDIA_SERVER",
            "an empty library was reported as the file not being in the library",
        )
        if diagnosis is not None:
            self.assertEqual(diagnosis.code, "EVIDENCE_UNAVAILABLE")

    def test_the_service_is_not_marked_broken_for_it(self):
        """It answered. Backing off a healthy server would be its own bug."""
        self._sync(self._empty)
        self.jellyfin.refresh_from_db()
        self.assertEqual(self.jellyfin.consecutive_failures, 0)
        self.assertIsNotNone(self.jellyfin.last_seen_ok)
        self.assertTrue(self.jellyfin.client_state.get("empty_library"))

    def test_a_populated_library_still_reports_a_genuinely_missing_file(self):
        """The guard must not silence the real diagnosis it sits in front of."""

        def populated(request: httpx.Request) -> httpx.Response:
            if int(request.url.params.get("startIndex", 0)) > 0:
                return httpx.Response(200, json={"Items": [], "TotalRecordCount": 1})
            return httpx.Response(
                200,
                json={
                    "TotalRecordCount": 1,
                    "Items": [
                        {
                            "Id": "abc",
                            "Name": "Some Other Film",
                            "Type": "Movie",
                            "Path": "/movies/Some Other Film (2011)/film.mkv",
                            "ProviderIds": {"Tmdb": "999"},
                        }
                    ],
                },
            )

        self._sync(populated)
        self.request_.refresh_from_db()

        self.assertTrue(
            self.request_.events.filter(dedupe_key__endswith=":missing").exists()
        )
        self.jellyfin.refresh_from_db()
        self.assertFalse(self.jellyfin.client_state.get("empty_library"))
        diagnosis = diagnose_request(self.request_)
        self.assertEqual(diagnosis.code, "NOT_IN_MEDIA_SERVER")

    def test_a_truncated_walk_is_an_error_not_a_short_library(self):
        """A page we cannot read must not quietly end the walk.

        Returning what we have so far looks harmless and is not: a short library is read
        downstream as files the server does not have.
        """
        from core.clients.base import ServiceError
        from core.clients.mediaserver import JellyfinClient

        def truncating(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=None)

        client = JellyfinClient(self.jellyfin)
        with mock_client(client, truncating):
            with self.assertRaises(ServiceError):
                client.build_path_index()


class MediaServerLinksMatchTheProductTests(TestCase):
    """The deep link has to be the one the configured server actually understands.

    Plex's is a `#!/server/-/details?key=...` fragment; Jellyfin and Emby use
    `#!/details?id=...`. Emitting Plex's form for all three gave every Jellyfin and Emby
    user a dead link labelled "Open in Plex", attached to a verdict about their library.
    """

    def _url(self, variant, item_id=""):
        from core.rules.base import RuleContext

        seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name=f"Seerr-{variant}",
            variant=ServiceVariant.SEERR,
        )
        make_service(
            ServiceKind.MEDIA_SERVER, name=f"server-{variant}", variant=variant,
            base_url="http://server:8096",
        )
        tracked = TrackedRequest.objects.create(
            service=seerr, remote_id=1, title="x", media_type="movie",
            requested_at=timezone.now(), media_server_item_id=item_id,
        )
        return RuleContext(tracked, []).media_server_url()

    def test_each_product_gets_its_own_link_and_name(self):
        url, label = self._url(ServiceVariant.PLEX, "20481")
        self.assertIn("%2Flibrary%2Fmetadata%2F20481", url)
        self.assertEqual(label, "Open in Plex")

        url, label = self._url(ServiceVariant.JELLYFIN, "abc123")
        self.assertIn("#!/details?id=abc123", url)
        self.assertNotIn("library%2Fmetadata", url)
        self.assertEqual(label, "Open in Jellyfin")

        url, label = self._url(ServiceVariant.EMBY, "abc123")
        self.assertIn("#!/details?id=abc123", url)
        self.assertEqual(label, "Open in Emby")

    def test_without_a_stored_id_it_still_points_at_the_right_product(self):
        url, label = self._url(ServiceVariant.JELLYFIN)
        self.assertTrue(url.endswith("/web/index.html"))
        self.assertEqual(label, "Open Jellyfin")
