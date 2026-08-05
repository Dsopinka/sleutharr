"""Poll orchestration.

One cycle walks the services in dependency order -- requests first, because everything
else hangs off a request; then the *arr join and history; then the download client; then
Plex; then diagnosis. Each stage is independently failure-tolerant: a dead Plex must not
stop us reporting that a download stalled.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from core.models import ServiceInstance, ServiceKind

logger = logging.getLogger(__name__)


def due_services(kind: str | None = None) -> list[ServiceInstance]:
    """Enabled services that are not backed off and whose interval has elapsed."""
    qs = ServiceInstance.objects.filter(enabled=True)
    if kind:
        qs = qs.filter(kind=kind)
    now = timezone.now()
    due = []
    for service in qs:
        if service.is_backed_off():
            continue
        last = service.last_attempt_at
        if last is None or (now - last).total_seconds() >= service.poll_interval:
            due.append(service)
    return due


def run_poll_cycle() -> None:
    """Run one full cycle. Never raises."""
    from core.ingest.arr import sync_arr_entities, sync_arr_history, sync_arr_queues
    from core.ingest.download import sync_download_clients
    from core.ingest.mediaserver import sync_media_servers
    from core.ingest.requests import sync_requests
    from core.rules.engine import diagnose_all

    stages = (
        ("requests", sync_requests),
        ("arr entities", sync_arr_entities),
        ("arr history", sync_arr_history),
        ("arr queues", sync_arr_queues),
        ("download clients", sync_download_clients),
        ("media servers", sync_media_servers),
        ("diagnosis", diagnose_all),
    )

    for label, fn in stages:
        try:
            fn()
        except Exception:  # noqa: BLE001 - one broken stage must not stop the rest
            logger.exception("Poll stage %r failed", label)

    logger.debug("Poll cycle complete")


def probe_all() -> None:
    """Health-check every enabled service without ingesting anything."""
    from core.clients import client_for

    for service in ServiceInstance.objects.filter(enabled=True):
        try:
            with client_for(service) as client:
                client.checked_probe()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Probe of %s failed: %s", service.name, exc)


__all__ = ["due_services", "probe_all", "run_poll_cycle", "ServiceKind"]
