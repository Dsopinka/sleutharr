"""The download join must be scoped to the client that issued the id.

This is invisible with one download client and produces confidently wrong verdicts with
two. NZBGet hands out small integers that are unique only within a single instance, so a
global "ask everyone about every id" lookup will match request A's NZBID 42 against a
completely unrelated NZB on a second NZBGet. See docs/api-notes.md finding 8.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.ingest.download import _refs_for_service, sync_download_clients
from core.models import (
    EventType,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.tests.factories import make_service


class ScopingTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.nzbget_a = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="NZBGet A",
            variant=ServiceVariant.NZBGET,
            base_url="http://nzbget-a:6789",
        )
        self.nzbget_b = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="NZBGet B",
            variant=ServiceVariant.NZBGET,
            base_url="http://nzbget-b:6789",
        )
        self.qbt = make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="qBittorrent",
            variant=ServiceVariant.QBITTORRENT,
            base_url="http://qbt:8080",
        )

    def _request(self, remote_id: int, download_id: str, client_name: str):
        tracked = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=remote_id,
            media_type="movie",
            requested_at=timezone.now(),
        )
        TimelineEvent.objects.create(
            request=tracked,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.QUEUED,
            occurred_at=timezone.now(),
            summary="queued",
            dedupe_key=f"arr:1:queue:{remote_id}",
            raw={"downloadId": download_id, "downloadClient": client_name},
        )
        return tracked

    def test_nzbget_id_is_not_offered_to_the_other_nzbget(self):
        """The core bug: id 42 means different things on different hosts."""
        tracked = self._request(1, "42", "NZBGet A")

        from core.ingest.download import _refs_by_request

        mapping = _refs_by_request()

        wanted_a = _refs_for_service(self.nzbget_a, mapping)
        wanted_b = _refs_for_service(self.nzbget_b, mapping)

        self.assertEqual(wanted_a[tracked.pk], {"42"})
        # NZBGet B must be asked nothing at all -- it never saw this download.
        self.assertNotIn(tracked.pk, wanted_b)

    def test_unattributed_id_reaches_torrent_clients_only(self):
        """Older history rows sometimes omit the client name.

        Falling back to a global lookup is safe for infohashes, which are globally
        unique, but never for usenet ids.
        """
        tracked = self._request(2, "a1b2c3", "")

        from core.ingest.download import _refs_by_request

        mapping = _refs_by_request()

        self.assertEqual(_refs_for_service(self.qbt, mapping)[tracked.pk], {"a1b2c3"})
        self.assertNotIn(tracked.pk, _refs_for_service(self.nzbget_a, mapping))

    def test_client_name_match_is_case_insensitive(self):
        tracked = self._request(3, "77", "nzbget a")

        from core.ingest.download import _refs_by_request

        mapping = _refs_by_request()
        self.assertEqual(_refs_for_service(self.nzbget_a, mapping)[tracked.pk], {"77"})

    def test_arr_client_name_override_is_used(self):
        """The *arr's name for a client often differs from what we call it here."""
        self.nzbget_a.arr_client_name = "Usenet Primary"
        self.nzbget_a.save()
        tracked = self._request(4, "9", "Usenet Primary")

        from core.ingest.download import _refs_by_request

        mapping = _refs_by_request()
        self.assertEqual(_refs_for_service(self.nzbget_a, mapping)[tracked.pk], {"9"})

    def test_wrong_client_is_never_queried(self):
        """End to end: the client that did not take the download is not even contacted."""
        self._request(5, "42", "NZBGet A")

        called: list[str] = []

        def fake_client(service):
            client = mock.MagicMock()
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: None

            def items_by_id(ids):
                called.append(service.name)
                return {}

            client.items_by_id = items_by_id
            return client

        with mock.patch("core.ingest.download.download_client", side_effect=fake_client):
            sync_download_clients()

        self.assertIn("NZBGet A", called)
        self.assertNotIn("NZBGet B", called)
        self.assertNotIn("qBittorrent", called)

    def test_arr_uppercase_hash_matches_client_lowercase(self):
        """The *arr uppercases infohashes; every client reports them lowercase."""
        tracked = self._request(6, "A1B2C3D4E5F6", "qBittorrent")

        from core.ingest.download import _refs_by_request

        mapping = _refs_by_request()
        self.assertEqual(
            _refs_for_service(self.qbt, mapping)[tracked.pk], {"a1b2c3d4e5f6"}
        )
