from __future__ import annotations

from django.urls import path

from core import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("request/<int:pk>/", views.request_detail, name="request_detail"),
    path("search/", views.search, name="search"),
    path("health/", views.health, name="health"),
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
    path("poll/", views.run_poll_now, name="run_poll_now"),
    path("api/requests/", views.api_requests, name="api_requests"),
]
