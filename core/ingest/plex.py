"""Plex ingestion and the path join.

Both join paths run, and running both is the point. The ratingKey tells us whether Plex
has the item at all; the path match tells us whether our path mapping is correct. The
combination is what separates "Plex has not scanned yet" from "your path mapping is
wrong" -- two situations with identical symptoms and completely different fixes.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from core.clients.base import ServiceError
from core.clients.plex import (
    PathMatchResult,
    PlexClient,
    PlexItem,
    match_paths,
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


def sync_plex() -> None:
    services = [
        s
        for s in ServiceInstance.objects.filter(enabled=True, kind=ServiceKind.PLEX)
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
        if t.plex_rating_key or t.arr_has_file or imported_paths(t)
    ]
    if not candidates:
        return

    mappings = list(PathMapping.objects.all())

    for service in services:
        client = PlexClient(service)
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


def _check_request(
    client: PlexClient,
    service: ServiceInstance,
    tracked: TrackedRequest,
    index: dict[str, PlexItem],
    mappings: list[PathMapping],
) -> None:
    now = timezone.now()

    # Path 1: the rating key the request manager recorded.
    by_rating_key: PlexItem | None = None
    if tracked.plex_rating_key:
        by_rating_key = client.item(tracked.plex_rating_key)

    # Path 2: translate the *arr's paths and look them up.
    arr_paths = imported_paths(tracked)
    match: PathMatchResult = match_paths(arr_paths, index, mappings)

    found = bool(by_rating_key or match.found)
    tracked.plex_found = found
    tracked.plex_matched_path = match.matched_path or ""
    if by_rating_key and not tracked.plex_rating_key:
        tracked.plex_rating_key = by_rating_key.rating_key
    tracked.save(
        update_fields=["plex_found", "plex_matched_path", "plex_rating_key"]
    )

    if match.found and match.item is not None:
        # The path now resolves, so any earlier mismatch/missing verdict is stale. These
        # are the events the rules read, so leaving them would keep reporting a problem
        # the user has already fixed.
        clear_events(tracked, f"plex:{service.pk}:path_mismatch")
        clear_events(tracked, f"plex:{service.pk}:missing")
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.PLEX,
            event_type=EventType.PLEX_AVAILABLE,
            occurred_at=now,
            summary=f"Present in Plex: {match.item.title}",
            detail=f"Matched path: {match.matched_path}",
            dedupe_key=f"plex:{service.pk}:found",
            raw=match.item.raw,
            update_existing=True,
        )
        return

    if by_rating_key is not None:
        # Plex has the item, but none of the translated *arr paths matched a Plex part.
        # That is a path-mapping problem, not a missing file -- and we can usually name
        # the exact prefix pair that would fix it.
        detail = _mapping_detail(arr_paths, match, by_rating_key)
        record_event(
            tracked,
            service=service,
            source_kind=ServiceKind.PLEX,
            event_type=EventType.PLEX_AVAILABLE,
            occurred_at=now,
            summary=(
                f"In Plex as '{by_rating_key.title}', but no configured path mapping "
                "resolves to it"
            ),
            detail=detail,
            dedupe_key=f"plex:{service.pk}:path_mismatch",
            raw={
                "ratingKey": by_rating_key.rating_key,
                "plexPaths": by_rating_key.paths,
                "arrPaths": arr_paths,
                "attempted": match.attempted,
                "basenameCandidate": match.basename_candidate,
            },
            update_existing=True,
        )
        return

    if arr_paths:
        detail = "Looked for:\n" + "\n".join(match.attempted)
        if match.basename_candidate:
            detail += (
                f"\n\nA file with the same name exists in Plex at:\n"
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
            source_kind=ServiceKind.PLEX,
            event_type=EventType.PLEX_MISSING,
            occurred_at=now,
            summary="Imported file not found in the Plex library",
            detail=detail,
            dedupe_key=f"plex:{service.pk}:missing",
            raw={
                "arrPaths": arr_paths,
                "attempted": match.attempted,
                "basenameCandidate": match.basename_candidate,
            },
            update_existing=True,
        )


def _mapping_detail(
    arr_paths: list[str], match: PathMatchResult, item: PlexItem
) -> str:
    lines = []
    if arr_paths:
        lines.append("The *arr reports:\n  " + "\n  ".join(arr_paths))
    if match.attempted:
        lines.append("After mapping, looked for:\n  " + "\n  ".join(match.attempted))
    if item.paths:
        lines.append("Plex reports:\n  " + "\n  ".join(item.paths))
        suggestion = suggest_mapping(arr_paths[0], item.paths[0]) if arr_paths else None
        if suggestion:
            lines.append(
                f"Add a path mapping: {suggestion[0]} -> {suggestion[1]}"
            )
    return "\n\n".join(lines)
