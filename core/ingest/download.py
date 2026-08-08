"""Download client ingestion.

The join key is the *arr's `downloadId`, collected from stored grab and queue payloads so
this works from history alone without the *arr still holding the item in its queue.

Scoping matters and is not obvious. Torrent infohashes are globally unique, so asking
every torrent client about every hash is safe. NZBGet ids are small integers unique only
within one instance, and SABnzbd nzo_ids are opaque per-instance strings -- asking a
second NZBGet about id "42" will cheerfully return an unrelated NZB and produce confident
nonsense. So non-torrent lookups are restricted to the client the *arr actually named on
the queue row.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from django.utils import timezone

from core.clients.base import ServiceError
from core.clients.download import DownloadItem, download_client
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

#: Events whose raw payload may carry a downloadId.
ID_BEARING_EVENTS = [
    EventType.GRABBED,
    EventType.QUEUED,
    EventType.IMPORT_BLOCKED,
    EventType.DOWNLOAD_FAILED,
]


@dataclass(slots=True, frozen=True)
class DownloadRef:
    """A download id together with the client the *arr said was handling it."""

    download_id: str
    client_name: str


def _client_name_from(raw: dict) -> str:
    """Pull the download client's name out of a queue row or history payload.

    Queue rows carry `downloadClient` directly; history `grabbed` events tuck it inside
    `data`. Either way it is the *arr's name for the client, which is what we match on.
    """
    for key in ("downloadClient", "downloadClientName"):
        value = raw.get(key)
        if value:
            return str(value).strip().lower()
    data = raw.get("data")
    if isinstance(data, dict):
        for key in ("downloadClient", "downloadClientName"):
            value = data.get(key)
            if value:
                return str(value).strip().lower()
    return ""


def _refs_by_request() -> dict[int, set[DownloadRef]]:
    """Every (download id, client name) pair seen for each still-open request."""
    open_ids = set(
        TrackedRequest.objects.exclude(
            availability__in=[MediaAvailability.AVAILABLE, MediaAvailability.DELETED]
        ).values_list("pk", flat=True)
    )
    if not open_ids:
        return {}

    mapping: dict[int, set[DownloadRef]] = defaultdict(set)
    rows = TimelineEvent.objects.filter(
        request_id__in=open_ids, event_type__in=ID_BEARING_EVENTS
    ).values_list("request_id", "raw")

    for request_id, raw in rows:
        if not isinstance(raw, dict):
            continue
        download_id = raw.get("downloadId")
        if not download_id:
            continue
        mapping[request_id].add(
            DownloadRef(
                # The *arr uppercases torrent hashes while the clients report lowercase;
                # normalising both sides is what makes the join work at all.
                download_id=str(download_id).strip().lower(),
                client_name=_client_name_from(raw),
            )
        )
    return mapping


def _refs_for_service(
    service: ServiceInstance, mapping: dict[int, set[DownloadRef]]
) -> dict[int, set[str]]:
    """Which ids this particular client should be asked about.

    A client is asked about an id when the *arr named it on the row. If the *arr named no
    client at all (older history rows sometimes omit it), we fall back to asking every
    torrent client -- safe, because infohashes are globally unique -- but never a usenet
    client, whose ids would collide across instances.
    """
    wanted: dict[int, set[str]] = defaultdict(set)
    service_name = service.client_name.strip().lower()
    globally_unique = service.ids_are_globally_unique

    for request_id, refs in mapping.items():
        for ref in refs:
            if ref.client_name:
                if ref.client_name == service_name:
                    wanted[request_id].add(ref.download_id)
            elif globally_unique:
                wanted[request_id].add(ref.download_id)
    return wanted


def sync_download_clients() -> None:
    mapping = _refs_by_request()
    if not mapping:
        return

    services = [
        s
        for s in ServiceInstance.objects.filter(
            enabled=True, kind=ServiceKind.DOWNLOAD_CLIENT
        )
        if not s.is_backed_off()
    ]
    if not services:
        return

    requests_by_id = {
        r.pk: r for r in TrackedRequest.objects.filter(pk__in=mapping.keys())
    }

    for service in services:
        wanted = _refs_for_service(service, mapping)
        all_ids = sorted({i for ids in wanted.values() for i in ids})
        if not all_ids:
            continue

        client = download_client(service)
        try:
            with client:
                items = client.items_by_id(all_ids)
            client.record_success()
        except ServiceError as exc:
            client.record_failure(exc)
            continue
        except Exception as exc:  # noqa: BLE001
            client.record_failure(exc)
            continue
        finally:
            client.close()

        if not items:
            continue

        for request_id, ids in wanted.items():
            tracked = requests_by_id.get(request_id)
            if tracked is None:
                continue
            for download_id in ids:
                item = items.get(download_id)
                if item is not None:
                    _record_item(service, tracked, item)


def _record_item(
    service: ServiceInstance, tracked: TrackedRequest, item: DownloadItem
) -> None:
    now = timezone.now()
    pct = item.progress * 100

    if item.is_errored:
        summary = f"Download client error ({item.state}): {item.name}"
        event_type = EventType.DOWNLOAD_FAILED
    elif item.is_complete:
        summary = f"Download complete in client: {item.name}"
        event_type = EventType.DOWNLOAD_PROGRESS
    elif item.is_stalled:
        summary = f"Stalled at {pct:.1f}%: {item.name}"
        event_type = EventType.DOWNLOAD_PROGRESS
    else:
        summary = f"Downloading {pct:.1f}%: {item.name}"
        event_type = EventType.DOWNLOAD_PROGRESS

    detail = _describe(item, service)

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
        dedupe_key=f"dl:{service.pk}:{item.download_id}:sample:{bucket}",
        raw=item.raw,
        facts=item.facts(),
        update_existing=True,
    )


def _describe(item: DownloadItem, service: ServiceInstance) -> str:
    parts = [f"{service.name} · state={item.state}"]

    if item.is_usenet:
        if item.health >= 0:
            parts.append(f"article health {item.health:.0f}%")
    else:
        if item.num_complete < 0:
            parts.append(f"seeds: {item.num_seeds} connected (swarm count unavailable)")
        else:
            parts.append(
                f"seeds: {item.num_seeds} connected / {item.num_complete} in swarm"
            )

    if item.download_rate:
        parts.append(f"{item.download_rate / 1024:.0f} KiB/s")
    if item.left:
        parts.append(f"{item.left / 1_048_576:.0f} MiB left")

    detail = " · ".join(parts)
    if item.error_message:
        detail += f"\n{item.error_message}"
    if not item.is_usenet and item.num_complete < 0 and item.num_seeds == 0:
        detail += (
            "\nNo swarm count reported, so 'no seeds' cannot be confirmed from the "
            "tracker alone."
        )
    return detail
