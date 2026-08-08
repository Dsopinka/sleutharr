"""Sleutharr checks the media server itself, so it can know better than the request
manager -- and when it does, it should say so.

From a live instance: Radarr imported the film, Plex was serving it at a path Sleutharr
had matched, and Seerr still said Processing. The request sat on the dashboard with no
diagnosis, which reads as confusion about an item that is plainly finished. The cause
was in Seerr's own log -- its recently-added Plex scan was erroring -- and deferring to
Seerr's status hid that completely.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.models import (
    EventType,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.rules.engine import diagnose_request
from core.tests.factories import make_service

PLEX_PATH = "/media/MOVIES/In Time (2011)/In Time (2011).mkv"


class ArrivedButNotReflectedTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.plex = make_service(
            ServiceKind.MEDIA_SERVER,
            name="DaveFlix",
            variant=ServiceVariant.PLEX,
            base_url="http://plex:32400",
        )
        self.request_ = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=56,
            title="In Time (2011)",
            media_type="movie",
            tmdb_id=49530,
            # Seerr's view: still outstanding.
            availability="processing",
            requested_at=timezone.now() - timezone.timedelta(days=1),
            arr_service=self.radarr,
            arr_entity_id=245,
            arr_has_file=True,
            media_server_found=True,
            media_server_matched_path=PLEX_PATH,
        )
        TimelineEvent.objects.create(
            request=self.request_,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.IMPORTED,
            occurred_at=timezone.now() - timezone.timedelta(hours=4),
            summary="Imported",
            dedupe_key="arr:1:history:616",
            raw={"data": {"importedPath": PLEX_PATH}},
        )

    def _found(self, hours_ago=3):
        return TimelineEvent.objects.create(
            request=self.request_,
            service=self.plex,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_AVAILABLE,
            occurred_at=timezone.now() - timezone.timedelta(hours=hours_ago),
            summary="Present in Plex: In Time",
            detail=f"Matched path: {PLEX_PATH}",
            dedupe_key=f"mediaserver:{self.plex.pk}:found",
        )

    def _code(self):
        diagnosis = diagnose_request(self.request_)
        return diagnosis.code if diagnosis else None

    def test_the_live_case(self):
        self._found()
        diagnosis = diagnose_request(self.request_)
        self.assertIsNotNone(diagnosis, "an arrived file produced no verdict at all")
        self.assertEqual(diagnosis.code, "ARRIVED_NOT_REFLECTED")
        self.assertIn("already arrived", diagnosis.message.lower())
        # Named by product, as every other rule does -- the user's own label for the
        # box ("DaveFlix") means nothing to someone reading advice about Plex.
        self.assertIn("Plex", diagnosis.message)
        self.assertIn("Seerr", diagnosis.message)
        self.assertIn(PLEX_PATH, diagnosis.message)
        # The actionable half: the fault is in the request manager, not the file.
        self.assertIn("logs", diagnosis.next_step.lower())

    def test_it_waits_out_the_normal_scan_delay(self):
        """A few minutes behind is how request managers work, not a fault."""
        self._found(hours_ago=0.1)
        self.assertNotEqual(self._code(), "ARRIVED_NOT_REFLECTED")

    def test_a_path_mismatch_is_not_an_arrival(self):
        """media_server_found is also set when only the id join matched.

        That proves the server knows the title, not that this file resolves -- claiming
        it had arrived would contradict the mismatch diagnosis that fires for it.
        """
        TimelineEvent.objects.create(
            request=self.request_,
            service=self.plex,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_AVAILABLE,
            occurred_at=timezone.now() - timezone.timedelta(hours=3),
            summary="In Plex, but no configured path mapping resolves to it",
            dedupe_key=f"mediaserver:{self.plex.pk}:path_mismatch",
            raw={"serverPaths": ["/movies/x.mkv"], "arrPaths": [PLEX_PATH]},
        )
        self.request_.media_server_matched_path = ""
        self.request_.save(update_fields=["media_server_matched_path"])
        self.assertEqual(self._code(), "PATH_MISMATCH")

    def test_an_unreachable_media_server_cannot_vouch_for_it_either(self):
        """A stale match would assert an arrival that may since have been deleted."""
        self._found()
        self.assertEqual(self._code(), "ARRIVED_NOT_REFLECTED")

        self.plex.consecutive_failures = 4
        self.plex.save(update_fields=["consecutive_failures"])
        self.assertNotEqual(self._code(), "ARRIVED_NOT_REFLECTED")

    def test_a_fulfilled_request_never_reaches_this(self):
        """Once the request manager catches up, the verdict must clear itself."""
        self._found()
        self.assertEqual(self._code(), "ARRIVED_NOT_REFLECTED")

        self.request_.availability = "available"
        self.request_.save(update_fields=["availability"])
        self.assertEqual(
            self._code(),
            "FULFILLED",
            "the verdict survived the request manager catching up",
        )
