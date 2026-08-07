from __future__ import annotations

from django.urls import path

from core import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("request/<int:pk>/", views.request_detail, name="request_detail"),
    path("search/", views.search, name="search"),
    path("health/", views.health, name="health"),
    # Container liveness probe -- see views.healthz for why it is not the page above.
    path("healthz", views.healthz, name="healthz"),
    path("health/<int:pk>/probe/", views.probe_service, name="probe_service"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/service/", views.service_save, name="service_create"),
    path("settings/service/<int:pk>/", views.service_save, name="service_save"),
    path(
        "settings/service/<int:pk>/delete/",
        views.service_delete,
        name="service_delete",
    ),
    path("settings/mapping/", views.mapping_save, name="mapping_save"),
    path(
        "settings/mapping/<int:pk>/delete/",
        views.mapping_delete,
        name="mapping_delete",
    ),
    path("settings/tunables/", views.tunables_save, name="tunables_save"),
    path("settings/identify/", views.identify_service, name="identify_service"),
    path("settings/plex/start/", views.plex_start, name="plex_start"),
    path("settings/plex/poll/", views.plex_poll, name="plex_poll"),
    path("settings/media-server/", views.media_server_save, name="media_server_save"),
    path("settings/discover/", views.discover, name="discover"),
    path("settings/discover/apply/", views.discover_apply, name="discover_apply"),
    path("request/<int:pk>/remove/", views.action_remove, name="action_remove"),
    path("request/<int:pk>/search/", views.action_search, name="action_search"),
    path("request/<int:pk>/retry/", views.action_retry, name="action_retry"),
    path("request/<int:pk>/why/", views.why_nothing_found, name="why_nothing_found"),
    path(
        "request/<int:pk>/apply-mapping/",
        views.apply_suggested_mapping,
        name="apply_suggested_mapping",
    ),
    path("poll/", views.run_poll_now, name="run_poll_now"),
    path("api/requests/", views.api_requests, name="api_requests"),
]
