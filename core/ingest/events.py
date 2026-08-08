"""Idempotent TimelineEvent writes.

Pollers re-read overlapping windows on purpose, so every write goes through here and is
keyed on a stable `dedupe_key`. Storing an event twice would double-count grab/fail
cycles and trip the blocklist-loop rule on a healthy title.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import IntegrityError
from django.utils import timezone

from core.models import ServiceInstance, TimelineEvent, TrackedRequest

logger = logging.getLogger(__name__)


def record_event(
    request: TrackedRequest,
    *,
    source_kind: str,
    event_type: str,
    occurred_at: datetime | None,
    summary: str,
    dedupe_key: str,
    service: ServiceInstance | None = None,
    detail: str = "",
    raw: Any = None,
    facts: dict | None = None,
    update_existing: bool = False,
) -> TimelineEvent | None:
    """Create the event unless it already exists.

    `update_existing` is for events that represent a *current* reading rather than a
    historical fact -- download progress, for instance, where we want one row per
    torrent that keeps its latest values instead of one row per poll.
    """
    if occurred_at is None:
        occurred_at = timezone.now()
    if timezone.is_naive(occurred_at):
        occurred_at = timezone.make_aware(occurred_at, timezone.utc)

    defaults = {
        "service": service,
        "source_kind": source_kind,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "summary": summary[:500],
        "detail": detail,
        "raw": raw if raw is not None else {},
        "facts": facts or {},
    }

    try:
        if update_existing:
            event, _ = TimelineEvent.objects.update_or_create(
                request=request, dedupe_key=dedupe_key, defaults=defaults
            )
            return event
        event, created = TimelineEvent.objects.get_or_create(
            request=request, dedupe_key=dedupe_key, defaults=defaults
        )
        return event if created else None
    except IntegrityError:
        # Another poll thread won the race; the event exists, which is all we wanted.
        logger.debug("Duplicate event %s for request %s", dedupe_key, request.pk)
        return None


def clear_events(request: TrackedRequest, prefix: str) -> int:
    """Drop transient events under a dedupe-key prefix (e.g. a torrent that vanished)."""
    deleted, _ = TimelineEvent.objects.filter(
        request=request, dedupe_key__startswith=prefix
    ).delete()
    return deleted
