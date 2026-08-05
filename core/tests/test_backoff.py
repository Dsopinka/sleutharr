"""Backoff behaviour.

Regression cover for a cycle-time blowup: a poll cycle touches each *arr in three
separate stages, so a too-short first backoff let one unreachable host be retried by
every stage, turning a single dead service into minutes of stalled cycle.
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from core.clients.base import (
    MAX_BACKOFF_SECONDS,
    MIN_BACKOFF_SECONDS,
    AuthError,
    BaseClient,
    ServiceError,
)
from core.ingest import due_services
from core.models import ServiceKind
from core.tests.factories import make_service


class BackoffTests(TestCase):
    def setUp(self):
        self.service = make_service(ServiceKind.RADARR, name="Radarr")
        self.client_ = BaseClient(self.service)

    def test_first_failure_backs_off_at_least_the_floor(self):
        self.client_.record_failure(ServiceError("boom"))
        self.service.refresh_from_db()
        self.assertEqual(self.service.consecutive_failures, 1)
        remaining = (self.service.backoff_until - timezone.now()).total_seconds()
        self.assertGreater(remaining, MIN_BACKOFF_SECONDS - 5)

    def test_a_failed_service_is_skipped_for_the_rest_of_the_cycle(self):
        """The actual bug: stage 2 and 3 must not retry what stage 1 just failed on."""
        self.client_.record_failure(ServiceError("boom"))
        self.service.refresh_from_db()
        self.assertTrue(self.service.is_backed_off())
        self.assertNotIn(
            self.service.pk, [s.pk for s in due_services(ServiceKind.RADARR)]
        )

    def test_backoff_grows_and_is_capped(self):
        for _ in range(20):
            self.client_.record_failure(ServiceError("boom"))
        self.service.refresh_from_db()
        remaining = (self.service.backoff_until - timezone.now()).total_seconds()
        self.assertLessEqual(remaining, MAX_BACKOFF_SECONDS + 1)

    def test_auth_failure_backs_off_hard(self):
        """A wrong API key does not fix itself, and hammering qBittorrent gets you banned."""
        self.client_.record_failure(AuthError("bad key"))
        self.service.refresh_from_db()
        remaining = (self.service.backoff_until - timezone.now()).total_seconds()
        self.assertGreater(remaining, MAX_BACKOFF_SECONDS - 5)

    def test_success_clears_backoff(self):
        self.client_.record_failure(ServiceError("boom"))
        self.client_.record_success(version="5.14.0")
        self.service.refresh_from_db()
        self.assertEqual(self.service.consecutive_failures, 0)
        self.assertIsNone(self.service.backoff_until)
        self.assertFalse(self.service.is_backed_off())
        self.assertEqual(self.service.version, "5.14.0")

    def test_a_recovered_service_becomes_due_once_its_interval_elapses(self):
        """Success clears the backoff but does not bypass normal per-service pacing."""
        self.client_.record_failure(ServiceError("boom"))
        self.client_.record_success()

        # Just polled, so not due yet -- that is pacing, not backoff.
        self.assertNotIn(
            self.service.pk, [s.pk for s in due_services(ServiceKind.RADARR)]
        )

        self.service.refresh_from_db()
        type(self.service).objects.filter(pk=self.service.pk).update(
            last_attempt_at=timezone.now()
            - timezone.timedelta(seconds=self.service.poll_interval + 1)
        )
        self.assertIn(self.service.pk, [s.pk for s in due_services(ServiceKind.RADARR)])
