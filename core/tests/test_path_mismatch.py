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
