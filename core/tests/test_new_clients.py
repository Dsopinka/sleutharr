"""Parsing tests for the services added in round 2.

Each of these locks in a specific way the integration can produce a confidently wrong
verdict, documented in docs/api-notes.md findings 5-11.
"""

from __future__ import annotations

from django.test import TestCase

from core.clients.download import (
    DelugeClient,
    NzbgetClient,
    QBittorrentClient,
    SabnzbdClient,
    TransmissionClient,
    download_client,
)
from core.clients.mediaserver import JellyfinClient, PlexClient, media_server_client
from core.clients.requestmanager import OmbiClient, request_manager_client
from core.models import (
    MediaType,
    RequestState,
    ServiceKind,
    ServiceVariant,
)
from core.tests.factories import make_service


class FactoryTests(TestCase):
    def test_every_variant_maps_to_a_client(self):
        cases = [
            (ServiceKind.REQUEST_MANAGER, ServiceVariant.OMBI, OmbiClient),
            (ServiceKind.MEDIA_SERVER, ServiceVariant.PLEX, PlexClient),
            (ServiceKind.MEDIA_SERVER, ServiceVariant.JELLYFIN, JellyfinClient),
            (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.QBITTORRENT, QBittorrentClient),
            (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.TRANSMISSION, TransmissionClient),
            (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.DELUGE, DelugeClient),
            (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.SABNZBD, SabnzbdClient),
            (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.NZBGET, NzbgetClient),
        ]
        for kind, variant, expected in cases:
            with self.subTest(variant=variant):
                service = make_service(kind, variant=variant, name=f"s-{variant}")
                if kind == ServiceKind.REQUEST_MANAGER:
                    built = request_manager_client(service)
                elif kind == ServiceKind.MEDIA_SERVER:
                    built = media_server_client(service)
                else:
                    built = download_client(service)
                self.assertIsInstance(built, expected)


class OmbiTests(TestCase):
    def setUp(self):
        self.service = make_service(
            ServiceKind.REQUEST_MANAGER,
            variant=ServiceVariant.OMBI,
            base_url="http://ombi:5000",
        )
        self.client_ = OmbiClient(self.service)

    def test_auth_header_is_capitalised_apikey(self):
        """Ombi's header is `ApiKey`, not `X-Api-Key`."""
        self.assertIn("ApiKey", self.client_.default_headers())
        self.assertNotIn("X-Api-Key", self.client_.default_headers())

    def test_parses_a_movie_request(self):
        parsed = self.client_.parse_ombi_request(
            {
                "id": 12,
                "title": "Dune: Part Two",
                "theMovieDbId": 693134,
                "imdbId": "tt15239678",
                "releaseDate": "2024-02-27T00:00:00",
                "requestedDate": "2026-07-01T10:00:00",
                "approved": True,
                "available": False,
                "denied": False,
                "requestedUser": {"userName": "alice"},
            },
            MediaType.MOVIE,
        )
        self.assertEqual(parsed.remote_id, 12)
        self.assertEqual(parsed.title, "Dune: Part Two")
        self.assertEqual(parsed.year, 2024)
        self.assertEqual(parsed.tmdb_id, 693134)
        self.assertEqual(parsed.requested_by, "alice")
        self.assertEqual(parsed.request_state, RequestState.APPROVED)

    def test_boolean_flags_map_onto_the_state_enum(self):
        def state(**flags):
            return self.client_.parse_ombi_request(
                {"id": 1, "requestedDate": "2026-07-01T10:00:00", **flags},
                MediaType.MOVIE,
            ).request_state

        self.assertEqual(state(), RequestState.PENDING)
        self.assertEqual(state(approved=True), RequestState.APPROVED)
        self.assertEqual(state(approved=True, available=True), RequestState.COMPLETED)
        self.assertEqual(state(denied=True), RequestState.DECLINED)

    def test_has_no_service_linkage_and_says_so(self):
        """Ombi records nothing about which *arr record a request became.

        The rules must know this: on Seerr a null externalServiceId is evidence the push
        failed, whereas on Ombi the field never existed and means nothing.
        """
        parsed = self.client_.parse_ombi_request(
            {"id": 1, "requestedDate": "2026-07-01T10:00:00", "theMovieDbId": 5},
            MediaType.MOVIE,
        )
        self.assertIsNone(parsed.keys.external_service_id)
        self.assertIsNone(parsed.keys.service_id)
        self.assertEqual(parsed.keys.rating_key, "")
        self.assertFalse(parsed.is_4k)
        self.assertFalse(OmbiClient.links_to_arr_entity)
        self.assertFalse(OmbiClient.has_4k_lane)

    def test_tv_seasons_come_from_child_requests(self):
        parsed = self.client_.parse_ombi_request(
            {
                "id": 3,
                "requestedDate": "2026-07-01T10:00:00",
                "tvDbId": 371572,
                "childRequests": [
                    {"seasonRequests": [{"seasonNumber": 2}, {"seasonNumber": 3}]}
                ],
            },
            MediaType.TV,
        )
        self.assertEqual(parsed.seasons, [2, 3])
        self.assertEqual(parsed.tvdb_id, 371572)


