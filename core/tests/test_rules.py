"""Rule tests against fixture timelines.

Every rule gets both a positive case and at least one negative case. The negative cases
matter more: a rule that fires when it should not sends the user to fix something that
is not broken, which is worse than no diagnosis at all.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import (
    Diagnosis,
    EventType,
    MediaAvailability,
    RequestState,
    ServiceKind,
    ServiceVariant,
    Severity,
)
from core.rules.base import RuleContext
from core.rules.engine import diagnose_request, evaluate
from core.tests.factories import add_event, make_request, make_service, torrent_sample


class RuleTestCase(TestCase):
    def setUp(self):
        self.radarr = make_service(
            ServiceKind.RADARR, name="Radarr", base_url="http://radarr:7878"
        )
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER,
            name="Seerr",
            variant=ServiceVariant.SEERR,
            base_url="http://seerr:5055",
        )

    def verdict_for(self, request):
        request.refresh_from_db()
        events = list(request.events.order_by("occurred_at", "id"))
        return evaluate(RuleContext(request, events))


class NeverAddedTests(RuleTestCase):
    def test_approved_with_no_entity_is_never_added(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, arr_entity_id=None
        )
        add_event(request, EventType.NOT_IN_ARR, hours_ago=1)
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NEVER_ADDED")
        self.assertEqual(verdict.severity, Severity.ERROR)
        self.assertIn("Radarr", verdict.message)

    def test_failed_request_state_gets_the_handoff_message(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            arr_entity_id=None,
            request_state=RequestState.FAILED,
        )
        add_event(request, EventType.REQUEST_FAILED, hours_ago=2)
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NEVER_ADDED")
        self.assertIn("FAILED", verdict.message)
        self.assertIn("root folder", verdict.next_step)
        self.assertTrue(verdict.evidence)

    def test_pending_approval_is_not_a_fault(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            arr_entity_id=None,
            request_state=RequestState.PENDING,
        )
        self.assertIsNone(self.verdict_for(request))

    def test_declined_is_reported_as_info_not_error(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            arr_entity_id=None,
            request_state=RequestState.DECLINED,
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "DECLINED")
        self.assertEqual(verdict.severity, Severity.INFO)

    def test_no_arr_instance_is_a_distinct_code(self):
        request = make_request(service=self.seerr, arr_service=None, arr_entity_id=None)
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NO_ARR_INSTANCE")

    def test_entity_present_does_not_fire(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, arr_entity_id=412
        )
        add_event(request, EventType.GRABBED, hours_ago=5)
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "NEVER_ADDED")


class UnmonitoredTests(RuleTestCase):
    def test_unmonitored_fires(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=False
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "UNMONITORED")
        self.assertIn("never be searched", verdict.message)

    def test_unmonitored_with_file_is_normal(self):
        """Monitoring is routinely switched off once a file is present."""
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            monitored=False,
            has_file=True,
        )
        add_event(request, EventType.IMPORTED, hours_ago=200)
        verdict = self.verdict_for(request)
        self.assertNotEqual(verdict.code if verdict else "", "UNMONITORED")

    def test_monitored_does_not_fire(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=True
        )
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "UNMONITORED")


class BlocklistLoopTests(RuleTestCase):
    def test_repeated_failures_trip_the_loop(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i in range(3):
            add_event(
                request,
                EventType.GRABBED,
                hours_ago=20 - i * 4,
                raw={"sourceTitle": "Bad.Release-GROUP"},
            )
            add_event(
                request,
                EventType.DOWNLOAD_FAILED,
                hours_ago=19 - i * 4,
                raw={"sourceTitle": "Bad.Release-GROUP"},
            )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "BLOCKLIST_LOOP")
        self.assertIn("same release", verdict.message)

    def test_distinct_releases_give_infrastructure_advice(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i in range(3):
            add_event(
                request,
                EventType.DOWNLOAD_FAILED,
                hours_ago=19 - i * 4,
                raw={"sourceTitle": f"Release.{i}-GROUP"},
            )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "BLOCKLIST_LOOP")
        self.assertIn("disk space", verdict.next_step)

    def test_below_threshold_does_not_fire(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i in range(2):
            add_event(request, EventType.DOWNLOAD_FAILED, hours_ago=10 - i)
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "BLOCKLIST_LOOP")

    def test_successful_import_after_failures_clears_it(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i in range(4):
            add_event(request, EventType.DOWNLOAD_FAILED, hours_ago=20 - i)
        add_event(request, EventType.IMPORTED, hours_ago=1, raw={})
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "BLOCKLIST_LOOP")


class DownloadedNotImportedTests(RuleTestCase):
    def test_import_blocked_quotes_the_arr_message(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(request, EventType.GRABBED, hours_ago=8)
        add_event(
            request,
            EventType.IMPORT_BLOCKED,
            hours_ago=2,
            detail=(
                "Importing failed, path does not exist or is not accessible by "
                "Radarr: /downloads/complete/movies/X"
            ),
            raw={"trackedDownloadState": "importBlocked"},
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "DOWNLOADED_NOT_IMPORTED")
        self.assertIn("not accessible by Radarr", verdict.message)

    def test_hardlink_error_gets_targeted_advice(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.IMPORT_BLOCKED,
            hours_ago=2,
            detail="Could not hardlink file, falling back to copy: cross-device link",
        )
        verdict = self.verdict_for(request)
        self.assertIn("different filesystems", verdict.next_step)

    def test_permission_error_gets_targeted_advice(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.IMPORT_BLOCKED,
            hours_ago=2,
            detail="Access to the path is denied",
        )
        verdict = self.verdict_for(request)
        self.assertIn("PUID/PGID", verdict.next_step)

    def test_complete_torrent_with_no_import_fires(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(request, EventType.GRABBED, hours_ago=8)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=1,
            raw=torrent_sample(1.0, state="uploading", amount_left=0),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "DOWNLOADED_NOT_IMPORTED")

    def test_import_after_the_block_clears_it(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(request, EventType.IMPORT_BLOCKED, hours_ago=5)
        add_event(request, EventType.IMPORTED, hours_ago=1, raw={})
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "DOWNLOADED_NOT_IMPORTED")


class GrabbedButStalledTests(RuleTestCase):
    def _stalled_request(self, **sample):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(request, EventType.GRABBED, hours_ago=30)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=24,
            raw=torrent_sample(0.42, **sample),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.1,
            raw=torrent_sample(0.4205, **sample),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        return request

    def test_no_progress_over_the_window_is_stalled(self):
        request = self._stalled_request(state="stalledDL")
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "GRABBED_BUT_STALLED")
        self.assertIn("stalled", verdict.message)

    def test_zero_seeds_fires_without_waiting_for_the_window(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.2,
            raw=torrent_sample(0.3, num_seeds=0, num_complete=0, dlspeed=0),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "GRABBED_BUT_STALLED")
        self.assertIn("cannot finish", verdict.message)

    def test_unknown_swarm_count_does_not_fire_zero_seed_branch(self):
        """num_complete == -1 is 'unknown', not 'no seeds'."""
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.2,
            raw=torrent_sample(0.3, num_seeds=8, num_complete=-1, dlspeed=900000),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "GRABBED_BUT_STALLED")

    def test_client_error_state_is_its_own_code(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.2,
            raw=torrent_sample(0.5, state="missingFiles"),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "DOWNLOAD_CLIENT_ERROR")

    def test_healthy_progress_does_not_fire(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=12,
            raw=torrent_sample(0.2),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.1,
            raw=torrent_sample(0.85),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "GRABBED_BUT_STALLED")


class NotInMediaServerTests(RuleTestCase):
    def setUp(self):
        super().setUp()
        # This whole rule is about what a media server does or does not have, so there
        # has to be one. Without it the rule correctly stays silent -- see
        # NoMediaServerConfiguredTests.
        self.plex = make_service(
            ServiceKind.MEDIA_SERVER,
            name="Plex",
            variant=ServiceVariant.PLEX,
            base_url="http://plex:32400",
        )

    def test_imported_but_absent_after_grace_fires(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.media_server_found = False
        request.save()
        add_event(request, EventType.IMPORTED, hours_ago=6, raw={})
        add_event(
            request,
            EventType.MEDIA_SERVER_MISSING,
            hours_ago=0.1,
            source_kind=ServiceKind.MEDIA_SERVER,
            raw={"arrPaths": ["/data/media/movies/X/X.mkv"]},
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NOT_IN_MEDIA_SERVER")

    def test_within_grace_period_does_not_fire(self):
        """Plex scans on its own schedule; a fresh import is not a fault."""
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.media_server_found = False
        request.save()
        add_event(request, EventType.IMPORTED, hours_ago=0.2, raw={})
        verdict = self.verdict_for(request)
        self.assertIsNone(verdict)

    def test_path_mismatch_fires_even_though_media_server_found_is_true(self):
        """Regression: the mismatch branch must not sit behind the media_server_found guard.

        The ingester sets media_server_found=True whenever *either* join succeeds, and in the
        mismatch case the ratingKey join does succeed. An early `if media_server_found: return`
        therefore made this diagnosis unreachable in production while still passing a
        test that set media_server_found=False by hand.
        """
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.media_server_found = True
        request.media_server_item_id = "20481"
        request.save()
        add_event(request, EventType.IMPORTED, hours_ago=10, raw={})
        add_event(
            request,
            EventType.MEDIA_SERVER_AVAILABLE,
            hours_ago=0.1,
            source_kind=ServiceKind.MEDIA_SERVER,
            dedupe_key="plex:1:path_mismatch",
            raw={
                "arrPaths": ["/data/media/movies/X/X.mkv"],
                "plexPaths": ["/movies/X/X.mkv"],
            },
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "PATH_MISMATCH")

    def test_path_mismatch_is_a_separate_diagnosis(self):
        """Plex has it, but our mapping does not resolve -- a different fix entirely."""
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.media_server_found = False
        request.save()
        add_event(request, EventType.IMPORTED, hours_ago=10, raw={})
        add_event(
            request,
            EventType.MEDIA_SERVER_AVAILABLE,
            hours_ago=0.1,
            source_kind=ServiceKind.MEDIA_SERVER,
            dedupe_key="plex:1:path_mismatch",
            raw={
                "arrPaths": ["/data/media/movies/X/X.mkv"],
                "plexPaths": ["/movies/X/X.mkv"],
            },
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "PATH_MISMATCH")
        self.assertIn("/movies/X/X.mkv", verdict.message)

    def test_found_in_plex_does_not_fire(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.media_server_found = True
        request.save()
        add_event(request, EventType.IMPORTED, hours_ago=10, raw={})
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "NOT_IN_MEDIA_SERVER")


class WrongQualityTests(RuleTestCase):
    def test_below_cutoff_fires(self):
        request = make_request(service=self.seerr, arr_service=self.radarr, has_file=True)
        request.media_server_found = True
        request.save()
        add_event(
            request,
            EventType.IMPORTED,
            hours_ago=5,
            raw={
                "qualityCutoffNotMet": True,
                "quality": {"quality": {"name": "SDTV"}},
            },
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "WRONG_QUALITY")
        self.assertIn("SDTV", verdict.message)
        self.assertIn("HD-1080p", verdict.message)

    def test_cutoff_met_does_not_fire(self):
        request = make_request(service=self.seerr, arr_service=self.radarr, has_file=True)
        request.media_server_found = True
        request.save()
        add_event(
            request,
            EventType.IMPORTED,
            hours_ago=5,
            raw={
                "qualityCutoffNotMet": False,
                "quality": {"quality": {"name": "WEBDL-1080p"}},
            },
        )
        self.assertIsNone(self.verdict_for(request))


class NoReleaseFoundTests(RuleTestCase):
    def test_monitored_with_no_grabs_fires(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            days_ago=14,
            snapshot={"isAvailable": True, "lastSearchTime": "2026-07-20T10:00:00Z"},
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NO_RELEASE_FOUND")
        self.assertIn("HD-1080p", verdict.message)

    def test_unreleased_movie_is_info_not_a_fault(self):
        """A film still in cinemas has no release to find."""
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            days_ago=14,
            snapshot={
                "isAvailable": False,
                "minimumAvailability": "released",
                "inCinemas": "2026-07-15T00:00:00Z",
                "digitalRelease": "2026-10-01T00:00:00Z",
            },
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NOT_RELEASED_YET")
        self.assertEqual(verdict.severity, Severity.INFO)
        self.assertIn("Oct 2026", verdict.message)

    def test_never_searched_is_a_distinct_code(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            days_ago=14,
            snapshot={"isAvailable": True, "lastSearchTime": None},
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NEVER_SEARCHED")

    def test_recent_request_is_given_time(self):
        request = make_request(
            service=self.seerr,
            arr_service=self.radarr,
            days_ago=0,
            snapshot={"isAvailable": True, "lastSearchTime": "2026-07-20T10:00:00Z"},
        )
        self.assertIsNone(self.verdict_for(request))

    def test_grabbed_does_not_fire(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, days_ago=14,
            snapshot={"isAvailable": True, "lastSearchTime": "2026-07-20T10:00:00Z"},
        )
        add_event(request, EventType.GRABBED, hours_ago=10)
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "NO_RELEASE_FOUND")


class EnginePersistenceTests(RuleTestCase):
    def test_diagnosis_is_saved_with_evidence(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=False
        )
        event = add_event(request, EventType.ADDED_TO_ARR, hours_ago=40)
        diagnosis = diagnose_request(request)
        self.assertEqual(diagnosis.code, "UNMONITORED")
        self.assertIn(event, list(diagnosis.evidence.all()))

    def test_available_request_is_marked_fulfilled(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        request.availability = MediaAvailability.AVAILABLE
        request.save()
        diagnosis = diagnose_request(request)
        self.assertEqual(diagnosis.code, "FULFILLED")
        self.assertEqual(diagnosis.severity, Severity.OK)

    def test_stale_diagnosis_is_cleared_when_nothing_matches(self):
        """A verdict that no longer applies is worse than no verdict."""
        request = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=False
        )
        self.assertIsNotNone(diagnose_request(request))
        self.assertTrue(Diagnosis.objects.filter(request=request).exists())

        # The user turned monitoring back on and re-requested; nothing is wrong now.
        request.arr_monitored = True
        request.requested_at = timezone.now()
        request.save()

        self.assertIsNone(diagnose_request(request))
        self.assertFalse(Diagnosis.objects.filter(request=request).exists())

    def test_first_match_wins_ordering(self):
        """An unmonitored item that also looks stalled reports the root cause."""
        request = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=False
        )
        add_event(
            request,
            EventType.DOWNLOAD_PROGRESS,
            hours_ago=0.1,
            raw=torrent_sample(0.3, num_seeds=0, num_complete=0, dlspeed=0),
            source_kind=ServiceKind.DOWNLOAD_CLIENT,
        )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "UNMONITORED")


class NoMediaServerConfiguredTests(RuleTestCase):
    """Found on a live instance: it claimed a file was missing from Plex when the user
    had never configured any media server. Absence of evidence is not evidence."""

    def _imported_request(self):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, has_file=True
        )
        add_event(request, EventType.IMPORTED, hours_ago=600, raw={})
        return request

    def test_silent_when_no_media_server_is_configured(self):
        request = self._imported_request()
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "NOT_IN_MEDIA_SERVER")

    def test_fires_once_a_media_server_exists(self):
        request = self._imported_request()
        make_service(
            ServiceKind.MEDIA_SERVER, name="Plex", variant=ServiceVariant.PLEX
        )
        request.media_server_found = False
        request.save()
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "NOT_IN_MEDIA_SERVER")

    def test_message_names_the_configured_product_not_plex(self):
        request = self._imported_request()
        make_service(
            ServiceKind.MEDIA_SERVER, name="Media", variant=ServiceVariant.JELLYFIN
        )
        request.media_server_found = False
        request.save()
        verdict = self.verdict_for(request)
        self.assertIn("Jellyfin", verdict.message)
        self.assertNotIn("Plex", verdict.message)

    def test_disabled_media_server_does_not_count(self):
        request = self._imported_request()
        service = make_service(
            ServiceKind.MEDIA_SERVER, name="Plex", variant=ServiceVariant.PLEX
        )
        service.enabled = False
        service.save()
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "NOT_IN_MEDIA_SERVER")


class QualityNoiseTests(RuleTestCase):
    """Found on a live instance: a 2160p HDR WEB-DL was reported as a quality problem.

    `qualityCutoffNotMet` is true for most well-configured libraries -- a cutoff of
    Bluray-2160p flags every WEBDL-2160p file. Only a real drop in resolution matters.
    """

    def _imported(self, resolution, cutoff, name="WEBDL-2160p"):
        request = make_request(
            service=self.seerr, arr_service=self.radarr, has_file=True
        )
        request.media_server_found = True
        request.arr_cutoff_resolution = cutoff
        request.save()
        add_event(
            request,
            EventType.IMPORTED,
            hours_ago=5,
            raw={
                "qualityCutoffNotMet": True,
                "quality": {"quality": {"name": name, "resolution": resolution}},
            },
        )
        return request

    def test_same_resolution_below_cutoff_is_not_reported(self):
        """WEBDL-2160p under a Bluray-2160p cutoff is a source preference, not a fault."""
        verdict = self.verdict_for(self._imported(2160, 2160))
        self.assertNotEqual(getattr(verdict, "code", None), "WRONG_QUALITY")

    def test_higher_resolution_than_cutoff_is_not_reported(self):
        verdict = self.verdict_for(self._imported(2160, 1080))
        self.assertNotEqual(getattr(verdict, "code", None), "WRONG_QUALITY")

    def test_genuine_resolution_drop_still_fires(self):
        verdict = self.verdict_for(self._imported(480, 1080, name="SDTV"))
        self.assertEqual(verdict.code, "WRONG_QUALITY")
        self.assertIn("480p", verdict.message)
        self.assertIn("1080p", verdict.message)

    def test_unknown_cutoff_falls_back_to_the_arr_flag(self):
        """With no cutoff resolution known, trust the *arr rather than stay silent."""
        verdict = self.verdict_for(self._imported(720, None, name="HDTV-720p"))
        self.assertEqual(verdict.code, "WRONG_QUALITY")


class SeasonPackCountingTests(RuleTestCase):
    """Sonarr writes one history row per episode.

    A single failed season pack therefore produces one row per episode, which counted
    naively trips a loop threshold of 3 on the very first failure.
    """

    def _season_pack_failure(self, request, download_id, episodes=8, hours_ago=10):
        for index in range(episodes):
            add_event(
                request,
                EventType.DOWNLOAD_FAILED,
                hours_ago=hours_ago,
                raw={
                    "downloadId": download_id,
                    "sourceTitle": "Some.Show.S05.2160p.WEB-DL-GROUP",
                },
            )

    def test_one_failed_season_pack_is_not_a_loop(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        self._season_pack_failure(request, "AAAA")
        verdict = self.verdict_for(request)
        self.assertNotEqual(getattr(verdict, "code", None), "BLOCKLIST_LOOP")

    def test_three_distinct_attempts_still_trip_it(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i, download_id in enumerate(["AAAA", "BBBB", "CCCC"]):
            self._season_pack_failure(request, download_id, hours_ago=20 - i * 4)
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "BLOCKLIST_LOOP")
        # Reports attempts, not the 24 underlying rows.
        self.assertIn("3 failure(s)", verdict.message)

    def test_missing_download_id_falls_back_to_release_and_day(self):
        request = make_request(service=self.seerr, arr_service=self.radarr)
        for i in range(3):
            add_event(
                request,
                EventType.DOWNLOAD_FAILED,
                hours_ago=60 - i * 24,
                raw={"sourceTitle": f"Release.{i}-GROUP"},
            )
        verdict = self.verdict_for(request)
        self.assertEqual(verdict.code, "BLOCKLIST_LOOP")
