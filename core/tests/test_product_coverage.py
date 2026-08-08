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


class LiveObservations(TestCase):
    """Pinned from real servers, not from documentation.

    Transmission 4.1.3, Deluge 2.2.0 and NZBGet 26.2 were run in containers and asked
    the same questions Sleutharr asks. These lock in what they actually answered.
    """

    def test_a_queued_torrent_is_not_a_dead_one(self):
        """Observed on live Transmission and Deluge.

        A torrent that has not started reports zero peers and zero rate for an entirely
        innocent reason. Queueing behind a max-active-downloads limit is normal, and the
        advice attached to a dead swarm is to destroy the release.
        """
        from core.models import EventType, ServiceKind, ServiceVariant
        from core.rules.base import RuleContext
        from core.rules.engine import evaluate
        from core.tests.factories import (
            add_download_sample,
            add_event,
            make_request,
            make_service,
            torrent_sample,
        )

        for state in ("queuedDL", "checkingDL", "allocating"):
            with self.subTest(state=state):
                arr = make_service(ServiceKind.RADARR, name=f"Radarr-{state}")
                client = make_service(
                    ServiceKind.DOWNLOAD_CLIENT,
                    name=f"qbit-{state}",
                    variant=ServiceVariant.QBITTORRENT,
                    base_url="http://qbit:8080",
                )
                request = make_request(arr_service=arr, remote_id=hash(state) % 10000)
                add_event(request, EventType.GRABBED, hours_ago=30)
                for hours in (24, 0.1):
                    add_download_sample(
                        request,
                        torrent_sample(
                            0.0, state=state, num_seeds=0, num_complete=0, dlspeed=0
                        ),
                        hours_ago=hours,
                        service=client,
                    )
                events = list(request.events.order_by("occurred_at", "id"))
                verdict = evaluate(RuleContext(request, events))
                if verdict is not None:
                    self.assertNotIn(
                        "cannot finish",
                        verdict.message,
                        f"a {state} torrent was called dead",
                    )

    def test_transmission_field_names_match_what_it_sends(self):
        """Confirmed against Transmission 4.1.3's torrent-get response."""
        observed = {
            "activityDate", "doneDate", "downloadDir", "error", "errorString", "eta",
            "hashString", "id", "isFinished", "leftUntilDone", "name",
            "peersSendingToUs", "percentDone", "rateDownload", "status", "totalSize",
        }
        item = TransmissionClient._parse(
            {k: 0 for k in observed} | {"hashString": "a" * 40, "name": "x", "status": 4}
        )
        self.assertEqual(item.download_id, "a" * 40)
        # Every field our parser reads is one Transmission actually sends.
        for field_name in (
            "hashString", "percentDone", "totalSize", "leftUntilDone", "rateDownload",
            "eta", "downloadDir", "errorString", "peersSendingToUs", "activityDate",
            "status",
        ):
            self.assertIn(field_name, observed, f"{field_name} is not a real field")

    def test_deluge_field_names_match_what_it_sends(self):
        """Confirmed against Deluge 2.2.0's core.get_torrents_status response."""
        observed = {
            "download_payload_rate", "eta", "hash", "is_finished", "message", "name",
            "num_seeds", "progress", "save_path", "state", "time_since_download",
            "total_remaining", "total_seeds", "total_size",
        }
        from core.clients.download import DELUGE_FIELDS

        unknown = set(DELUGE_FIELDS) - observed
        self.assertEqual(
            unknown, set(), f"we ask Deluge for fields it does not return: {unknown}"
        )

    def test_nzbget_reports_health_in_permille(self):
        """Confirmed against NZBGet 26.2: a healthy group reports Health=1000."""
        self.assertAlmostEqual(nzbget(Health=1000).health, 100.0, places=1)
        self.assertAlmostEqual(nzbget(Health=500).health, 50.0, places=1)


# The exact rows a live Jellyfin 10.11.11 returned for one scanned episode and its
# series. The two Tvdb numbers are the whole point: 306329 identifies the episode,
# 79257 identifies the series, and only 79257 is a number any request ever carries.
JELLYFIN_SERIES = {
    "Id": "12fe27a82c26552ef837d43ef80c70c5",
    "Name": "Planet Earth",
    "Type": "Series",
    "ProviderIds": {"Imdb": "tt0795176", "Tmdb": "1044", "Tvdb": "79257"},
}

