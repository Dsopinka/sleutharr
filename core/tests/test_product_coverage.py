"""The same scenarios, run through every product's real parser.

Most of these integrations have never been pointed at a live instance. What *can* be
proved without one is that each product's parser turns that product's documented payload
into the same normalised meaning as every other -- because that normalised meaning is
what the rules reason about, and a parser that disagrees with its peers is exactly how a
confidently wrong verdict gets made.

Every payload below uses field names verified against the product's own schema or API
reference (recorded in docs/api-notes.md), not from memory. The casing and the value
*types* are deliberately faithful: SABnzbd sends numbers as strings, Deluge sends
progress as 0-100, NZBGet sends health in permille and splits 64-bit sizes into Hi/Lo.
Those are precisely the details that break silently.

What this cannot prove: that a given product actually matches its own documentation.
That is what the bug reports are for.
"""

from __future__ import annotations

from django.test import TestCase

from core.clients.download import (
    DelugeClient,
    NzbgetClient,
    QBittorrentClient,
    SabnzbdClient,
    TransmissionClient,
)
from core.clients.mediaserver import EmbyClient, JellyfinClient, PlexClient

# One true size, expressed in whatever unit each product actually uses. SABnzbd and
# NZBGet count in MiB, not MB, so a round MiB figure keeps every fixture exact and
# lets the assertions catch a real units bug instead of tolerating one.
SIZE = 8 * 1024**3          # 8 GiB
LEFT = int(SIZE * 0.58)     # 42% done
MIB = 1024 * 1024

# --------------------------------------------------------------------- payloads
#
# One scenario per row, expressed in each product's own dialect. `parse` returns a
# DownloadItem so the assertions below can talk about meaning rather than field names.


def qbit(**over):
    row = {
        "hash": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "name": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "state": "downloading",
        "progress": 0.42,
        "num_seeds": 5,
        "num_complete": 20,
        "dlspeed": 1048576,
        "amount_left": LEFT,
        "size": SIZE,
        "save_path": "/downloads/",
        "category": "radarr",
    }
    row.update(over)
    return QBittorrentClient._parse(row)


def transmission(**over):
    # Transmission RPC uses camelCase and an integer status: 0 stopped, 1/2 verifying,
    # 3 download-wait, 4 downloading, 5 seed-wait, 6 seeding.
    row = {
        "hashString": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "name": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "status": 4,
        "percentDone": 0.42,
        "totalSize": SIZE,
        "leftUntilDone": LEFT,
        "rateDownload": 1048576,
        "peersSendingToUs": 5,
        "downloadDir": "/downloads",
        "errorString": "",
    }
    row.update(over)
    return TransmissionClient._parse(row)


def deluge(**over):
    # Deluge reports progress as 0-100, unlike everyone else's 0-1.
    row = {
        "hash": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "name": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "state": "Downloading",
        "progress": 42.0,
        "num_seeds": 5,
        "total_seeds": 20,
        "download_payload_rate": 1048576,
        "total_remaining": LEFT,
        "total_size": SIZE,
        "save_path": "/downloads",
        "message": "OK",
    }
    row.update(over)
    return DelugeClient._parse(row["hash"], row)


def sab(**over):
    # SABnzbd sends numbers as strings, and percentage on a 0-100 scale.
    row = {
        "status": "Downloading",
        "nzo_id": "SABnzbd_nzo_abc123",
        "filename": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "percentage": "42",
        "mb": f"{SIZE / MIB:.1f}",
        "mbleft": f"{LEFT / MIB:.1f}",
        "mbmissing": "0.0",
        "cat": "movies",
    }
    row.update(over)
    return SabnzbdClient._parse_queue_slot(row)


def nzbget(**over):
    # NZBGet splits 64-bit sizes into Hi/Lo words and reports health in permille.
    row = {
        "NZBID": 42,
        "NZBName": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "Status": "DOWNLOADING",
        "FileSizeLo": SIZE % (2**32),
        "FileSizeHi": SIZE // (2**32),
        "RemainingSizeLo": LEFT % (2**32),
        "RemainingSizeHi": LEFT // (2**32),
        "DestDir": "/downloads",
        "Category": "movies",
        "Health": 1000,
    }
    row.update(over)
    return NzbgetClient._parse_group(row)


TORRENT_CLIENTS = {
    "qbittorrent": qbit,
    "transmission": transmission,
    "deluge": deluge,
}
USENET_CLIENTS = {
    "sabnzbd": sab,
    "nzbget": nzbget,
}
ALL_CLIENTS = {**TORRENT_CLIENTS, **USENET_CLIENTS}


