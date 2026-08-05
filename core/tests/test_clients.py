"""Client parsing tests, driven by recorded fixtures.

These lock in the wire-format findings recorded in docs/api-notes.md. Each of them
corresponds to a real way this integration can silently produce a wrong verdict.
"""

from __future__ import annotations

from django.test import TestCase

from core.clients.arr import RadarrClient, SonarrClient, canonical_event
from core.clients.download import QBittorrentClient
from core.clients.plex import PlexClient, match_paths, normalise_path, suggest_mapping
from core.clients.requestmanager import (
    MEDIA_STATUS_BY_VARIANT,
    OverseerrClient,
    SeerrClient,
    resolve_service_keys,
)
from core.models import (
    EventType,
    MediaAvailability,
    MediaType,
    PathMapping,
    RequestState,
    ServiceKind,
    ServiceVariant,
)
from core.tests.factories import load, make_service


class RequestManagerParsingTests(TestCase):
    def setUp(self):
        self.service = make_service(
            ServiceKind.REQUEST_MANAGER,
            variant=ServiceVariant.SEERR,
            base_url="http://seerr.local:5055",
        )
        self.client_ = SeerrClient(self.service)
        self.payload = load("seerr_requests.json")

    def test_parses_standard_request(self):
        parsed = self.client_.parse_request(self.payload["results"][0])
        self.assertEqual(parsed.remote_id, 101)
        self.assertEqual(parsed.media_type, MediaType.MOVIE)
        self.assertEqual(parsed.requested_by, "alice")
        self.assertEqual(parsed.request_state, RequestState.APPROVED)
        self.assertEqual(parsed.tmdb_id, 693134)
        self.assertFalse(parsed.is_4k)

    def test_4k_request_resolves_the_4k_key_pair(self):
        """The whole join hangs off this.

        Requests 101 and 102 share one media object. The 4K one must resolve to
        serviceId 1 / externalServiceId 77, not the standard lane's 0 / 412 -- otherwise
        it joins to the wrong Radarr instance and every downstream verdict is wrong.
        """
        standard = self.client_.parse_request(self.payload["results"][0])
        four_k = self.client_.parse_request(self.payload["results"][1])

        self.assertEqual(standard.keys.service_id, 0)
        self.assertEqual(standard.keys.external_service_id, 412)
        self.assertEqual(standard.keys.rating_key, "20481")

        self.assertTrue(four_k.is_4k)
        self.assertEqual(four_k.keys.service_id, 1)
        self.assertEqual(four_k.keys.external_service_id, 77)
        # ratingKey4k is null in the fixture: the 4K copy is not in Plex.
        self.assertEqual(four_k.keys.rating_key, "")

    def test_4k_status_comes_from_status4k(self):
        standard = self.client_.parse_request(self.payload["results"][0])
        four_k = self.client_.parse_request(self.payload["results"][1])
        # status=3 PROCESSING, status4k=1 UNKNOWN
        self.assertEqual(standard.keys.availability, MediaAvailability.PROCESSING)
        self.assertEqual(four_k.keys.availability, MediaAvailability.UNKNOWN)

    def test_null_external_service_id_is_preserved_not_defaulted(self):
        """Request 103 failed to push; a null id must stay null.

        Substituting the non-4K id or a zero here would make the 'never added'
        diagnosis impossible to reach.
        """
        parsed = self.client_.parse_request(self.payload["results"][2])
        self.assertIsNone(parsed.keys.external_service_id)
        self.assertEqual(parsed.request_state, RequestState.FAILED)
        self.assertEqual(parsed.seasons, [2, 3])
        self.assertEqual(parsed.tvdb_id, 371572)

    def test_media_status_6_differs_between_seerr_and_overseerr(self):
        """Verified against each project's server/constants/media.ts.

        Seerr/Jellyseerr: 6 = BLOCKLISTED, 7 = DELETED.
        Overseerr:        6 = DELETED (it has no BLOCKLISTED member).
        The published Seerr OpenAPI docs still say 6 = DELETED, which is stale.
        """
        media = {"status": 6}
        seerr = resolve_service_keys(media, False, ServiceVariant.SEERR)
        overseerr = resolve_service_keys(media, False, ServiceVariant.OVERSEERR)
        self.assertEqual(seerr.availability, MediaAvailability.BLOCKLISTED)
        self.assertEqual(overseerr.availability, MediaAvailability.DELETED)

        self.assertEqual(
            MEDIA_STATUS_BY_VARIANT[ServiceVariant.SEERR][7],
            MediaAvailability.DELETED,
        )
        self.assertNotIn(7, MEDIA_STATUS_BY_VARIANT[ServiceVariant.OVERSEERR])

    def test_overseerr_client_uses_the_overseerr_table(self):
        service = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="overseerr",
            variant=ServiceVariant.OVERSEERR,
        )
        item = dict(self.payload["results"][0])
        item["media"] = {**item["media"], "status": 6}
        parsed = OverseerrClient(service).parse_request(item)
        self.assertEqual(parsed.keys.availability, MediaAvailability.DELETED)

    def test_missing_created_at_is_skipped_not_crashed(self):
        item = dict(self.payload["results"][0])
        item.pop("createdAt")
        self.assertIsNone(self.client_.parse_request(item))


