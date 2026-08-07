"""Ingest requests from the request manager.

This is the root of every trace: no request, nothing to diagnose.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.clients.base import ServiceError
from core.clients.requestmanager import NormalisedRequest, request_manager_client
from core.ingest import due_services
from core.ingest.events import record_event
from core.models import (
    AppSetting,
    EventType,
    IngestCursor,
    MediaAvailability,
    RequestState,
    ServiceInstance,
    ServiceKind,
    TrackedRequest,
)

logger = logging.getLogger(__name__)


def sync_requests() -> None:
    for service in due_services(ServiceKind.REQUEST_MANAGER):
        try:
            sync_service_requests(service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Request sync failed for %s: %s", service.name, exc)


def _reconcile_due(cursor: IngestCursor) -> bool:
    """Whether it is time for a full walk that can notice deletions.

    The normal sync stops as soon as it recognises records, which is what keeps it cheap
    -- but it also means a request deleted upstream is never revisited and lingers here
    forever. A periodic complete walk is the only way to see an absence.
    """
    minutes = float(AppSetting.get("reconcile_minutes", 30) or 30)
    last = cursor.last_reconcile
    return last is None or (timezone.now() - last) >= timedelta(minutes=minutes)


def sync_service_requests(service: ServiceInstance, force_reconcile: bool = False) -> int:
    """Pull requests for one request-manager instance.

    On first run this walks the full history back to the configured cutoff. Afterwards it
    stops early once it reaches records it already has -- results are sorted newest-first,
    so the first run of already-seen records means everything older is also stored.

    Periodically it instead walks everything, so that requests deleted in the request
    manager can be removed here too.
    """
    client = request_manager_client(service)
    cursor, _ = IngestCursor.objects.get_or_create(service=service, scope="requests")
    backfill_days = int(AppSetting.get("backfill_days", 90))
    cutoff = timezone.now() - timedelta(days=backfill_days)

    reconciling = force_reconcile or _reconcile_due(cursor)
    seen_existing_streak = 0
    processed = 0
    seen_ids: set[int] = set()
    walked_everything = False

    try:
        with client:
            for parsed in client.iter_requests(newest_first=True):
                # Backfill boundary. Requests older than the cutoff are out of scope.
                if parsed.requested_at < cutoff and cursor.backfill_complete:
                    break
                if parsed.requested_at < cutoff:
                    cursor.backfill_complete = True
                    break

                seen_ids.add(parsed.remote_id)
                tracked, created, changed = _upsert(service, parsed, client)
                processed += 1

                if not created and not changed:
                    seen_existing_streak += 1
                    # 25 consecutive untouched records means we have caught up. Not 1 --
                    # an edited older request can appear among newer ones. Skipped while
                    # reconciling, which has to see every record to trust an absence.
                    if (
                        not reconciling
                        and cursor.backfill_complete
                        and seen_existing_streak >= 25
                    ):
                        break
                else:
                    seen_existing_streak = 0

            else:
                # Ran out of pages without breaking: we have seen the whole list.
                cursor.backfill_complete = True
                walked_everything = True

        client.record_success()
    except ServiceError as exc:
        client.record_failure(exc)
        raise
    finally:
        client.close()

    removed = 0
    if reconciling and walked_everything:
        removed = _remove_deleted(service, seen_ids, cutoff)
        cursor.last_reconcile = timezone.now()

    cursor.high_water = timezone.now()
    cursor.save(
        update_fields=[
            "high_water",
            "backfill_complete",
            "last_reconcile",
            "updated_at",
        ]
    )
    logger.info(
        "%s: processed %d requests%s",
        service.name,
        processed,
        f", removed {removed} deleted upstream" if removed else "",
    )
    return processed


def _remove_deleted(
    service: ServiceInstance, seen_ids: set[int], cutoff
) -> int:
    """Drop local requests that no longer exist in the request manager.

    Only ever called after a walk that completed without error and without stopping
    early, because deleting on the strength of a partial list would wipe real data on a
    transient failure.

    Scoped to the backfill window as well: anything older than the cutoff was never
    walked, so its absence from `seen_ids` means nothing.
    """
    stale = TrackedRequest.objects.filter(
        service=service, requested_at__gte=cutoff
    ).exclude(remote_id__in=seen_ids)

    titles = list(stale.values_list("title", flat=True)[:10])
    count = stale.count()
    if not count:
        return 0

    stale.delete()
    logger.info(
        "%s: removed %d request(s) deleted upstream: %s",
        service.name,
        count,
        ", ".join(t or "(untitled)" for t in titles),
    )
    return count


@transaction.atomic
def _upsert(
    service: ServiceInstance, parsed: NormalisedRequest, client
) -> tuple[TrackedRequest, bool, bool]:
    tracked = TrackedRequest.objects.filter(
        service=service, remote_id=parsed.remote_id
    ).first()
    created = tracked is None
    if tracked is None:
        tracked = TrackedRequest(service=service, remote_id=parsed.remote_id)

    before = (tracked.request_state, tracked.availability, tracked.arr_entity_id)

    tracked.media_type = parsed.media_type
    tracked.requested_at = parsed.requested_at
    tracked.updated_at_remote = parsed.updated_at
    tracked.requested_by = parsed.requested_by
    tracked.request_state = parsed.request_state
    tracked.availability = parsed.keys.availability
    tracked.tmdb_id = parsed.tmdb_id
    tracked.tvdb_id = parsed.tvdb_id
    tracked.imdb_id = parsed.imdb_id
    tracked.is_4k = parsed.is_4k
    tracked.requested_seasons = parsed.seasons
    tracked.arr_title_slug = parsed.keys.external_service_slug
    tracked.media_server_item_id = parsed.keys.rating_key
    tracked.last_polled = timezone.now()
    tracked.raw = parsed.raw

    # Route to the owning *arr instance using the request manager's serviceId. This is
    # the only reliable route when several instances are configured -- and it must use
    # the 4K-aware key, already resolved in resolve_service_keys().
    tracked.arr_service = _match_arr_service(parsed, tracked)
    if parsed.keys.external_service_id:
        tracked.arr_entity_id = parsed.keys.external_service_id

    if not tracked.title and parsed.tmdb_id:
        title, year = client.fetch_title(parsed.media_type, parsed.tmdb_id)
        if title:
            tracked.title = title
            tracked.year = year

    tracked.save()

    after = (tracked.request_state, tracked.availability, tracked.arr_entity_id)
    changed = before != after

    if created:
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.REQUEST_MANAGER,
            event_type=EventType.REQUESTED,
            occurred_at=parsed.requested_at,
            summary=f"Requested by {parsed.requested_by or 'unknown'}",
            detail=(
                f"{parsed.media_type} · "
                f"{'4K' if parsed.is_4k else 'standard'} lane"
                + (f" · seasons {parsed.seasons}" if parsed.seasons else "")
            ),
            dedupe_key=f"request:{parsed.remote_id}:created",
            raw=parsed.raw,
        )

    _record_state_events(tracked, parsed, service)
    return tracked, created, changed


def _record_state_events(
    tracked: TrackedRequest, parsed: NormalisedRequest, service: ServiceInstance
) -> None:
    """Emit lifecycle events for terminal request states."""
    when = parsed.updated_at or parsed.requested_at

    if parsed.request_state == RequestState.APPROVED:
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.REQUEST_MANAGER,
            event_type=EventType.APPROVED,
            occurred_at=when,
            summary="Approved",
            dedupe_key=f"request:{parsed.remote_id}:approved",
            raw={"status": parsed.request_state},
        )
    elif parsed.request_state == RequestState.DECLINED:
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.REQUEST_MANAGER,
            event_type=EventType.DECLINED,
            occurred_at=when,
            summary="Declined",
            dedupe_key=f"request:{parsed.remote_id}:declined",
            raw={"status": parsed.request_state},
        )
    elif parsed.request_state == RequestState.FAILED:
        # The request manager sets FAILED when the push to Sonarr/Radarr errored --
        # the most direct possible evidence for the "never added" diagnosis.
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.REQUEST_MANAGER,
            event_type=EventType.REQUEST_FAILED,
            occurred_at=when,
            summary="Request manager reports the request FAILED",
            detail="The push to Sonarr/Radarr did not succeed.",
            dedupe_key=f"request:{parsed.remote_id}:failed",
            raw={"status": parsed.request_state},
        )

    if parsed.keys.availability == MediaAvailability.BLOCKLISTED:
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.REQUEST_MANAGER,
            event_type=EventType.DOWNLOAD_IGNORED,
            occurred_at=when,
            summary="Media is blocklisted in the request manager",
            dedupe_key=f"request:{parsed.remote_id}:blocklisted",
            raw={"status": parsed.keys.availability},
        )


def _match_arr_service(
    parsed: NormalisedRequest, tracked: TrackedRequest
) -> ServiceInstance | None:
    """Find the configured Sonarr/Radarr instance this request was sent to.

    Preference order:
      1. exact `serviceId` match on the correct (4K or standard) lane;
      2. any instance of the right kind on the matching 4K lane;
      3. any instance of the right kind.

    Falling back is better than giving up -- most setups have one instance per kind, and
    an unrouted request produces no diagnosis at all.
    """
    kind = ServiceKind.RADARR if parsed.media_type == "movie" else ServiceKind.SONARR
    candidates = list(ServiceInstance.objects.filter(kind=kind, enabled=True))
    if not candidates:
        return None

    if parsed.keys.service_id is not None:
        exact = [c for c in candidates if c.remote_service_id == parsed.keys.service_id]
        if exact:
            return exact[0]

    lane = [c for c in candidates if c.is_4k == parsed.is_4k]
    if len(lane) == 1:
        return lane[0]
    if lane:
        return lane[0]
    if len(candidates) == 1:
        return candidates[0]
    return tracked.arr_service or candidates[0]
