"""View smoke tests: every page renders, with and without data."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.models import (
    EventType,
    PathMapping,
    ServiceInstance,
    ServiceKind,
    ServiceVariant,
)
from core.rules.engine import diagnose_request
from core.tests.factories import add_event, make_request, make_service


class EmptyStateTests(TestCase):
    def test_dashboard_renders_with_no_services(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No services configured")

    def test_all_pages_render_empty(self):
        for name in ("dashboard", "search", "health", "settings"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class PopulatedTests(TestCase):
    def setUp(self):
        self.radarr = make_service(ServiceKind.RADARR, name="Radarr")
        self.seerr = make_service(
            ServiceKind.REQUEST_MANAGER, name="Seerr", variant=ServiceVariant.SEERR
        )
        self.request_ = make_request(
            service=self.seerr, arr_service=self.radarr, monitored=False
        )
        add_event(self.request_, EventType.ADDED_TO_ARR, hours_ago=50)
        diagnose_request(self.request_)

    def test_dashboard_shows_the_diagnosis_badge(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "UNMONITORED")
        self.assertContains(response, "Dune: Part Two")

    def test_dashboard_filters_by_code(self):
        response = self.client.get(reverse("dashboard"), {"code": "UNMONITORED"})
        self.assertContains(response, "Dune: Part Two")
        response = self.client.get(reverse("dashboard"), {"code": "NEVER_ADDED"})
        self.assertNotContains(response, "Dune: Part Two")

    def test_detail_renders_timeline_and_evidence(self):
        response = self.client.get(
            reverse("request_detail", args=[self.request_.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "UNMONITORED")
        self.assertContains(response, "Timeline")
        self.assertContains(response, "evidence")

    def test_search_by_requester(self):
        response = self.client.get(reverse("search"), {"q": "alice"})
        self.assertContains(response, "Dune: Part Two")

    def test_search_by_diagnosis_code(self):
        response = self.client.get(reverse("search"), {"q": "UNMONITORED"})
        self.assertContains(response, "Dune: Part Two")

    def test_api_returns_json(self):
        response = self.client.get(reverse("api_requests"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["diagnosis"]["code"], "UNMONITORED")


class SettingsTests(TestCase):
    def test_create_service(self):
        response = self.client.post(
            reverse("service_create"),
            {
                "name": "Radarr 4K",
                "kind": ServiceKind.RADARR,
                "variant": ServiceVariant.NATIVE,
                "base_url": "http://radarr4k:7878/",
                "api_key": "abc123",
                "poll_interval": "90",
                "remote_service_id": "1",
                "enabled": "on",
                "is_4k": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = ServiceInstance.objects.get(name="Radarr 4K")
        self.assertEqual(service.api_key, "abc123")
        self.assertEqual(service.remote_service_id, 1)
        self.assertTrue(service.is_4k)
        self.assertEqual(service.poll_interval, 90)
        self.assertEqual(service.url, "http://radarr4k:7878")

    def test_blank_api_key_on_edit_keeps_the_stored_one(self):
        """The form renders secrets masked, so submitting must not wipe a working key."""
        service = make_service(ServiceKind.RADARR, name="Radarr")
        service.api_key = "original"
        service.save()

        self.client.post(
            reverse("service_save", args=[service.pk]),
            {
                "name": "Radarr",
                "kind": ServiceKind.RADARR,
                "variant": ServiceVariant.NATIVE,
                "base_url": service.base_url,
                "api_key": "",
                "enabled": "on",
            },
        )
        service.refresh_from_db()
        self.assertEqual(service.api_key, "original")

    def test_missing_required_fields_rejected(self):
        response = self.client.post(
            reverse("service_create"),
            {"name": "", "kind": ServiceKind.RADARR, "base_url": ""},
        )
        self.assertEqual(response.status_code, 400)

    def test_path_mapping_crud(self):
        self.client.post(
            reverse("mapping_save"),
            {
                "source_prefix": "/data/media/movies",
                "target_prefix": "/movies",
                "note": "radarr to plex",
                "order": "0",
            },
        )
        mapping = PathMapping.objects.get()
        self.assertEqual(
            mapping.apply("/data/media/movies/X/X.mkv"), "/movies/X/X.mkv"
        )
        self.client.post(reverse("mapping_delete", args=[mapping.pk]))
        self.assertFalse(PathMapping.objects.exists())

    def test_tunables_save(self):
        from core.models import AppSetting

        self.client.post(reverse("tunables_save"), {"stall_hours": "12"})
        self.assertEqual(AppSetting.get("stall_hours"), 12)


class HealthProbeTests(TestCase):
    def test_probe_of_an_unreachable_service_renders_a_row_not_a_500(self):
        service = make_service(
            ServiceKind.RADARR, name="Down", base_url="http://127.0.0.1:1"
        )
        response = self.client.post(reverse("probe_service", args=[service.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "failing")
        service.refresh_from_db()
        self.assertGreater(service.consecutive_failures, 0)


class HealthzTests(TestCase):
    def test_returns_ok_without_rendering_a_template(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_stays_cheap_as_services_are_added(self):
        """The probe must not scale with the size of the setup.

        It replaced a check that rendered the full /health/ page, whose cost grew with
        every configured service and was timing out the container healthcheck.
        """
        for i in range(25):
            make_service(ServiceKind.RADARR, name=f"Radarr {i}")
        with self.assertNumQueries(1):
            self.client.get(reverse("healthz"))