class EveryClientAgreesOnMeaning(TestCase):
    """Five products, five dialects, one set of conclusions."""

    def test_a_healthy_download_in_progress(self):
        for name, parse in ALL_CLIENTS.items():
            with self.subTest(client=name):
                item = parse()
                # 42% is the single most likely thing to be wrong: three of these five
                # products use a different scale for it.
                self.assertAlmostEqual(item.progress, 0.42, places=2)
                self.assertFalse(item.is_complete)
                self.assertFalse(item.is_errored)
                self.assertFalse(item.is_paused)
                self.assertFalse(item.has_no_seeds)
                self.assertFalse(item.unhealthy_articles)
                self.assertIn("Dune", item.name)
                self.assertTrue(item.download_id)

    def test_size_survives_each_products_units(self):
        """Megabyte strings, byte integers and 64-bit Hi/Lo words, all to bytes."""
        for name, parse in ALL_CLIENTS.items():
            with self.subTest(client=name):
                item = parse()
                self.assertAlmostEqual(item.size / SIZE, 1.0, places=2)

    def test_a_completed_download(self):
        cases = {
            "qbittorrent": qbit(progress=1.0, amount_left=0, state="uploading"),
            "transmission": transmission(percentDone=1.0, leftUntilDone=0, status=6),
            "deluge": deluge(progress=100.0, total_remaining=0, state="Seeding"),
            "sabnzbd": sab(percentage="100", mbleft="0.0"),
            "nzbget": nzbget(RemainingSizeLo=0, RemainingSizeHi=0),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertTrue(item.is_complete, f"{name} did not read as complete")

    def test_a_paused_download(self):
        cases = {
            "qbittorrent": qbit(state="pausedDL"),
            "transmission": transmission(status=0),
            "deluge": deluge(state="Paused"),
            "sabnzbd": sab(status="Paused"),
            "nzbget": nzbget(Status="PAUSED"),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertTrue(item.is_paused, f"{name} did not read as paused")

    def test_an_errored_download(self):
        cases = {
            "qbittorrent": qbit(state="error"),
            "transmission": transmission(errorString="No data found!"),
            "deluge": deluge(state="Error", message="File error"),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertTrue(item.is_errored, f"{name} did not read as errored")

    def test_only_torrents_can_run_out_of_seeds(self):
        """The bug this whole suite exists to prevent."""
        for name, parse in USENET_CLIENTS.items():
            with self.subTest(client=name):
                item = parse()
                self.assertTrue(item.is_usenet)
                self.assertFalse(
                    item.has_no_seeds,
                    f"{name} claimed a swarm it does not have",
                )

    def test_a_dead_torrent_is_recognised_everywhere(self):
        cases = {
            "qbittorrent": qbit(num_seeds=0, num_complete=0, dlspeed=0),
            "transmission": transmission(peersSendingToUs=0, rateDownload=0),
            "deluge": deluge(num_seeds=0, total_seeds=0, download_payload_rate=0),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertTrue(item.has_no_seeds, f"{name} missed a dead torrent")

    def test_an_unknown_swarm_is_not_an_empty_one(self):
        """A private tracker withholding scrape data must not read as zero seeds."""
        cases = {
            "qbittorrent": qbit(num_seeds=8, num_complete=-1, dlspeed=900_000),
            # Transmission never reports a swarm-wide count at all.
            "transmission": transmission(peersSendingToUs=8, rateDownload=900_000),
            "deluge": deluge(num_seeds=8, total_seeds=-1, download_payload_rate=900_000),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertFalse(item.has_no_seeds, f"{name} faked an empty swarm")

    def test_missing_articles_are_detected_on_both_usenet_clients(self):
        """Different fields, different scales, same conclusion.

        SABnzbd reports `mbmissing` in megabytes; NZBGet reports `Health` in permille.
        Both mean "the provider cannot supply this", which is the one usenet case where
        blocklisting the release is genuinely the right advice.
        """
        cases = {
            # 4000 of 8000 MB missing -> 50% available.
            "sabnzbd": sab(mbmissing=f"{SIZE / MIB / 2:.1f}"),
            # 500 permille -> 50%.
            "nzbget": nzbget(Health=500),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertTrue(
                    item.unhealthy_articles, f"{name} missed unrecoverable articles"
                )
                self.assertAlmostEqual(item.health, 50.0, places=0)

    def test_intact_articles_are_not_flagged(self):
        for name, item in {"sabnzbd": sab(), "nzbget": nzbget()}.items():
            with self.subTest(client=name):
                self.assertFalse(item.unhealthy_articles)

    def test_absent_fields_never_invent_a_state(self):
        """A trimmed or older payload must read as unknown, not as broken.

        Every client here is handed a payload with nothing but an id. None of them may
        conclude the download is complete, dead, errored or paused from that.
        """
        cases = {
            "qbittorrent": QBittorrentClient._parse({"hash": "a" * 40}),
            "transmission": TransmissionClient._parse({"hashString": "a" * 40}),
            "deluge": DelugeClient._parse("a" * 40, {"hash": "a" * 40}),
            "sabnzbd": SabnzbdClient._parse_queue_slot({"nzo_id": "x"}),
            "nzbget": NzbgetClient._parse_group({"NZBID": 1}),
        }
        for name, item in cases.items():
            with self.subTest(client=name):
                self.assertFalse(item.is_complete, f"{name} guessed complete")
                self.assertFalse(item.has_no_seeds, f"{name} guessed a dead swarm")
                self.assertFalse(item.unhealthy_articles, f"{name} guessed bad articles")

    def test_facts_expose_the_same_keys_for_every_client(self):
        """Rules read facts by key, so a client omitting one reads as a silent zero."""
        expected = set(qbit().facts())
        for name, parse in ALL_CLIENTS.items():
            with self.subTest(client=name):
                self.assertEqual(set(parse().facts()), expected)


# ------------------------------------------------------------------ media servers


JELLYFIN_MOVIE = {
    # Id is a Guid on Jellyfin and a 32-char hex string on Emby; both are stringified.
    "Id": "7e8a1b2c3d4e4f5a6b7c8d9e0f1a2b3c",
    "Name": "Dune: Part Two",
    "Type": "Movie",
    "Path": "/data/media/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv",
    "ProviderIds": {"Tmdb": "693134", "Imdb": "tt15239678"},
}

PLEX_MOVIE = {
    "ratingKey": "51423",
    "title": "Dune: Part Two",
    "type": "movie",
    "addedAt": 1751370000,
    "Media": [
        {
            "Part": [
                {"file": "/data/media/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"}
            ]
        }
    ],
}


class MediaServersAgreeOnMeaning(TestCase):
    def test_every_server_yields_an_id_a_title_and_a_path(self):
        cases = {
            "plex": PlexClient._parse_item(PLEX_MOVIE),
            "jellyfin": JellyfinClient._parse_item(JELLYFIN_MOVIE),
            "emby": EmbyClient._parse_item(JELLYFIN_MOVIE),
        }
        for name, item in cases.items():
            with self.subTest(server=name):
                self.assertTrue(item.item_id, f"{name} lost the id")
                self.assertEqual(item.title, "Dune: Part Two")
                self.assertEqual(len(item.paths), 1)
                self.assertTrue(item.paths[0].endswith("Dune.Part.Two.2024.mkv"))

    def test_provider_ids_survive_inconsistent_casing(self):
        """Jellyfin, Emby and their versions disagree on how to spell Tmdb."""
        for spelling in ("Tmdb", "TMDB", "tmdb"):
            with self.subTest(spelling=spelling):
                row = {**JELLYFIN_MOVIE, "ProviderIds": {spelling: "693134"}}
                self.assertEqual(JellyfinClient._parse_item(row).tmdb_id, 693134)

    def test_a_non_numeric_provider_id_is_dropped_not_crashed(self):
        row = {**JELLYFIN_MOVIE, "ProviderIds": {"Tmdb": "not-a-number"}}
        self.assertIsNone(JellyfinClient._parse_item(row).tmdb_id)

    def test_media_sources_contribute_extra_paths(self):
        row = {
            **JELLYFIN_MOVIE,
            "MediaSources": [{"Path": "/data/media/movies/other/copy.mkv"}],
        }
        self.assertEqual(len(JellyfinClient._parse_item(row).paths), 2)

    def test_an_item_with_no_path_yields_no_paths(self):
        """An absent path is unknown; recording it as "" would match everything."""
        row = {k: v for k, v in JELLYFIN_MOVIE.items() if k != "Path"}
        self.assertEqual(JellyfinClient._parse_item(row).paths, [])
        plex_row = {k: v for k, v in PLEX_MOVIE.items() if k != "Media"}
        self.assertEqual(PlexClient._parse_item(plex_row).paths, [])


class MediaServerMatchingIsProductAgnostic(TestCase):
    """The join that decides NOT_IN_MEDIA_SERVER and PATH_MISMATCH.

    This has only ever been exercised against Plex payloads. The matching itself works on
    normalised MediaItems, so the thing worth proving is that a Jellyfin- or Emby-shaped
    item reaches it carrying the same information a Plex one does.
    """

    def _index(self, *items):
        # Same shape the client builds internally: normalised path -> item.
        from core.clients.mediaserver import normalise_path

        return {
            normalise_path(path): item
            for item in items
            for path in item.paths
            if path
        }

    def _mapping(self, source_prefix, target_prefix):
        from core.models import PathMapping

        return PathMapping(source_prefix=source_prefix, target_prefix=target_prefix)

    def test_a_mapped_path_matches_on_every_server(self):
        from core.clients.mediaserver import match_paths

        arr_path = "/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"
        server_path = "/data/media/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"
        mapping = self._mapping("/movies", "/data/media/movies")

        for name, item in {
            "plex": PlexClient._parse_item(
                {**PLEX_MOVIE, "Media": [{"Part": [{"file": server_path}]}]}
            ),
            "jellyfin": JellyfinClient._parse_item({**JELLYFIN_MOVIE, "Path": server_path}),
            "emby": EmbyClient._parse_item({**JELLYFIN_MOVIE, "Path": server_path}),
        }.items():
            with self.subTest(server=name):
                result = match_paths([arr_path], self._index(item), [mapping])
                self.assertIsNotNone(result.item, f"{name} failed to match a mapped path")

    def test_an_unmapped_path_is_reported_as_a_mismatch_not_a_miss(self):
        """The file is there; only our prefix is wrong. Saying 'not in Plex' would lie."""
        from core.clients.mediaserver import match_paths, suggest_mapping

        arr_path = "/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"
        server_path = "/data/media/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"

        for name, item in {
            "jellyfin": JellyfinClient._parse_item({**JELLYFIN_MOVIE, "Path": server_path}),
            "emby": EmbyClient._parse_item({**JELLYFIN_MOVIE, "Path": server_path}),
        }.items():
            with self.subTest(server=name):
                result = match_paths([arr_path], self._index(item), mappings=[])
                self.assertIsNone(result.item)
                self.assertTrue(
                    result.basename_candidate,
                    f"{name} lost the evidence that the file exists elsewhere",
                )
                self.assertEqual(
                    suggest_mapping(arr_path, result.basename_candidate),
                    ("/movies", "/data/media/movies"),
                )

    def test_a_genuinely_absent_file_stays_absent(self):
        """The guard against turning every miss into a mapping problem."""
        from core.clients.mediaserver import match_paths

        item = JellyfinClient._parse_item(
            {**JELLYFIN_MOVIE, "Path": "/data/media/movies/Something Else/other.mkv"}
        )
        result = match_paths(
            ["/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"],
            self._index(item),
            mappings=[],
        )
        self.assertIsNone(result.item)
        self.assertFalse(result.basename_candidate)

    def test_windows_paths_match_across_the_separator_change(self):
        """A Windows *arr against a Linux server is a very common Jellyfin setup."""
        from core.clients.mediaserver import match_paths, suggest_mapping

        arr_path = r"D:\Media\Movies\Dune Part Two (2024)\Dune.Part.Two.2024.mkv"
        server_path = "/data/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"
        item = JellyfinClient._parse_item({**JELLYFIN_MOVIE, "Path": server_path})

        result = match_paths([arr_path], self._index(item), mappings=[])
        self.assertTrue(result.basename_candidate)
        suggestion = suggest_mapping(arr_path, result.basename_candidate)
        self.assertIsNotNone(suggestion, "no mapping suggested for a Windows *arr path")
        arr_prefix, server_prefix = suggestion
        self.assertTrue(arr_prefix)
        self.assertTrue(server_prefix)

    def test_a_suggestion_never_collapses_to_the_filesystem_root(self):
        """A mapping of '/' would match everything and is worse than none."""
        from core.clients.mediaserver import suggest_mapping

        suggestion = suggest_mapping(
            "/data/media/movies/X (2024)/X.mkv", "/movies/X (2024)/X.mkv"
        )
        if suggestion is not None:
            arr_prefix, server_prefix = suggestion
            self.assertNotEqual(arr_prefix, "/")
            self.assertNotEqual(server_prefix, "/")
            self.assertTrue(arr_prefix.strip("/"))
            self.assertTrue(server_prefix.strip("/"))


class OmbiRequestsReadCorrectly(TestCase):
    """Ombi is a genuinely different API, not a Seerr dialect.

    It has no status enum -- state is inferred from three independent booleans -- and it
    records no link at all between a request and the *arr record it became. Getting the
    booleans wrong would mark live requests as finished (hiding real failures) or
    finished ones as stuck (inventing them).
    """

    def _client(self):
        from core.clients.requestmanager import OmbiClient
        from core.models import ServiceKind, ServiceVariant
        from core.tests.factories import make_service

        return OmbiClient(
            make_service(
                ServiceKind.REQUEST_MANAGER,
                variant=ServiceVariant.OMBI,
                name="Ombi",
                base_url="http://ombi:5000",
            )
        )

    def _movie(self, **over):
        row = {
            "id": 17,
            "requestedDate": "2026-07-01T10:00:00",
            "title": "Dune: Part Two",
            "releaseDate": "2024-02-27T00:00:00",
            "theMovieDbId": 693134,
            "imdbId": "tt15239678",
            "approved": False,
            "available": False,
            "denied": False,
            "requestedUser": {"userName": "alice"},
        }
        row.update(over)
        return row

    def test_the_three_booleans_map_to_the_right_state(self):
        from core.models import MediaAvailability, MediaType, RequestState

        client = self._client()
        cases = [
            ({}, RequestState.PENDING, MediaAvailability.PENDING),
            ({"approved": True}, RequestState.APPROVED, MediaAvailability.PROCESSING),
            (
                {"approved": True, "available": True},
                RequestState.COMPLETED,
                MediaAvailability.AVAILABLE,
            ),
            (
                {"approved": True, "denied": True},
                RequestState.DECLINED,
                MediaAvailability.PROCESSING,
            ),
        ]
        for over, state, availability in cases:
            with self.subTest(flags=over or "none set"):
                parsed = client.parse_ombi_request(self._movie(**over), MediaType.MOVIE)
                self.assertEqual(parsed.request_state, state)
                self.assertEqual(parsed.keys.availability, availability)

    def test_denied_wins_over_available(self):
        """Both flags set is contradictory; declining is the safer reading."""
        from core.models import MediaType, RequestState

        parsed = self._client().parse_ombi_request(
            self._movie(available=True, denied=True), MediaType.MOVIE
        )
        self.assertEqual(parsed.request_state, RequestState.DECLINED)

    def test_a_request_with_no_date_is_skipped_rather_than_guessed(self):
        from core.models import MediaType

        row = self._movie()
        row.pop("requestedDate")
        self.assertIsNone(self._client().parse_ombi_request(row, MediaType.MOVIE))

    def test_season_numbers_are_collected_from_child_requests(self):
        """Ombi nests seasons two levels down, unlike Seerr's flat list."""
        from core.models import MediaType

        row = self._movie(
            tvDbId=81189,
            childRequests=[
                {"seasonRequests": [{"seasonNumber": 2}, {"seasonNumber": 3}]},
            ],
        )
        parsed = self._client().parse_ombi_request(row, MediaType.TV)
        self.assertEqual(sorted(parsed.seasons), [2, 3])

    def test_year_comes_from_the_release_date(self):
        from core.models import MediaType

        parsed = self._client().parse_ombi_request(self._movie(), MediaType.MOVIE)
        self.assertEqual(parsed.year, 2024)

    def test_a_malformed_release_date_does_not_crash_or_invent_a_year(self):
        from core.models import MediaType

        for bad in ("", "unknown", None):
            with self.subTest(value=bad):
                parsed = self._client().parse_ombi_request(
                    self._movie(releaseDate=bad), MediaType.MOVIE
                )
                self.assertIsNone(parsed.year)

    def test_ombi_never_claims_to_link_arr_entities(self):
        """The absence of a link means nothing on Ombi, and rules must know that.

        On Seerr, a missing externalServiceId is evidence the hand-off failed. On Ombi
        the field does not exist, so treating its absence as evidence would accuse every
        Ombi user's setup of a failure that never happened.
        """
        from core.clients.requestmanager import OmbiClient

        self.assertFalse(getattr(OmbiClient, "links_to_arr_entity", True))