class ArrEventNormalisationTests(TestCase):
    def test_sonarr_and_radarr_import_names_both_normalise(self):
        """The two apps name the same concept differently.

        Missing either would make imports invisible for one media type, which silently
        turns every imported movie (or episode) into a false 'never imported' verdict.
        """
        self.assertEqual(canonical_event("seriesFolderImported"), EventType.IMPORTED)
        self.assertEqual(canonical_event("movieFolderImported"), EventType.IMPORTED)
        self.assertEqual(canonical_event("downloadFolderImported"), EventType.IMPORTED)

    def test_delete_names_both_normalise(self):
        self.assertEqual(canonical_event("episodeFileDeleted"), EventType.FILE_DELETED)
        self.assertEqual(canonical_event("movieFileDeleted"), EventType.FILE_DELETED)

    def test_case_insensitive(self):
        self.assertEqual(canonical_event("Grabbed"), EventType.GRABBED)

    def test_integer_event_type_is_not_guessed(self):
        """Integers are refused rather than mapped by ordinal.

        The two enums order their members differently and the C# values are
        non-contiguous, so guessing would mislabel events.
        """
        self.assertEqual(canonical_event(3), EventType.UNKNOWN)
        self.assertEqual(canonical_event(None), EventType.UNKNOWN)

    def test_unknown_string_falls_back(self):
        self.assertEqual(canonical_event("somethingNew"), EventType.UNKNOWN)


class ArrHistoryParsingTests(TestCase):
    def test_radarr_history_parses(self):
        rows = [RadarrClient.parse_history_row(r) for r in load("radarr_history.json")]
        grab, imported = rows
        self.assertEqual(grab.event_type, EventType.GRABBED)
        self.assertEqual(grab.download_id, "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678")
        self.assertEqual(grab.quality, "WEBDL-1080p")
        self.assertEqual(imported.event_type, EventType.IMPORTED)
        self.assertEqual(imported.raw_event_type, "downloadFolderImported")
        self.assertEqual(
            imported.data["importedPath"],
            "/data/media/movies/Dune Part Two (2024)/Dune Part Two (2024) WEBDL-1080p.mkv",
        )

    def test_sonarr_history_normalises_its_own_import_name(self):
        rows = [SonarrClient.parse_history_row(r) for r in load("sonarr_history.json")]
        types = [r.event_type for r in rows]
        self.assertEqual(
            types,
            [
                EventType.GRABBED,
                EventType.IMPORTED,
                EventType.DOWNLOAD_FAILED,
                EventType.FILE_DELETED,
            ],
        )
        self.assertEqual(rows[1].raw_event_type, "seriesFolderImported")
        self.assertEqual(rows[3].raw_event_type, "episodeFileDeleted")

    def test_history_row_without_date_is_skipped(self):
        self.assertIsNone(RadarrClient.parse_history_row({"id": 1, "eventType": "grabbed"}))

    def test_sonarr_queue_param_names_differ_from_radarr(self):
        self.assertEqual(SonarrClient.unknown_items_param, "includeUnknownSeriesItems")
        self.assertEqual(RadarrClient.unknown_items_param, "includeUnknownMovieItems")
        self.assertEqual(SonarrClient.entity_id_param, "seriesId")
        self.assertEqual(RadarrClient.entity_id_param, "movieId")

    def test_queue_row_reads_status_messages_not_just_error_message(self):
        """The hardlink/permission text lives in statusMessages, not errorMessage."""
        service = make_service(ServiceKind.RADARR)
        client = RadarrClient(service)
        row = load("radarr_queue.json")["records"][0]
        item = client._parse_queue_row(row)
        self.assertEqual(item.error_message, "")
        self.assertEqual(item.tracked_state, "importBlocked")
        self.assertTrue(item.blocked_messages)
        self.assertIn("not accessible by Radarr", item.blocked_messages[0])
        self.assertEqual(item.progress, 1.0)


