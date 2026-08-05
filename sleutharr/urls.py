from __future__ import annotations

from django.conf import settings
from django.urls import include, path
from django.views.static import serve

urlpatterns = [
    path("", include("core.urls")),
]

# Serve static files from the app itself. This is a single-container, single-user tool
# behind whatever proxy the user already runs, so pulling in a dedicated static-file
# middleware would be a dependency without a job.
urlpatterns += [
    path(
        "static/<path:path>",
        serve,
        {"document_root": settings.BASE_DIR / "core" / "static"},
    ),
]
