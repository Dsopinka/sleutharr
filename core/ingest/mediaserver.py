"""Media server ingestion and the path join (Plex, Jellyfin, Emby).

Both join paths run, and running both is the point. The server-native id tells us whether
the server has the item at all; the path match tells us whether our path mapping is
correct. The combination is what separates "the server has not scanned yet" from "your
path mapping is wrong" -- identical symptoms, completely different fixes.

The id join differs by product: Plex has a rating key stored by the request manager,
while Jellyfin and Emby expose ProviderIds so we can match on tmdb/tvdb directly.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from core.clients.base import ServiceError
from core.clients.mediaserver import (
    MediaItem,
    MediaServerClient,
    PathMatchResult,
    match_paths,
    media_server_client,
    suggest_mapping,
)
from core.ingest.events import clear_events, record_event
from core.models import (
    EventType,
    MediaAvailability,
    PathMapping,
    ServiceInstance,
    ServiceKind,
    TimelineEvent,
    TrackedRequest,
)

logger = logging.getLogger(__name__)


def imported_paths(tracked: TrackedRequest) -> list[str]:
    """Absolute paths of files the *arr says it imported.

    Read from stored history payloads and the entity snapshot, so no extra upstream calls
    are needed to do the path join.
    """
    paths: list[str] = []

    for raw in TimelineEvent.objects.filter(
        request=tracked, event_type=EventType.IMPORTED
    ).values_list("raw", flat=True):
        if not isinstance(raw, dict):
            continue
        data = raw.get("data") or {}
        for key in ("importedPath", "droppedPath", "path"):
            value = data.get(key)
            if value and value not in paths:
                paths.append(str(value))

    snapshot = tracked.arr_snapshot or {}
    movie_file = snapshot.get("movieFile") or {}
    if movie_file.get("path") and movie_file["path"] not in paths:
        paths.append(str(movie_file["path"]))

    return paths


def sync_media_servers() -> None:
    services = [
        s
        for s in ServiceInstance.objects.filter(enabled=True, kind=ServiceKind.MEDIA_SERVER)
        if not s.is_backed_off()
    ]
    if not services:
        return

    candidates = list(
        TrackedRequest.objects.exclude(
            availability__in=[MediaAvailability.DELETED]
        ).select_related("arr_service")
    )
    # Only bother with requests that have something to look for.
    candidates = [
        t
        for t in candidates
        if t.media_server_item_id or t.arr_has_file or imported_paths(t)
    ]
    if not candidates:
        return

    mappings = list(PathMapping.objects.all())

    for service in services:
        client = media_server_client(service)
        try:
            with client:
                index = client.build_path_index()
                for tracked in candidates:
                    # ServiceError propagates: a missing item already returns None from
                    # client.item(), so anything here is service-level and retrying it
                    # once per candidate would stall the cycle on a dead Plex.
                    _check_request(client, service, tracked, index, mappings)
            client.record_success()
        except ServiceError as exc:
            client.record_failure(exc)
        except Exception as exc:  # noqa: BLE001
            client.record_failure(exc)
        finally:
            client.close()



def _find_by_id(
    client: MediaServerClient,
    tracked: TrackedRequest,
    index: dict[str, MediaItem],
) -> MediaItem | None:
    """Locate the item by a server-native identifier rather than by path.

    Plex: the rating key the request manager stored.
    Jellyfin/Emby: ProviderIds on the item, which map straight to tmdb/tvdb.

    This is deliberately independent of the path join -- when this succeeds and the path
    join fails, the file is present and the path mapping is what is wrong.
    """
    if tracked.media_server_item_id:
        found = client.item(tracked.media_server_item_id)
        if found is not None:
            return found

    # Jellyfin/Emby expose provider ids, so we can match without any stored id at all.
    if tracked.tmdb_id or tracked.tvdb_id:
        for item in index.values():
            if tracked.tmdb_id and item.tmdb_id == tracked.tmdb_id:
                return item
            if tracked.tvdb_id and item.tvdb_id == tracked.tvdb_id:
                return item
    return None


def _check_request(
    client: MediaServerClient,
    service: ServiceInstance,
    tracked: TrackedRequest,
    index: dict[str, MediaItem],
    mappings: list[PathMapping],
) -> None:
    now = timezone.now()
    product = client.product

    # Join 1: server-native id.
    by_id = _find_by_id(client, tracked, index)

    # Join 2: translate the *arr's paths and look them up.
    arr_paths = imported_paths(tracked)
    match: PathMatchResult = match_paths(arr_paths, index, mappings)

    tracked.media_server_found = bool(by_id or match.found)
    tracked.media_server_matched_path = match.matched_path or ""
    if by_id and not tracked.media_server_item_id:
        tracked.media_server_item_id = by_id.item_id
    tracked.save(
        update_fields=[
            "media_server_found",
            "media_server_matched_path",
            "media_server_item_id",
        ]
    )

    scope = f"mediaserver:{service.pk}"

    if match.found and match.item is not None:
        # The path now resolves, so any earlier mismatch/missing verdict is stale. These
        # are the events the rules read, so leaving them would keep reporting a problem
        # the user has already fixed.
        clear_events(tracked, f"{scope}:path_mismatch")
        clear_events(tracked, f"{scope}:missing")
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_AVAILABLE,
            occurred_at=now,
            summary=f"Present in {product}: {match.item.title}",
            detail=f"Matched path: {match.matched_path}",
            dedupe_key=f"{scope}:found",
            raw=match.item.raw,
            update_existing=True,
        )
        return

    if by_id is not None:
        # The server has the item, but none of the translated *arr paths matched any of
        # its files. That is a path-mapping problem, not a missing file -- and we can
        # usually name the exact prefix pair that would fix it.
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_AVAILABLE,
            occurred_at=now,
            summary=(
                f"In {product} as '{by_id.title}', but no configured path mapping "
                "resolves to it"
            ),
            detail=_mapping_detail(arr_paths, match, by_id, product),
            dedupe_key=f"{scope}:path_mismatch",
            raw={
                "itemId": by_id.item_id,
                "serverPaths": by_id.paths,
                "arrPaths": arr_paths,
                "attempted": match.attempted,
                "basenameCandidate": match.basename_candidate,
                "product": product,
            },
            update_existing=True,
        )
        return

    if arr_paths:
        detail = "Looked for:\n  " + "\n  ".join(match.attempted)
        if match.basename_candidate:
            detail += (
                f"\n\nA file with the same name exists in {product} at:\n  "
                f"{match.basename_candidate}"
            )
            suggestion = suggest_mapping(
                match.attempted[0] if match.attempted else arr_paths[0],
                match.basename_candidate,
            )
            if suggestion:
                detail += (
                    f"\n\nThat implies a path mapping of "
                    f"{suggestion[0]} -> {suggestion[1]}."
                )
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.MEDIA_SERVER,
            event_type=EventType.MEDIA_SERVER_MISSING,
            occurred_at=now,
            summary=f"Imported file not found in the {product} library",
            detail=detail,
            dedupe_key=f"{scope}:missing",
            raw={
                "arrPaths": arr_paths,
                "attempted": match.attempted,
                "basenameCandidate": match.basename_candidate,
                "product": product,
            },
            update_existing=True,
        )


def _mapping_detail(
    arr_paths: list[str], match: PathMatchResult, item: MediaItem, product: str
) -> str:
    lines = []
    if arr_paths:
        lines.append("The *arr reports:\n  " + "\n  ".join(arr_paths))
    if match.attempted and match.attempted != arr_paths:
        lines.append("After mapping, looked for:\n  " + "\n  ".join(match.attempted))
    if item.paths:
        lines.append(f"{product} reports:\n  " + "\n  ".join(item.paths))
        suggestion = suggest_mapping(arr_paths[0], item.paths[0]) if arr_paths else None
        if suggestion:
            lines.append(f"Add a path mapping: {suggestion[0]} -> {suggestion[1]}")
    return "\n\n".join(lines)