class JellyfinTests(TestCase):
    def test_path_is_flat_not_nested_like_plex(self):
        item = JellyfinClient._parse_item(
            {
                "Id": "abc123",
                "Name": "Dune: Part Two",
                "Type": "Movie",
                "Path": "/media/movies/Dune (2024)/Dune.mkv",
                "ProviderIds": {"Tmdb": "693134", "Imdb": "tt15239678"},
            }
        )
        self.assertEqual(item.item_id, "abc123")
        self.assertEqual(item.paths, ["/media/movies/Dune (2024)/Dune.mkv"])
        self.assertEqual(item.tmdb_id, 693134)

    def test_provider_id_casing_is_tolerated(self):
        """Provider keys are cased inconsistently across versions and products."""
        item = JellyfinClient._parse_item(
            {"Id": "x", "ProviderIds": {"tmdb": "42", "TVDB": "99"}}
        )
        self.assertEqual(item.tmdb_id, 42)
        self.assertEqual(item.tvdb_id, 99)

    def test_non_numeric_provider_id_is_ignored(self):
        item = JellyfinClient._parse_item({"Id": "x", "ProviderIds": {"Tmdb": ""}})
        self.assertIsNone(item.tmdb_id)

    def test_emby_uses_the_same_token_header(self):
        service = make_service(ServiceKind.MEDIA_SERVER, variant=ServiceVariant.EMBY)
        self.assertIn("X-Emby-Token", JellyfinClient(service).default_headers())


class TransmissionTests(TestCase):
    def test_status_enum_is_the_modern_0_to_6_scheme(self):
        # Transmission 2.40 renumbered these; docs describing 1-16 are the old scheme.
        # A healthy peer and rate are supplied so the stall heuristic does not fire.
        def state(code):
            return TransmissionClient._parse(
                {
                    "hashString": "a",
                    "status": code,
                    "peersSendingToUs": 3,
                    "rateDownload": 5000,
                }
            ).state

        self.assertEqual(state(0), "paused")
        self.assertEqual(state(2), "checking")
        self.assertEqual(state(3), "queued")
        self.assertEqual(state(4), "downloading")
        self.assertEqual(state(6), "seeding")

    def test_hash_is_lowercased_to_match_the_arr_uppercase(self):
        item = TransmissionClient._parse({"hashString": "AABBCC", "status": 4})
        self.assertEqual(item.download_id, "aabbcc")

    def test_no_peers_and_no_rate_reads_as_stalled(self):
        item = TransmissionClient._parse(
            {
                "hashString": "a",
                "status": 4,
                "percentDone": 0.4,
                "peersSendingToUs": 0,
                "rateDownload": 0,
            }
        )
        self.assertEqual(item.state, "stalled")

    def test_swarm_count_stays_unknown(self):
        """Transmission reports only connected peers, never a swarm-wide seed count.

        Faking a swarm count from the peer count would let the zero-seed branch fire on
        a torrent whose swarm is actually healthy.
        """
        item = TransmissionClient._parse(
            {"hashString": "a", "status": 4, "peersSendingToUs": 3, "rateDownload": 100}
        )
        self.assertEqual(item.num_complete, -1)
        self.assertFalse(item.has_no_seeds)


class DelugeTests(TestCase):
    def test_progress_is_rescaled_from_0_100(self):
        """Deluge reports 0-100 where qBittorrent reports 0-1.

        Treating Deluge's scale as a fraction makes every torrent look 100x complete and
        permanently finished, routing stalls into the wrong diagnosis entirely.
        """
        item = DelugeClient._parse("abc", {"hash": "abc", "progress": 42.5})
        self.assertAlmostEqual(item.progress, 0.425)
        self.assertFalse(item.is_complete)

    def test_full_progress_reads_as_complete(self):
        item = DelugeClient._parse(
            "abc", {"hash": "abc", "progress": 100.0, "state": "Seeding"}
        )
        self.assertAlmostEqual(item.progress, 1.0)
        self.assertTrue(item.is_complete)


