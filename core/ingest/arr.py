"""Sonarr/Radarr ingestion: entity join, history, queue.

Naming note: `CANONICAL_EVENT` lives in `core.clients.arr` because it is a property of the
wire format, but it is re-exported here since this is where readers look for it.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.utils import timezone

from core.clients.arr import CANONICAL_EVENT, ArrClient, ArrQueueItem, arr_client
from core.clients.base import ServiceError
from core.models import (
    EventType,
    MediaAvailability,
    ServiceInstance,
    ServiceKind,
    TrackedRequest,
)
from core.ingest.events import record_event

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_EVENT",
    "sync_arr_entities",
    "sync_arr_history",
    "sync_arr_queues",
]

# Queue states that mean the *arr has the file but cannot get it into the library.
IMPORT_BLOCKED_STATES = {"importBlocked", "failedPending", "failed"}


def _open_requests() -> "models.QuerySet[TrackedRequest]":
    """Requests still worth polling.

    Available and deleted requests are done -- continuing to poll them would grow load
    linearly with library size forever, for no diagnostic value.
    """
    return TrackedRequest.objects.exclude(
        availability__in=[MediaAvailability.AVAILABLE, MediaAvailability.DELETED]
    ).select_related("arr_service", "service")


# ------------------------------------------------------------------ entity join


def sync_arr_entities() -> None:
    """Resolve each request to its Sonarr/Radarr entity and snapshot its state."""
    by_service: dict[int, list[TrackedRequest]] = defaultdict(list)
    for tracked in _open_requests():
        if tracked.arr_service_id:
            by_service[tracked.arr_service_id].append(tracked)

    for service_id, requests in by_service.items():
        service = ServiceInstance.objects.filter(pk=service_id, enabled=True).first()
        if service is None or service.is_backed_off():
            continue
        try:
            _sync_entities_for_service(service, requests)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Entity sync failed for %s: %s", service.name, exc)


def _sync_entities_for_service(
    service: ServiceInstance, requests: list[TrackedRequest]
) -> None:
    client = arr_client(service)
    try:
        with client:
            try:
                profiles = client.quality_profiles()
            except ServiceError:
                profiles = {}
            for tracked in requests:
                try:
                    _resolve_entity(client, service, tracked, profiles)
                except ServiceError as exc:
                    logger.debug("Entity lookup failed for %s: %s", tracked, exc)
        client.record_success()
    except ServiceError as exc:
        client.record_failure(exc)
    finally:
        client.close()


def _resolve_entity(
    client: ArrClient,
    service: ServiceInstance,
    tracked: TrackedRequest,
    profiles: dict[int, str],
) -> None:
    entity = None

    if tracked.arr_entity_id:
        entity = client.get_entity(tracked.arr_entity_id)

    if entity is None:
        # No externalServiceId, or it no longer resolves. Falling back to tmdb/tvdb is
        # what distinguishes "the request manager never recorded the link" from "the
        # item genuinely is not in the library" -- and only the latter is a diagnosis.
        entity = client.lookup_entity(tmdb_id=tracked.tmdb_id, tvdb_id=tracked.tvdb_id)

    if entity is None:
        tracked.arr_entity_id = None
        tracked.arr_last_synced = timezone.now()
        tracked.save(update_fields=["arr_entity_id", "arr_last_synced"])
        record_event(
            tracked,
            service=service,
            source_kind=service.kind,
            event_type=EventType.NOT_IN_ARR,
            occurred_at=timezone.now(),
            summary=f"No matching entry in {service.name}",
            detail=(
                f"Looked up by externalServiceId={tracked.arr_entity_id or 'null'}, "
                f"tmdbId={tracked.tmdb_id}, tvdbId={tracked.tvdb_id}."
            ),
            dedupe_key=f"arr:{service.pk}:missing",
            raw={"tmdbId": tracked.tmdb_id, "tvdbId": tracked.tvdb_id},
            update_existing=True,
        )
        return

    entity_id = int(entity.get("id") or 0)
    first_link = tracked.arr_entity_id != entity_id

    tracked.arr_entity_id = entity_id
    tracked.arr_title_slug = str(entity.get("titleSlug") or tracked.arr_title_slug)
    tracked.arr_monitored = bool(entity.get("monitored"))
    tracked.arr_quality_profile_id = entity.get("qualityProfileId")
    tracked.arr_quality_profile_name = profiles.get(
        entity.get("qualityProfileId") or -1, ""
    )
    tracked.arr_snapshot = entity
    tracked.arr_last_synced = timezone.now()

    if not tracked.title:
        tracked.title = str(entity.get("title") or "")[:500]
        tracked.year = entity.get("year") or tracked.year

    # A series has no `hasFile`; completeness comes from statistics.
    if service.kind == ServiceKind.SONARR:
        stats = entity.get("statistics") or {}
        have = int(stats.get("episodeFileCount") or 0)
        want = int(stats.get("episodeCount") or 0)
        tracked.arr_has_file = have > 0 and have >= want if want else have > 0
    else:
        tracked.arr_has_file = bool(entity.get("hasFile"))

    tracked.save()

    if first_link:
        added = entity.get("added")
        record_event(
            tracked,
            service=service,
            source_kind=service.kind,
            event_type=EventType.ADDED_TO_ARR,
            occurred_at=_parse(added) or tracked.requested_at,
            summary=f"Present in {service.name} as #{entity_id}",
            detail=f"Quality profile: {tracked.arr_quality_profile_name or 'unknown'}",
            dedupe_key=f"arr:{service.pk}:entity:{entity_id}",
            raw=entity,
        )


def _parse(value):
    from django.utils.dateparse import parse_datetime

    if not value:
        return None
    parsed = parse_datetime(str(value))
    # Sonarr/Radarr use year 1 as a null sentinel for dates like `added`.
    if parsed and parsed.year < 1900:
        return None
    return parsed


# --------------------------------------------------------------------- history


def sync_arr_history() -> None:
    by_service: dict[int, list[TrackedRequest]] = defaultdict(list)
    for tracked in _open_requests():
        if tracked.arr_service_id and tracked.arr_entity_id:
            by_service[tracked.arr_service_id].append(tracked)

    for service_id, requests in by_service.items():
        service = ServiceInstance.objects.filter(pk=service_id, enabled=True).first()
        if service is None or service.is_backed_off():
            continue
        client = arr_client(service)
        try:
            with client:
                for tracked in requests:
                    try:
                        _ingest_entity_history(client, service, tracked)
                    except ServiceError as exc:
                        logger.debug("History failed for %s: %s", tracked, exc)
            client.record_success()
        except ServiceError as exc:
            client.record_failure(exc)
        finally:
            client.close()


def _ingest_entity_history(
    client: ArrClient, service: ServiceInstance, tracked: TrackedRequest
) -> None:
    events = client.entity_history(tracked.arr_entity_id)
    for event in events:
        summary, detail = _describe(event, service)
        record_event(
            tracked,
            service=service,
            source_kind=service.kind,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            summary=summary,
            detail=detail,
            # History ids are stable per instance, which makes this naturally idempotent.
            dedupe_key=f"arr:{service.pk}:history:{event.remote_id}",
            raw=event.raw,
        )


def _describe(event, service: ServiceInstance) -> tuple[str, str]:
    """Human summary. The raw eventType is kept in the detail for traceability."""
    quality = f" [{event.quality}]" if event.quality else ""
    title = event.source_title or "(untitled release)"
    data = event.data or {}

    if event.event_type == EventType.GRABBED:
        indexer = data.get("indexer") or "unknown indexer"
        summary = f"Grabbed {title}{quality}"
        detail = f"Indexer: {indexer}"
        if data.get("downloadClient"):
            detail += f" · Client: {data['downloadClient']}"
    elif event.event_type == EventType.IMPORTED:
        summary = f"Imported {title}{quality}"
        detail = ""
        if data.get("importedPath"):
            detail = f"Imported to: {data['importedPath']}"
        elif data.get("droppedPath"):
            detail = f"From: {data['droppedPath']}"
        if event.quality_cutoff_not_met:
            detail += (
                "\nBelow the quality profile cutoff (qualityCutoffNotMet=true)."
            )
    elif event.event_type == EventType.DOWNLOAD_FAILED:
        summary = f"Download failed: {title}"
        detail = str(data.get("message") or data.get("reason") or "")
    elif event.event_type == EventType.DOWNLOAD_IGNORED:
        summary = f"Download ignored: {title}"
        detail = str(data.get("message") or data.get("reason") or "")
    elif event.event_type == EventType.FILE_DELETED:
        summary = f"File deleted: {title}"
        detail = str(data.get("reason") or "")
    elif event.event_type == EventType.FILE_RENAMED:
        summary = f"File renamed: {title}"
        detail = ""
    else:
        summary = f"{event.raw_event_type or 'Unknown event'}: {title}"
        detail = ""

    detail = (detail + f"\n{service.name} eventType={event.raw_event_type}").strip()
    return summary, detail


# ----------------------------------------------------------------------- queue


def sync_arr_queues() -> None:
    """Snapshot each *arr's queue and attach rows to their requests.

    Queue rows are the only place the *arr surfaces import errors (hardlink and
    permission failures), so they are the evidence rule 4 quotes.
    """
    for service in ServiceInstance.objects.filter(
        enabled=True, kind__in=[ServiceKind.SONARR, ServiceKind.RADARR]
    ):
        if service.is_backed_off():
            continue
        client = arr_client(service)
        try:
            with client:
                items = client.queue()
            client.record_success()
        except ServiceError as exc:
            client.record_failure(exc)
            continue
        finally:
            client.close()

        by_entity: dict[int, list[ArrQueueItem]] = defaultdict(list)
        for item in items:
            if item.entity_id:
                by_entity[int(item.entity_id)].append(item)

        tracked_rows = _open_requests().filter(arr_service=service)
        for tracked in tracked_rows:
            rows = by_entity.get(tracked.arr_entity_id or -1, [])
            _ingest_queue_rows(service, tracked, rows)


def _ingest_queue_rows(
    service: ServiceInstance, tracked: TrackedRequest, rows: list[ArrQueueItem]
) -> None:
    for row in rows:
        blocked = row.tracked_state in IMPORT_BLOCKED_STATES or (
            row.tracked_status == "error"
        )
        messages = row.blocked_messages

        if blocked:
            event_type = EventType.IMPORT_BLOCKED
            summary = f"Import blocked: {row.title}"
            detail = (
                "\n".join(messages)
                or f"trackedDownloadState={row.tracked_state}, status={row.status}"
            )
        else:
            event_type = EventType.QUEUED
            pct = row.progress * 100
            summary = f"In {service.name} queue: {row.title} ({pct:.0f}%)"
            detail = (
                f"status={row.status} · trackedDownloadState={row.tracked_state} · "
                f"protocol={row.protocol}"
            )
            if messages:
                detail += "\n" + "\n".join(messages)

        record_event(
            tracked,
            service=service,
            source_kind=service.kind,
            event_type=event_type,
            occurred_at=timezone.now(),
            summary=summary,
            detail=detail,
            # A queue row is a current reading, not a historical fact: one row per
            # download, refreshed, instead of a new event every poll.
            dedupe_key=f"arr:{service.pk}:queue:{row.download_id or row.remote_id}",
            raw=row.raw,
            update_existing=True,
        )