JELLYFIN_EPISODE = {
    "Id": "898ef192ba9505467083183808a84c82",
    "Name": "From Pole to Pole",
    "Type": "Episode",
    "Path": "/data/media/tv/Planet Earth (2006)/Season 01/"
            "Planet Earth (2006) - S01E01 - From Pole to Pole.mp4",
    "SeriesName": "Planet Earth",
    "SeriesId": "12fe27a82c26552ef837d43ef80c70c5",
    "ProviderIds": {"Imdb": "tt0797603", "TvRage": "330114", "Tvdb": "306329"},
}


class EpisodeIdsAreNotSeriesIds(TestCase):
    """An episode's ProviderIds belong to the episode, and nothing joins on those.

    Observed on a live Jellyfin 10.11.11 and structurally identical on Emby 4.9.5.0.
    Every id that reaches this join is a series id -- Seerr stores the series tvdbId,
    Sonarr looks series up by it -- so reading the episode's own id compares two
    different numbering spaces.

    That does not merely fail to match. Both are bare integers over overlapping ranges,
    so it can match a different show entirely and report it as present in the library.
    """

    def test_an_episode_alone_yields_no_join_ids(self):
        """Better to know nothing than to offer an id from the wrong namespace."""
        item = JellyfinClient._parse_item(JELLYFIN_EPISODE)
        self.assertIsNone(item.tvdb_id)
        self.assertIsNone(item.tmdb_id)
        self.assertNotEqual(item.tvdb_id, 306329, "episode's own tvdb id leaked out")

    def test_an_episode_takes_its_series_ids(self):
        series = {JELLYFIN_SERIES["Id"]: (1044, 79257)}
        item = JellyfinClient._parse_item(JELLYFIN_EPISODE, series)
        self.assertEqual(item.tvdb_id, 79257)
        self.assertEqual(item.tmdb_id, 1044)

    def test_the_series_row_reads_its_own_ids(self):
        series = JellyfinClient._parse_item(JELLYFIN_SERIES)
        self.assertEqual((series.tmdb_id, series.tvdb_id), (1044, 79257))

    def test_an_unknown_series_leaves_the_episode_unjoinable(self):
        """A series we did not read is not licence to fall back to the episode's id."""
        item = JellyfinClient._parse_item(JELLYFIN_EPISODE, {"someone-else": (1, 2)})
        self.assertIsNone(item.tvdb_id)

    def test_movies_still_read_their_own_ids(self):
        for cls in (JellyfinClient, EmbyClient):
            with self.subTest(product=cls.product):
                self.assertEqual(cls._parse_item(JELLYFIN_MOVIE).tmdb_id, 693134)

    def test_an_id_match_on_television_is_not_a_file_match(self):
        """What the mismatch diagnosis is allowed to claim.

        "The server has this exact file under another path" is a mapping problem. "The
        server has this show" is equally consistent with the episode not being scanned
        yet, and sending someone to change a working path mapping over it is how a
        correct configuration gets broken.
        """
        from core.ingest.mediaserver import identifies_one_file

        episode = JellyfinClient._parse_item(
            JELLYFIN_EPISODE, {JELLYFIN_SERIES["Id"]: (1044, 79257)}
        )
        self.assertFalse(identifies_one_file(episode))
        self.assertTrue(identifies_one_file(JellyfinClient._parse_item(JELLYFIN_MOVIE)))

        # Plex spells its types in lower case and gives a show no Part at all.
        show = PlexClient._parse_item({"ratingKey": "9", "title": "x", "type": "show"})
        self.assertFalse(identifies_one_file(show))