class QBittorrentParsingTests(TestCase):
    def test_hash_is_lowercased_for_the_join(self):
        """*arr downloadIds are uppercase; qBittorrent hashes are lowercase."""
        rows = load("qbittorrent_torrents.json")
        parsed = QBittorrentClient._parse(rows[0])
        self.assertEqual(parsed.hash, rows[0]["hash"])
        self.assertEqual(parsed.hash, parsed.hash.lower())

        arr_download_id = "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"
        self.assertEqual(arr_download_id.lower(), parsed.hash)

    def test_zero_seeds_detected(self):
        parsed = QBittorrentClient._parse(load("qbittorrent_torrents.json")[0])
        self.assertTrue(parsed.is_stalled)
        self.assertTrue(parsed.has_no_seeds)
        self.assertFalse(parsed.is_complete)

    def test_unknown_swarm_count_is_not_treated_as_zero_seeds(self):
        """num_complete == -1 means the tracker withheld the count.

        Reading it as zero would flag every healthy private-tracker torrent as dead.
        """
        parsed = QBittorrentClient._parse(load("qbittorrent_torrents.json")[1])
        self.assertEqual(parsed.num_complete, -1)
        self.assertFalse(parsed.has_no_seeds)
        self.assertFalse(parsed.is_stalled)

    def test_referer_and_origin_headers_are_set(self):
        """qBittorrent's CSRF protection rejects calls without a matching Referer."""
        service = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            variant=ServiceVariant.QBITTORRENT,
            base_url="http://qbt.local:8080",
        )
        headers = QBittorrentClient(service).default_headers()
        self.assertEqual(headers["Referer"], "http://qbt.local:8080")
        self.assertEqual(headers["Origin"], "http://qbt.local:8080")


class PlexPathTests(TestCase):
    def test_parses_nested_media_part_file(self):
        payload = load("plex_metadata.json")
        row = payload["MediaContainer"]["Metadata"][0]
        item = PlexClient._parse_item(row)
        self.assertEqual(item.rating_key, "20481")
        self.assertEqual(
            item.paths,
            ["/movies/Dune Part Two (2024)/Dune Part Two (2024) WEBDL-1080p.mkv"],
        )

    def test_item_without_media_yields_no_paths(self):
        item = PlexClient._parse_item({"ratingKey": "1", "title": "x"})
        self.assertEqual(item.paths, [])

    def test_path_mapping_translates_and_matches(self):
        payload = load("plex_metadata.json")
        item = PlexClient._parse_item(payload["MediaContainer"]["Metadata"][0])
        index = {normalise_path(p): item for p in item.paths}

        arr_path = (
            "/data/media/movies/Dune Part Two (2024)/"
            "Dune Part Two (2024) WEBDL-1080p.mkv"
        )
        mapping = PathMapping(source_prefix="/data/media/movies", target_prefix="/movies")

        unmapped = match_paths([arr_path], index, [])
        self.assertFalse(unmapped.found)

        mapped = match_paths([arr_path], index, [mapping])
        self.assertTrue(mapped.found)
        self.assertEqual(mapped.item, item)

    def test_missing_mapping_surfaces_a_basename_candidate(self):
        """The same filename under a different prefix is evidence of a bad mapping."""
        payload = load("plex_metadata.json")
        item = PlexClient._parse_item(payload["MediaContainer"]["Metadata"][0])
        index = {normalise_path(p): item for p in item.paths}
        arr_path = (
            "/data/media/movies/Dune Part Two (2024)/"
            "Dune Part Two (2024) WEBDL-1080p.mkv"
        )
        result = match_paths([arr_path], index, [])
        self.assertFalse(result.found)
        self.assertTrue(result.basename_candidate)

    def test_suggest_mapping_derives_the_prefix_pair(self):
        suggestion = suggest_mapping(
            "/data/media/movies/Dune (2024)/Dune.mkv",
            "/movies/Dune (2024)/Dune.mkv",
        )
        self.assertEqual(suggestion, ("/data/media/movies", "/movies"))

    def test_suggest_mapping_returns_none_when_identical(self):
        self.assertIsNone(suggest_mapping("/movies/a/b.mkv", "/movies/a/b.mkv"))

    def test_normalise_path_is_case_and_separator_insensitive(self):
        self.assertEqual(
            normalise_path("/Movies/Dune/Dune.MKV"),
            normalise_path("\\movies\\dune\\dune.mkv"),
        )
