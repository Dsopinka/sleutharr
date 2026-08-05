"""Download client ingestion.

The join key is the *arr's `downloadId`, which for torrent clients is the infohash. We
collect every download id seen in a request's grab and queue events, then ask the client
about exactly those.

Progress is stored as one sample per hour rather than one row per poll. That keeps a real
time series (which stall detection needs -- "negligible progress over a window" is
meaningless without history) while bounding growth to 24 rows a day per download.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from django.utils import timezone

from core.clients.base import ServiceError
from core.clients.download import TorrentStatus, download_client
from core.ingest.events import record_event
from core.models import (
    EventType,
    MediaAvailability,
    ServiceInstance,
    ServiceKind,
    TimelineEvent,
    TrackedRequest,
)

logger = logging.getLogger(__name__)


def _download_ids_by_request() -> dict[int, set[str]]:
    """Every download id we have seen for each still-open request.

    Read from stored raw payloads, so this works from history alone and does not need
    the *arr to still have the item in its queue.
    """
    open_ids = set(
        TrackedRequest.objects.exclude(
            availability__in=[MediaAvailability.AVAILABLE, MediaAvailability.DELETED]
        ).values_list("pk", flat=True)
    )
    if not open_ids:
        return {}

    mapping: dict[int, set[str]] = defaultdict(set)
    events = TimelineEvent.objects.filter(
        request_id__in=open_ids,
        event_type__in=[
            EventType.GRABBED,
            EventType.QUEUED,
            EventType.IMPORT_BLOCKED,
            EventType.DOWNLOAD_FAILED,
        ],
    ).values_list("request_id", "raw")

    for request_id, raw in events:
        if not isinstance(raw, dict):
            continue
        download_id = raw.get("downloadId")
        if download_id:
            # The *arr reports uppercase hex; qBittorrent uses lowercase. Normalise at
            # the boundary so the join cannot silently miss.
            mapping[request_id].add(str(download_id).lower())
    return mapping


def sync_download_clients() -> None:
    mapping = _download_ids_by_request()
    if not mapping:
        return

    all_hashes = sorted({h for hashes in mapping.values() for h in hashes})

    for service in ServiceInstance.objects.filter(
        enabled=True, kind=ServiceKind.DOWNLOAD_CLIENT
    ):
        if service.is_backed_off():
            continue
        client = download_client(service)
        try:
            with client:
                statuses = client.torrents_by_hash(all_hashes)
            client.record_success()
        except ServiceError as exc:
            client.record_failure(exc)
            continue
        except Exception as exc:  # noqa: BLE001
            client.record_failure(exc)
            continue
        finally:
            client.close()

        if not statuses:
            continue

        requests = TrackedRequest.objects.filter(pk__in=mapping.keys())
        by_id = {r.pk: r for r in requests}
        for request_id, hashes in mapping.items():
            tracked = by_id.get(request_id)
            if tracked is None:
                continue
            for infohash in hashes:
                status = statuses.get(infohash)
                if status is not None:
                    _record_status(service, tracked, status)


def _record_status(
    service: ServiceInstance, tracked: TrackedRequest, status: TorrentStatus
) -> None:
    now = timezone.now()
    pct = status.progress * 100

    if status.is_errored:
        summary = f"Download client error ({status.state}): {status.name}"
        event_type = EventType.DOWNLOAD_FAILED
    elif status.is_complete:
        summary = f"Download complete in client: {status.name}"
        event_type = EventType.DOWNLOAD_PROGRESS
    elif status.is_stalled:
        summary = f"Stalled at {pct:.1f}%: {status.name}"
        event_type = EventType.DOWNLOAD_PROGRESS
    else:
        summary = f"Downloading {pct:.1f}%: {status.name}"
        event_type = EventType.DOWNLOAD_PROGRESS

    seeds = (
        f"{status.num_seeds} connected"
        if status.num_complete < 0
        else f"{status.num_seeds} connected / {status.num_complete} in swarm"
    )
    detail = (
        f"state={status.state} · seeds: {seeds} · "
        f"{status.dlspeed / 1024:.0f} KiB/s · "
        f"{status.amount_left / 1_048_576:.0f} MiB left"
    )
    if status.num_complete < 0:
        detail += "\nTracker did not report a swarm count (num_complete=-1)."

    # One sample per hour: enough resolution for a stall window, bounded growth.
    bucket = now.strftime("%Y%m%d%H")
    record_event(
        tracked,
        service=service,
        source_kind=ServiceKind.DOWNLOAD_CLIENT,
        event_type=event_type,
        occurred_at=now,
        summary=summary,
        detail=detail,
        dedupe_key=f"dl:{service.pk}:{status.hash}:sample:{bucket}",
        raw=status.raw,
        update_existing=True,
    )