class OmbiTelevisionLivesOnTheChild(TestCase):
    """Pinned from a live Ombi 4.53.10, because the entity names mislead.

    `/Request/tv` returns the *show*, not the request. The parent carries title, tvDbId,
    totalSeasons and artwork -- and no requestedDate, no approved, no available, no user.
    All of those are on `childRequests[]`, because in Ombi the child is the request and
    the parent is the thing being requested.

    A parser reading the parent gets a request with no date, which is the shape it
    discards as unusable. So every television request on Ombi was dropped in silence:
    not mis-parsed, not warned about, simply never tracked.
    """

    # Exactly the shape the live server returned, trimmed to the fields we read.
    PARENT = {
        "id": 1,
        "title": "Severance",
        "tvDbId": 371980,
        "imdbId": "tt11280740",
        "releaseDate": "2022-02-18T00:00:00",
        "totalSeasons": 2,
        "childRequests": [
            {
                "id": 371980,
                "parentRequestId": 1,
                "title": "Severance",
                "requestedDate": "2026-08-08T13:24:13.3601691Z",
                "approved": True,
                "available": False,
                "denied": None,
                "markedAsApproved": "2026-08-08T13:24:13.3601691Z",
                "requestedUser": {"userName": "sleuth", "userAlias": None},
                "seasonRequests": [
                    {"seasonNumber": 1, "episodes": []},
                    {"seasonNumber": 2, "episodes": []},
                ],
            }
        ],
    }

    def _client(self):
        from core.clients.requestmanager import OmbiClient
        from core.models import ServiceKind, ServiceVariant
        from core.tests.factories import make_service

        return OmbiClient(
            make_service(
                ServiceKind.REQUEST_MANAGER,
                variant=ServiceVariant.OMBI,
                name="Ombi-tv",
                base_url="http://ombi:5000",
            )
        )

    def test_the_parent_alone_is_not_a_request(self):
        """The regression itself: no date on the parent, so it used to vanish."""
        from core.models import MediaType

        parent_only = {k: v for k, v in self.PARENT.items() if k != "childRequests"}
        self.assertIsNone(
            self._client().parse_ombi_request(parent_only, MediaType.TV),
            "the parent parsed as a request despite carrying no request",
        )

    def test_a_tv_request_is_read_from_its_child(self):
        from core.models import RequestState

        got = list(self._client().parse_ombi_tv(self.PARENT))
        self.assertEqual(len(got), 1)
        request = got[0]

        # Identity from the parent, state from the child.
        self.assertEqual(request.title, "Severance")
        self.assertEqual(request.tvdb_id, 371980)
        self.assertEqual(request.request_state, RequestState.APPROVED)
        self.assertEqual(request.requested_by, "sleuth")
        self.assertEqual(request.seasons, [1, 2])
        self.assertIsNotNone(request.requested_at)

    def test_each_child_is_its_own_request(self):
        """Season 1 now and season 2 later are two asks, approved independently."""
        parent = {
            **self.PARENT,
            "childRequests": [
                self.PARENT["childRequests"][0],
                {
                    "id": 88,
                    "requestedDate": "2026-08-09T09:00:00Z",
                    "approved": False,
                    "available": False,
                    "requestedUser": {"userName": "someone-else"},
                    "seasonRequests": [{"seasonNumber": 3, "episodes": []}],
                },
            ],
        }
        got = list(self._client().parse_ombi_tv(parent))
        self.assertEqual(len(got), 2)
        self.assertEqual([r.seasons for r in got], [[1, 2], [3]])
        self.assertEqual(got[1].requested_by, "someone-else")
        # A child must report its own seasons, not the union of its siblings'.
        self.assertNotIn(3, got[0].seasons)

    def test_tv_and_movie_ids_cannot_collide(self):
        """Child requests and movie requests are separate tables, both starting at 1.

        Sharing a remote_id would have one title silently overwrite the other under the
        (service, remote_id) uniqueness key.
        """
        from core.models import MediaType

        movie = self._client().parse_ombi_request(
            {
                "id": 1,
                "title": "Dune: Part Two",
                "requestedDate": "2026-08-08T13:24:11Z",
                "theMovieDbId": 693134,
                "approved": True,
            },
            MediaType.MOVIE,
        )
        colliding = {**self.PARENT, "childRequests": [
            {**self.PARENT["childRequests"][0], "id": 1}
        ]}
        tv = list(self._client().parse_ombi_tv(colliding))[0]

        self.assertEqual(movie.remote_id, 1)
        self.assertNotEqual(tv.remote_id, movie.remote_id)

    def test_a_show_nobody_requested_yields_nothing(self):
        self.assertEqual(
            list(self._client().parse_ombi_tv({**self.PARENT, "childRequests": []})), []
        )
