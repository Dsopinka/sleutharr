"""Fixture loading and small builders shared across tests."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from core.models import (
    EventType,
    MediaType,
    RequestState,
    ServiceInstance,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def make_service(
    kind: str = ServiceKind.RADARR,
    *,
    name: str = "",
    variant: str = ServiceVariant.NATIVE,
    base_url: str = "http://arr.local:7878",
    remote_service_id: int | None = None,
    is_4k: bool = False,
    healthy: bool = True,
) -> ServiceInstance:
    """A configured service, healthy and recently polled unless told otherwise.

    `healthy` matters more than it looks: rules refuse to conclude anything from the
    silence of a service that has never answered, so a fixture left at the model default
    (`last_seen_ok=None`) represents a service that was never reachable, and every
    absence-based verdict correctly declines to fire against it.
    """
    return ServiceInstance.objects.create(
        kind=kind,
        variant=variant,
        name=name or f"test-{kind}",
        base_url=base_url,
        api_key="k",
        remote_service_id=remote_service_id,
        is_4k=is_4k,
        last_seen_ok=timezone.now() if healthy else None,
    )


def make_request(
    *,
    service: ServiceInstance | None = None,
    arr_service: ServiceInstance | None = None,
    media_type: str = MediaType.MOVIE,
    days_ago: int = 10,
    arr_entity_id: int | None = 412,
    monitored: bool | None = True,
    has_file: bool | None = False,
    snapshot: dict | None = None,
    remote_id: int = 101,
    **kwargs,
) -> TrackedRequest:
    service = service or make_service(
        ServiceKind.REQUEST_MANAGER,
        name="seerr",
        variant=ServiceVariant.SEERR,
        base_url="http://seerr.local:5055",
    )
    return TrackedRequest.objects.create(
        service=service,
        remote_id=remote_id,
        title=kwargs.pop("title", "Dune: Part Two"),
        year=kwargs.pop("year", 2024),
        media_type=media_type,
        requested_by=kwargs.pop("requested_by", "alice"),
        requested_at=timezone.now() - timedelta(days=days_ago),
        request_state=kwargs.pop("request_state", RequestState.APPROVED),
        tmdb_id=kwargs.pop("tmdb_id", 693134),
        arr_service=arr_service,
        arr_entity_id=arr_entity_id,
        arr_monitored=monitored,
        arr_has_file=has_file,
        arr_snapshot=snapshot or {},
        arr_quality_profile_name=kwargs.pop("profile", "HD-1080p"),
        **kwargs,
    )


def add_event(
    request: TrackedRequest,
    event_type: str,
    *,
    hours_ago: float = 1,
    summary: str = "",
    detail: str = "",
    raw: dict | None = None,
    source_kind: str = ServiceKind.RADARR,
    dedupe_key: str = "",
    service: ServiceInstance | None = None,
) -> TimelineEvent:
    return TimelineEvent.objects.create(
        request=request,
        service=service,
        source_kind=source_kind,
        event_type=event_type,
        occurred_at=timezone.now() - timedelta(hours=hours_ago),
        summary=summary or event_type,
        detail=detail,
        raw=raw or {},
        dedupe_key=dedupe_key or f"{event_type}:{hours_ago}:{TimelineEvent.objects.count()}",
    )


def torrent_sample(progress: float, **overrides) -> dict:
    """A qBittorrent torrents/info row with sane defaults."""
    base = {
        "hash": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "name": "Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
        "state": "downloading",
        "progress": progress,
        "num_seeds": 5,
        "num_complete": 20,
        "dlspeed": 1048576,
        "eta": 600,
        "amount_left": int(8_000_000_000 * (1 - progress)),
        "size": 8_000_000_000,
        "added_on": 1751364240,
        "completion_on": -1,
        "last_activity": 1751370000,
        "save_path": "/downloads/",
        "content_path": "/downloads/x",
        "category": "radarr",
        "ratio": 0.0,
        "availability": 1.0,
    }
    base.update(overrides)
    return base


def download_facts(raw: dict, *, usenet: bool = False) -> dict:
    """Normalise a raw client payload the way the ingester does.

    Deliberately runs the real product parser rather than hand-writing the normalised
    dict: a test that invents its own facts would keep passing if a parser broke, which
    is exactly the failure that let usenet downloads be diagnosed as dead torrents.
    """
    from core.clients.download import QBittorrentClient, SabnzbdClient

    item = (
        SabnzbdClient._parse_queue_slot(raw)
        if usenet
        else QBittorrentClient._parse(raw)
    )
    return item.facts()


def add_download_sample(
    request: TrackedRequest,
    raw: dict,
    *,
    usenet: bool = False,
    hours_ago: float = 1,
    service: ServiceInstance | None = None,
    **kwargs,
) -> TimelineEvent:
    """A DOWNLOAD_PROGRESS event carrying both the raw payload and normalised facts.

    The event is attributed to a healthy download client, because rules refuse to judge
    a transfer from readings taken by a client that is not currently answering -- without
    a service attached, every sample would read as evidence nobody can vouch for.
    """
    if service is None:
        service = ServiceInstance.objects.filter(
            kind=ServiceKind.DOWNLOAD_CLIENT
        ).first() or make_service(
            ServiceKind.DOWNLOAD_CLIENT,
            name="sab" if usenet else "qbit",
            variant=ServiceVariant.SABNZBD if usenet else ServiceVariant.QBITTORRENT,
            base_url="http://dl.local:8080",
        )
    event = add_event(
        request,
        EventType.DOWNLOAD_PROGRESS,
        hours_ago=hours_ago,
        raw=raw,
        service=service,
        source_kind=kwargs.pop("source_kind", ServiceKind.DOWNLOAD_CLIENT),
        **kwargs,
    )
    event.facts = download_facts(raw, usenet=usenet)
    event.save(update_fields=["facts"])
    return event
