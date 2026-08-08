"""A download that is behaving should say so.

Built from a live instance: a 26 GB film showed "Still checking -- Sleutharr has not
worked this one out yet" while its own timeline showed 36%, article health 100% and
eleven minutes left. The verdict was not wrong, there just was not one, because no rule
claims a request that is working. It read as confusion, and it put a healthy download in
the list of things needing attention.

The payloads below are real SABnzbd queue slots copied from that instance, run through
the real parser -- inventing the normalised facts here would keep passing if the parser
broke, which is the failure that let usenet downloads be diagnosed as dead torrents.
"""

from __future__ import annotations

from django.test import TestCase

from core.models import EventType, ServiceKind, ServiceVariant
from core.rules.engine import diagnose_request
from core.tests.factories import (
    add_download_sample,
    add_event,
    make_request,
    make_service,
)

TOTAL_MB = "26900.60"


def sab_slot(percentage, mbleft, *, status="Downloading", mbmissing="0.71", **over):
    """A SABnzbd queue slot, in the exact shape and types SABnzbd sends."""
    slot = {
        "status": status,
        "nzo_id": "5909c196-6173-4bd5-a4ae-11fe00938ed7",
        "filename": "In.Time.2011.BluRay.2160p.AI.Upscale.DTS-HD.MA.5.1.SDR.10Bit.x265-ZAX",
        "percentage": str(percentage),
        "mb": TOTAL_MB,
        "mbleft": str(mbleft),
        "mbmissing": mbmissing,
        "cat": "movies",
        "timeleft": "0:11:36",
    }
    slot.update(over)
    return slot


class ADownloadThatIsMovingIsAnAnswer(TestCase):
    def setUp(self):
        self.arr = make_service(ServiceKind.RADARR, name="Radarr")
        self.sab = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="sab",
            variant=ServiceVariant.SABNZBD,
            base_url="http://sab:8080",
        )
        self.request_ = make_request(arr_service=self.arr, remote_id=20)
        add_event(self.request_, EventType.GRABBED, hours_ago=26)

    def _sample(self, slot, hours_ago):
        add_download_sample(
            self.request_, slot, usenet=True, hours_ago=hours_ago, service=self.sab
        )

    def _clear_samples(self):
        self.request_.events.filter(event_type=EventType.DOWNLOAD_PROGRESS).delete()

    def _code(self):
        diagnosis = diagnose_request(self.request_)
        return diagnosis.code if diagnosis else None

    def test_a_progressing_download_is_reported_as_progressing(self):
        """The live case, start to finish."""
        self._sample(sab_slot(0, TOTAL_MB, mbmissing="0.00"), 26)
        self._sample(sab_slot(10, "24001.58"), 1)
        self._sample(sab_slot(36, "17186.95"), 0.05)

        diagnosis = diagnose_request(self.request_)
        self.assertIsNotNone(diagnosis, "a healthy download produced no verdict at all")
        self.assertEqual(diagnosis.code, "DOWNLOAD_IN_PROGRESS")
        self.assertIn("36%", diagnosis.message)
        self.assertIn("downloading normally", diagnosis.message)
        self.assertIn("16.8 GB", diagnosis.message)

    def test_it_is_filed_as_nothing_to_do(self):
        """A working download does not belong in the list of things needing a look."""
        from core.views import WAITING_CODES

        self.assertIn("DOWNLOAD_IN_PROGRESS", WAITING_CODES)

    def test_it_runs_last_so_a_fault_always_wins(self):
        from core.rules import RULES

        self.assertEqual(RULES[-1].code, "DOWNLOAD_IN_PROGRESS")

    def test_a_download_that_has_not_moved_says_nothing(self):
        """Stuck at the same percentage is not progress, whatever the status says."""
        self._sample(sab_slot(36, "17186.95"), 3)
        self._sample(sab_slot(36, "17186.95"), 0.05)
        self.assertNotEqual(self._code(), "DOWNLOAD_IN_PROGRESS")

    def test_a_paused_download_is_not_progress(self):
        self._sample(sab_slot(10, "24001.58"), 3)
        self._sample(sab_slot(36, "17186.95", status="Paused"), 0.05)
        self.assertNotEqual(self._code(), "DOWNLOAD_IN_PROGRESS")

    def test_missing_articles_are_not_progress(self):
        """Article health is the usenet dead-swarm signal; rule 5 owns that verdict."""
        self._sample(sab_slot(10, "24001.58"), 3)
        self._sample(sab_slot(36, "17186.95", mbmissing="4000.0"), 0.05)
        self.assertNotEqual(self._code(), "DOWNLOAD_IN_PROGRESS")

    def test_a_finished_download_is_left_to_the_import_rules(self):
        self._sample(sab_slot(50, "13450.30"), 3)
        self._sample(sab_slot(100, "0.00"), 0.05)
        self.assertNotEqual(self._code(), "DOWNLOAD_IN_PROGRESS")

    def test_a_client_that_stopped_answering_silences_it(self):
        """Otherwise "it is downloading" becomes a claim about our own stale records."""
        self._sample(sab_slot(10, "24001.58"), 3)
        self._sample(sab_slot(36, "17186.95"), 0.05)
        self.assertEqual(self._code(), "DOWNLOAD_IN_PROGRESS")

        self.sab.consecutive_failures = 3
        self.sab.save(update_fields=["consecutive_failures"])
        self.assertNotEqual(self._code(), "DOWNLOAD_IN_PROGRESS")
