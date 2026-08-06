"""Helping the user fix things, rather than telling them to go and fix things.

Two mechanisms with very different risk profiles:

* the rejection report is read-only -- it just asks the *arr what it found and why it
  said no;
* the path mapping writes, but only to Sleutharr's own settings, so it needs no
  confirmation and cannot damage anything upstream.
"""

from __future__ import annotations

from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import (
    EventType,
    PathMapping,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.rules.engine import diagnose_request
from core.searchreport import run_search_report
from core.tests.factories import add_event, make_request, make_service
from core.tests.test_ingest import mock_client

RELEASES = [
    {
        "title": "Show.S01.2160p.WEB-DL-A",
        "rejected": True,
        "rejections": ["Quality WEBDL-2160p is larger than the maximum allowed 20 GB"],
    },
    {
        "title": "Show.S01.2160p.WEB-DL-B",
        "rejected": True,
        "rejections": ["Quality WEBDL-2160p is larger than the maximum allowed 20 GB"],
    },
    {
        "title": "Show.S01.1080p-C",
        "rejected": True,
        "rejections": ["Custom Format Score 0 is below minimum 100"],
    },
    {
        "title": "Show.S01.720p-D",
        "rejected": True,
        # A release can be rejected for several reasons at once.
        "rejections": [
            "Custom Format Score 0 is below minimum 100",
            "Not an upgrade for existing episode file(s)",
        ],
    },
]


class SearchReportTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.tracked = make_request(service=self.seerr, arr_service=self.radarr)

    def _report(self, payload):
        from core.clients.arr import RadarrClient

        client = RadarrClient(self.radarr)
        handler = lambda r: httpx.Response(200, json=payload)  # noqa: E731
        with mock_client(client, handler), mock.patch(
            "core.searchreport.arr_client", return_value=client
        ):
            return run_search_report(self.tracked)

    def test_groups_by_reason_most_common_first(self):
        report = self._report(RELEASES)
        self.assertTrue(report.ok)
        self.assertEqual(report.total, 4)
        self.assertEqual(report.accepted, 0)
        self.assertTrue(report.all_rejected)

        top = report.groups[0]
        self.assertEqual(top.count, 2)
        self.assertIn("maximum allowed", top.reason)

    def test_counts_every_reason_on_a_multi_reason_release(self):
        report = self._report(RELEASES)
        reasons = {g.reason: g.count for g in report.groups}
        self.assertEqual(reasons["Custom Format Score 0 is below minimum 100"], 2)
        self.assertEqual(reasons["Not an upgrade for existing episode file(s)"], 1)

    def test_attaches_a_plain_english_hint(self):
        report = self._report(RELEASES)
        size_group = next(g for g in report.groups if "maximum allowed" in g.reason)
        self.assertIn("size limits", size_group.hint.lower())

    def test_shows_example_titles(self):
        report = self._report(RELEASES)
        self.assertTrue(report.groups[0].examples)

    def test_accepted_releases_change_the_conclusion(self):
        payload = RELEASES + [{"title": "Good.Release", "rejected": False}]
        report = self._report(payload)
        self.assertEqual(report.accepted, 1)
        self.assertFalse(report.all_rejected)
        self.assertIn("would be accepted", report.summary)

    def test_empty_result_blames_indexer_coverage_not_settings(self):
        """Nothing found at all is a different problem from everything being filtered."""
        report = self._report([])
        self.assertTrue(report.ok)
        self.assertEqual(report.total, 0)
        self.assertIn("indexer coverage", report.summary)

    def test_unlinked_request_is_refused_cleanly(self):
        self.tracked.arr_service = None
        self.tracked.save()
        report = run_search_report(self.tracked)
        self.assertFalse(report.ok)
        self.assertIn("not linked", report.error)

    def test_service_error_is_reported_not_raised(self):
        from core.clients.arr import RadarrClient

        client = RadarrClient(self.radarr)
        handler = lambda r: httpx.Response(500, json={})  # noqa: E731
        with mock_client(client, handler), mock.patch(
            "core.searchreport.arr_client", return_value=client
        ):
            report = run_search_report(self.tracked)
        self.assertFalse(report.ok)
        self.assertTrue(report.error)


class SearchReportViewTests(SearchReportTests):
    def test_is_post_only(self):
        response = self.client.get(reverse("why_nothing_found", args=[self.tracked.pk]))
        self.assertEqual(response.status_code, 405)

    def test_renders_the_grouped_reasons(self):
        from core.clients.arr import RadarrClient

        client = RadarrClient(self.radarr)
        handler = lambda r: httpx.Response(200, json=RELEASES)  # noqa: E731
        with mock_client(client, handler), mock.patch(
            "core.searchreport.arr_client", return_value=client
        ):
            response = self.client.post(
                reverse("why_nothing_found", args=[self.tracked.pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "maximum allowed")
        self.assertContains(response, "size limits")


class ApplyMappingTests(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.plex = make_service(
            ServiceKind.MEDIA_SERVER, name="Plex", variant=ServiceVariant.PLEX
        )
        self.tracked = make_request(
            service=self.seerr, arr_service=self.radarr, has_file=True
        )
        add_event(self.tracked, EventType.IMPORTED, hours_ago=10, raw={})
        TimelineEvent.objects.create(
            request=self.tracked,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_AVAILABLE,
            occurred_at=timezone.now(),
            summary="In Plex but no mapping resolves",
            dedupe_key="mediaserver:1:path_mismatch",
            raw={
                "arrPaths": ["/data/media/movies/Dune (2024)/Dune.mkv"],
                "serverPaths": ["/movies/Dune (2024)/Dune.mkv"],
            },
        )

    def test_adds_the_mapping_it_worked_out(self):
        response = self.client.post(
            reverse("apply_suggested_mapping", args=[self.tracked.pk]),
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        mapping = PathMapping.objects.get()
        self.assertEqual(mapping.source_prefix, "/data/media/movies")
        self.assertEqual(mapping.target_prefix, "/movies")
        self.assertEqual(
            mapping.apply("/data/media/movies/X/X.mkv"), "/movies/X/X.mkv"
        )

    def test_applying_twice_does_not_duplicate(self):
        for _ in range(2):
            self.client.post(
                reverse("apply_suggested_mapping", args=[self.tracked.pk]),
                headers={"HX-Request": "true"},
            )
        self.assertEqual(PathMapping.objects.count(), 1)

    def test_without_a_mismatch_event_it_says_so(self):
        self.tracked.events.filter(dedupe_key__endswith=":path_mismatch").delete()
        response = self.client.post(
            reverse("apply_suggested_mapping", args=[self.tracked.pk]),
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No mapping could be worked out")
        self.assertFalse(PathMapping.objects.exists())

    def test_is_post_only(self):
        response = self.client.get(
            reverse("apply_suggested_mapping", args=[self.tracked.pk])
        )
        self.assertEqual(response.status_code, 405)

    def test_touches_no_upstream_service(self):
        """It edits Sleutharr's own config, which is why it needs no confirmation."""
        with mock.patch("core.clients.client_for") as client_for:
            self.client.post(
                reverse("apply_suggested_mapping", args=[self.tracked.pk]),
                headers={"HX-Request": "true"},
            )
        client_for.assert_not_called()
