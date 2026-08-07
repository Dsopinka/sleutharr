"""Remediation action tests.

These are the only writes Sleutharr performs, so the safety properties get explicit
cover: correct parameters, a stale-id guard, an audit trail on both success and failure,
and no path that acts without a human.
"""

from __future__ import annotations

from unittest import mock

import httpx
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.actions import (
    ActionError,
    describe_remove,
    find_queue_targets,
    remove_from_queue,
    trigger_search,
)
from core.clients.arr import RadarrClient
from core.models import (
    ActionLog,
    ActionStatus,
    AppSetting,
    EventType,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.tests.factories import make_service
from core.tests.test_ingest import mock_client

QUEUE_ROW = {
    "id": 6001,
    "movieId": 412,
    "title": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
    "size": 8698986496.0,
    "sizeleft": 0.0,
    "status": "completed",
    "trackedDownloadStatus": "warning",
    "trackedDownloadState": "importBlocked",
    "statusMessages": [],
    "errorMessage": "",
    "downloadId": "A1B2C3",
    "protocol": "torrent",
    "downloadClient": "qBittorrent",
}


class ActionTestCase(TestCase):
    def setUp(self):
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.radarr = make_service(
            ServiceKind.RADARR, name="Radarr", base_url="http://radarr:7878"
        )
        self.tracked = TrackedRequest.objects.create(
            service=self.seerr,
            remote_id=101,
            title="Dune: Part Two",
            media_type="movie",
            requested_at=timezone.now(),
            arr_service=self.radarr,
            arr_entity_id=412,
        )
        TimelineEvent.objects.create(
            request=self.tracked,
            service=self.radarr,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.IMPORT_BLOCKED,
            occurred_at=timezone.now(),
            summary="Import blocked",
            dedupe_key="arr:1:queue:A1B2C3",
            raw=QUEUE_ROW,
        )
        self.deleted: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deleted.append(request)
            return httpx.Response(200, json={})
        if request.url.path == "/api/v3/queue":
            return httpx.Response(
                200, json={"totalRecords": 1, "records": [QUEUE_ROW]}
            )
        if request.url.path == "/api/v3/command":
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404, json={})

    def run_action(self, fn, *args, **kwargs):
        client = RadarrClient(self.radarr)
        with mock_client(client, self.handler), mock.patch(
            "core.actions.arr_client", return_value=client
        ):
            return fn(*args, **kwargs)


class DescribeTests(ActionTestCase):
    def test_targets_are_read_from_stored_events(self):
        targets = find_queue_targets(self.tracked)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].queue_id, 6001)
        self.assertEqual(targets[0].state, "importBlocked")

    def test_description_spells_out_the_consequences(self):
        """The dialog must state deletion and blocklisting, not just say "remove"."""
        described = describe_remove(self.tracked)
        self.assertTrue(described["can_act"])
        joined = " ".join(described["effects"]).lower()
        self.assertIn("delete", joined)
        self.assertIn("blocklist", joined)

    def test_cannot_act_without_an_arr_link(self):
        self.tracked.arr_service = None
        self.tracked.save()
        described = describe_remove(self.tracked)
        self.assertFalse(described["can_act"])
        self.assertIn("not linked", described["reason"])


class RemoveTests(ActionTestCase):
    def test_sends_the_documented_parameters(self):
        self.run_action(remove_from_queue, self.tracked, 6001)

        self.assertEqual(len(self.deleted), 1)
        params = dict(self.deleted[0].url.params)
        self.assertEqual(params["removeFromClient"], "true")
        self.assertEqual(params["blocklist"], "true")
        # False on purpose: the *arr then searches for a replacement itself, which is
        # why there is no separate search step in this flow.
        self.assertEqual(params["skipRedownload"], "false")
        self.assertIn("/queue/6001", self.deleted[0].url.path)

    def test_success_is_logged_with_detail(self):
        entry = self.run_action(remove_from_queue, self.tracked, 6001)
        self.assertEqual(entry.status, ActionStatus.SUCCESS)
        self.assertIn("blocklisted", entry.detail)
        self.assertEqual(entry.params["queueId"], 6001)
        self.assertEqual(ActionLog.objects.count(), 1)

    def test_stale_queue_id_is_refused(self):
        """A stored queue id may no longer exist; reusing it could delete something else."""
        with self.assertRaises(ActionError) as caught:
            self.run_action(remove_from_queue, self.tracked, 9999)
        self.assertIn("no longer in", str(caught.exception))
        self.assertEqual(self.deleted, [])

    def test_failure_is_also_logged(self):
        with self.assertRaises(ActionError):
            self.run_action(remove_from_queue, self.tracked, 9999)
        entry = ActionLog.objects.get()
        self.assertEqual(entry.status, ActionStatus.FAILED)
        self.assertTrue(entry.error)

    def test_queue_id_belonging_to_another_title_is_refused(self):
        self.tracked.arr_entity_id = 999
        self.tracked.save()
        with self.assertRaises(ActionError) as caught:
            self.run_action(remove_from_queue, self.tracked, 6001)
        self.assertIn("different title", str(caught.exception))
        self.assertEqual(self.deleted, [])

    def test_stale_queue_events_are_cleared_afterwards(self):
        """They describe a download that no longer exists."""
        self.run_action(remove_from_queue, self.tracked, 6001)
        self.assertFalse(
            self.tracked.events.filter(
                event_type__in=[EventType.QUEUED, EventType.IMPORT_BLOCKED]
            ).exists()
        )

    def test_the_action_appears_on_the_timeline(self):
        self.run_action(remove_from_queue, self.tracked, 6001)
        event = self.tracked.events.get(dedupe_key="action:remove:6001")
        self.assertIn("Removed from queue by Sleutharr", event.summary)
        self.assertEqual(event.raw["performedBy"], "sleutharr")