class SabnzbdTests(TestCase):
    def test_queue_slot_sizes_are_megabytes_as_strings(self):
        item = SabnzbdClient._parse_queue_slot(
            {
                "nzo_id": "SABnzbd_nzo_abc123",
                "filename": "Some.Movie.2024",
                "status": "Downloading",
                "percentage": "42",
                "mb": "8192.5",
                "mbleft": "4096.25",
            }
        )
        self.assertEqual(item.download_id, "sabnzbd_nzo_abc123")
        self.assertAlmostEqual(item.progress, 0.42)
        self.assertEqual(item.size, int(8192.5 * 1024 * 1024))
        self.assertTrue(item.is_usenet)

    def test_usenet_never_reports_no_seeds(self):
        """There is no swarm on usenet; the zero-seed branch must not apply."""
        item = SabnzbdClient._parse_queue_slot({"nzo_id": "x", "mb": "1", "mbleft": "1"})
        self.assertFalse(item.has_no_seeds)

    def test_history_failure_carries_the_message(self):
        item = SabnzbdClient._parse_history_slot(
            {
                "nzo_id": "x",
                "name": "Some.Movie",
                "status": "Failed",
                "fail_message": "Unpacking failed, write error or disk is full?",
                "storage": "/downloads/complete/x",
            }
        )
        self.assertEqual(item.state, "failed")
        self.assertTrue(item.is_errored)
        self.assertIn("disk is full", item.error_message)


class NzbgetTests(TestCase):
    def test_64_bit_sizes_are_reassembled_from_hi_lo(self):
        """A 12 GB file arrives as two 32-bit halves.

        Reading only the Lo half is correct under 4 GiB and silently wrong above it,
        which is exactly the size range this application cares about.
        """
        twelve_gb = 12 * 1024**3
        row = {
            "NZBID": 42,
            "NZBName": "Big.Remux",
            "FileSizeHi": twelve_gb >> 32,
            "FileSizeLo": twelve_gb & 0xFFFFFFFF,
            "RemainingSizeHi": 0,
            "RemainingSizeLo": 0,
            "Status": "DOWNLOADING",
        }
        item = NzbgetClient._parse_group(row)
        self.assertEqual(item.size, twelve_gb)
        # Reading only Lo would give this much smaller, wrong number.
        self.assertNotEqual(item.size, twelve_gb & 0xFFFFFFFF)

    def test_progress_cannot_exceed_one(self):
        item = NzbgetClient._parse_group(
            {
                "NZBID": 1,
                "FileSizeHi": 0,
                "FileSizeLo": 1000,
                "RemainingSizeHi": 0,
                "RemainingSizeLo": 0,
            }
        )
        self.assertLessEqual(item.progress, 1.0)
        self.assertGreaterEqual(item.progress, 0.0)

    def test_download_id_is_a_plain_integer_string(self):
        item = NzbgetClient._parse_group({"NZBID": 42, "FileSizeLo": 1})
        self.assertEqual(item.download_id, "42")

    def test_drone_parameter_is_also_a_candidate_id(self):
        """Sonarr prefers a `drone` post-processing parameter over NZBID when present."""
        ids = NzbgetClient._download_ids(
            {"NZBID": 42, "Parameters": [{"Name": "drone", "Value": "99"}]}
        )
        self.assertIn("42", ids)
        self.assertIn("99", ids)

    def test_health_is_per_mille(self):
        item = NzbgetClient._parse_group({"NZBID": 1, "FileSizeLo": 1, "Health": 850})
        self.assertAlmostEqual(item.health, 85.0)
        self.assertTrue(item.unhealthy_articles)

    def test_history_failure_status(self):
        item = NzbgetClient._parse_history(
            {"NZBID": 7, "Name": "x", "Status": "FAILURE/PAR"}
        )
        self.assertEqual(item.state, "failed")
        self.assertTrue(item.is_errored)


class IdUniquenessTests(TestCase):
    def test_only_torrent_clients_have_globally_unique_ids(self):
        """NZBGet id 42 on one host is a different NZB from id 42 on another."""
        for variant, unique in [
            (ServiceVariant.QBITTORRENT, True),
            (ServiceVariant.TRANSMISSION, True),
            (ServiceVariant.DELUGE, True),
            (ServiceVariant.SABNZBD, False),
            (ServiceVariant.NZBGET, False),
        ]:
            with self.subTest(variant=variant):
                service = make_service(
                    ServiceKind.DOWNLOAD_CLIENT, variant=variant, name=f"c-{variant}"
                )
                self.assertEqual(service.ids_are_globally_unique, unique)

    def test_client_name_falls_back_to_the_service_name(self):
        service = make_service(
            ServiceKind.DOWNLOAD_CLIENT, variant=ServiceVariant.NZBGET, name="NZBGet"
        )
        self.assertEqual(service.client_name, "NZBGet")
        service.arr_client_name = "NZBGet Main"
        self.assertEqual(service.client_name, "NZBGet Main")
