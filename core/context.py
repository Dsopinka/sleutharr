"""Template context available on every page."""

from __future__ import annotations

VERSION = "2.12.0"


def app_context(request) -> dict:
    from core.models import ServiceInstance

    unhealthy = ServiceInstance.objects.filter(enabled=True, consecutive_failures__gt=0)
    return {
        "app_version": VERSION,
        "unhealthy_count": unhealthy.count(),
    }