class SearchActionTests(ActionTestCase):
    def test_disabled_by_default(self):
        """Removing already triggers a replacement search, so this is off unless asked."""
        with self.assertRaises(ActionError) as caught:
            self.run_action(trigger_search, self.tracked)
        self.assertIn("disabled", str(caught.exception))

    def test_enabled_sends_the_command(self):
        AppSetting.set("enable_search_action", True)
        entry = self.run_action(trigger_search, self.tracked)
        self.assertEqual(entry.status, ActionStatus.SUCCESS)
        self.assertEqual(entry.params["name"], "MoviesSearch")


class ActionViewTests(ActionTestCase):
    def test_get_is_rejected(self):
        """Destructive actions are POST-only."""
        response = self.client.get(reverse("action_remove", args=[self.tracked.pk]))
        self.assertEqual(response.status_code, 405)

    def test_post_performs_the_action(self):
        client = RadarrClient(self.radarr)
        with mock_client(client, self.handler), mock.patch(
            "core.actions.arr_client", return_value=client
        ):
            response = self.client.post(
                reverse("action_remove", args=[self.tracked.pk]),
                {"queue_id": "6001"},
                headers={"HX-Request": "true"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.deleted), 1)
        self.assertEqual(ActionLog.objects.count(), 1)

    def test_error_is_shown_not_raised(self):
        client = RadarrClient(self.radarr)
        with mock_client(client, self.handler), mock.patch(
            "core.actions.arr_client", return_value=client
        ):
            response = self.client.post(
                reverse("action_remove", args=[self.tracked.pk]),
                {"queue_id": "9999"},
                headers={"HX-Request": "true"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no longer in")

    def test_missing_queue_id_is_handled(self):
        response = self.client.post(
            reverse("action_remove", args=[self.tracked.pk]),
            {},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No queue item")


class NoAutomationTests(TestCase):
    def test_the_poll_cycle_performs_no_actions(self):
        """Guard rail: nothing in the scheduled path may write upstream.

        If someone later wires an action into a poll stage, this fails.
        """
        import inspect

        import core.ingest as ingest_pkg

        source = inspect.getsource(ingest_pkg)
        for name in ("remove_from_queue", "trigger_search", "core.actions"):
            self.assertNotIn(name, source)


class RetryRequestTests(ActionTestCase):
    """NEVER_ADDED is the one diagnosis whose fix lives in the request manager."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/retry"):
            self.retried = getattr(self, "retried", 0) + 1
            return httpx.Response(200, json={"id": 101})
        return httpx.Response(404, json={})

    def run_retry(self):
        from core.clients.requestmanager import SeerrClient

        client = SeerrClient(self.seerr)
        with mock_client(client, self.handler), mock.patch(
            "core.clients.requestmanager.request_manager_client", return_value=client
        ), mock.patch(
            "core.actions.request_manager_client", return_value=client, create=True
        ):
            from core.actions import retry_request

            return retry_request(self.tracked)

    def test_posts_to_the_retry_endpoint(self):
        entry = self.run_retry()
        self.assertEqual(self.retried, 1)
        self.assertEqual(entry.status, ActionStatus.SUCCESS)
        self.assertEqual(entry.params["requestId"], 101)

    def test_records_it_on_the_timeline(self):
        self.run_retry()
        self.assertTrue(
            self.tracked.events.filter(summary="Retried by Sleutharr").exists()
        )

    def test_clears_the_stale_not_in_arr_evidence(self):
        """The verdict was about a state we have just tried to change."""
        TimelineEvent.objects.create(
            request=self.tracked,
            source_kind=ServiceKind.RADARR,
            event_type=EventType.NOT_IN_ARR,
            occurred_at=timezone.now(),
            summary="Not in Radarr",
            dedupe_key="arr:1:missing",
        )
        self.run_retry()
        self.assertFalse(
            self.tracked.events.filter(event_type=EventType.NOT_IN_ARR).exists()
        )

    def test_ombi_is_refused_with_a_reason(self):
        """Ombi has no retry endpoint, so the button must not promise one."""
        from core.actions import ActionError, can_retry

        self.seerr.variant = ServiceVariant.OMBI
        self.seerr.save()
        self.tracked.refresh_from_db()
        self.assertFalse(can_retry(self.tracked))
        with self.assertRaises(ActionError) as caught:
            self.run_retry()
        self.assertIn("no retry endpoint", str(caught.exception))

    def test_view_is_post_only(self):
        response = self.client.get(reverse("action_retry", args=[self.tracked.pk]))
        self.assertEqual(response.status_code, 405)
