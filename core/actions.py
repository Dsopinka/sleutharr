"""Remediation actions -- the only place Sleutharr writes to an upstream service.

Design constraints, in order of importance:

1. **Nothing runs unattended.** Every action is invoked by a human clicking a button and
   confirming a dialog that states exactly what will happen. There is no scheduler hook
   here and no "auto-remediate" flag, deliberately: diagnosis rules are heuristics over
   five services that disagree with each other, they will misfire, and a misfiring rule
   wired to a delete key destroys real downloads. A wrong badge costs ten seconds; a
   wrong deletion costs the file and blocklists the release so the *arr will not fetch
   it again.
2. **Removal goes through the *arr, never straight to the download client.** The *arr
   then updates its own queue and history, applies the blocklist, and starts the
   replacement search. Deleting from the client directly would leave the *arr believing
   the download is still in flight.
3. **Everything is logged.** "Did Sleutharr delete that, or did something else?" needs an
   answer that is not a guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from core.clients.arr import arr_client
from core.clients.base import ServiceError
from core.models import (
    ActionLog,
    ActionStatus,
    AppSetting,
    EventType,
    ServiceKind,
    TimelineEvent,
    TrackedRequest,
)

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """An action could not be performed. Carries a message fit for the UI."""


@dataclass(slots=True)
class QueueTarget:
    """A queue row we could act on, resolved from stored timeline events."""

    queue_id: int
    download_id: str
    title: str
    service_name: str
    state: str

    @property
    def label(self) -> str:
        return self.title or self.download_id or f"queue item {self.queue_id}"


def find_queue_targets(tracked: TrackedRequest) -> list[QueueTarget]:
    """Queue rows recorded for this request, newest first.

    Read from stored payloads rather than re-polling, so the confirmation dialog can be
    rendered without a round trip. The action itself re-reads the live queue before
    acting, because a stale queue id must never be used to delete something.
    """
    targets: list[QueueTarget] = []
    seen: set[int] = set()
    events = (
        TimelineEvent.objects.filter(
            request=tracked,
            event_type__in=[EventType.QUEUED, EventType.IMPORT_BLOCKED],
        )
        .select_related("service")
        .order_by("-occurred_at")
    )
    for event in events:
        raw = event.raw if isinstance(event.raw, dict) else {}
        queue_id = raw.get("id")
        if not isinstance(queue_id, int) or queue_id in seen:
            continue
        seen.add(queue_id)
        targets.append(
            QueueTarget(
                queue_id=queue_id,
                download_id=str(raw.get("downloadId") or ""),
                title=str(raw.get("title") or ""),
                service_name=event.service.name if event.service else "",
                state=str(raw.get("trackedDownloadState") or raw.get("status") or ""),
            )
        )
    return targets


def _log(
    tracked: TrackedRequest,
    action: str,
    *,
    status: str,
    detail: str = "",
    params: dict | None = None,
    error: str = "",
    service_name: str = "",
) -> ActionLog:
    return ActionLog.objects.create(
        request=tracked,
        request_title=tracked.display_title[:500],
        action=action,
        target_service=service_name,
        detail=detail,
        params=params or {},
        status=status,
        error=error[:2000],
        performed_at=timezone.now(),
    )


def describe_remove(tracked: TrackedRequest) -> dict:
    """What `remove_from_queue` would do, for the confirmation dialog.

    The dialog must state the consequences in full -- including that the files are
    deleted and the release blocklisted -- because those are not obvious from a button
    labelled "remove".
    """
    targets = find_queue_targets(tracked)
    service = tracked.arr_service
    return {
        "targets": targets,
        "service_name": service.name if service else "",
        "can_act": bool(targets and service),
        "reason": (
            ""
            if targets and service
            else "No download-client queue entry is recorded for this request."
            if service
            else "This request is not linked to a Sonarr/Radarr instance."
        ),
        "effects": [
            "Delete the partially-downloaded files from the download client.",
            "Blocklist this release so the *arr will not grab it again.",
            f"Let {service.name if service else 'the *arr'} search for a "
            "different release automatically.",
        ],
    }


def remove_from_queue(
    tracked: TrackedRequest,
    queue_id: int,
    *,
    blocklist: bool = True,
    remove_from_client: bool = True,
) -> ActionLog:
    """Remove one queue item via Sonarr/Radarr.

    `skipRedownload` is deliberately left false: with the release blocklisted, the *arr
    searches for a replacement itself, which is what the user wants and means Sleutharr
    does not need to issue a search command of its own.
    """
    service = tracked.arr_service
    if service is None:
        raise ActionError("This request is not linked to a Sonarr/Radarr instance.")
    if not service.enabled:
        raise ActionError(f"{service.name} is disabled.")

    params = {
        "removeFromClient": "true" if remove_from_client else "false",
        "blocklist": "true" if blocklist else "false",
        "skipRedownload": "false",
    }

    client = arr_client(service)
    try:
        with client:
            # Re-read the live queue first. A queue id from a stored event may be stale,
            # and reusing a stale id risks deleting whatever now occupies it.
            live = {item.remote_id: item for item in client.queue()}
            target = live.get(queue_id)
            if target is None:
                raise ActionError(
                    "That queue item is no longer in "
                    f"{service.name} — it may have finished or already been removed. "
                    "Refresh and try again."
                )
            if target.entity_id and tracked.arr_entity_id and (
                int(target.entity_id) != int(tracked.arr_entity_id)
            ):
                # Belt and braces: the id resolved to a different library item.
                raise ActionError(
                    "That queue item now belongs to a different title. Refresh and "
                    "try again."
                )

            client.request("DELETE", f"/queue/{queue_id}", params=params)
            label = target.title or str(queue_id)
        client.record_success()
    except ActionError as exc:
        _log(
            tracked,
            "remove_from_queue",
            status=ActionStatus.FAILED,
            error=str(exc),
            params={"queueId": queue_id, **params},
            service_name=service.name,
        )
        raise
    except (ServiceError, Exception) as exc:  # noqa: BLE001
        client.record_failure(exc)
        _log(
            tracked,
            "remove_from_queue",
            status=ActionStatus.FAILED,
            error=str(exc),
            params={"queueId": queue_id, **params},
            service_name=service.name,
        )
        raise ActionError(f"{service.name} rejected the request: {exc}") from exc
    finally:
        client.close()

    detail = (
        f"Removed “{label}” from the {service.name} queue"
        + (", deleted from the download client" if remove_from_client else "")
        + (", blocklisted the release" if blocklist else "")
        + ". The *arr will search for a replacement."
    )
    entry = _log(
        tracked,
        "remove_from_queue",
        status=ActionStatus.SUCCESS,
        detail=detail,
        params={"queueId": queue_id, **params},
        service_name=service.name,
    )

    # Record it on the timeline too, so the request's own story stays complete.
    TimelineEvent.objects.update_or_create(
        request=tracked,
        dedupe_key=f"action:remove:{queue_id}",
        defaults={
            "service": service,
            "source_kind": ServiceKind.RADARR
            if tracked.media_type == "movie"
            else ServiceKind.SONARR,
            "event_type": EventType.DOWNLOAD_IGNORED,
            "occurred_at": timezone.now(),
            "summary": f"Removed from queue by Sleutharr: {label}",
            "detail": detail,
            "raw": {"queueId": queue_id, **params, "performedBy": "sleutharr"},
        },
    )

    # The stale queue/progress events describe a download that no longer exists; leaving
    # them would keep the old verdict alive until the next full poll.
    TimelineEvent.objects.filter(
        request=tracked, event_type__in=[EventType.QUEUED, EventType.IMPORT_BLOCKED]
    ).delete()

    return entry


def search_enabled() -> bool:
    return bool(AppSetting.get("enable_search_action"))


def trigger_search(tracked: TrackedRequest) -> ActionLog:
    """Ask the *arr to search for this title.

    Off by default. Removing a queue item with `blocklist=true` already makes the *arr
    search for a replacement, so this exists only for the case where nothing was ever
    grabbed -- `NO_RELEASE_FOUND` and `NEVER_SEARCHED`.
    """
    if not search_enabled():
        raise ActionError("The search action is disabled in Settings.")

    service = tracked.arr_service
    if service is None or not tracked.arr_entity_id:
        raise ActionError("This request is not linked to a Sonarr/Radarr record.")

    if tracked.media_type == "movie":
        payload = {"name": "MoviesSearch", "movieIds": [tracked.arr_entity_id]}
    else:
        payload = {"name": "SeriesSearch", "seriesId": tracked.arr_entity_id}

    client = arr_client(service)
    try:
        with client:
            client.request("POST", "/command", json=payload)
        client.record_success()
    except Exception as exc:  # noqa: BLE001
        client.record_failure(exc)
        _log(
            tracked,
            "trigger_search",
            status=ActionStatus.FAILED,
            error=str(exc),
            params=payload,
            service_name=service.name,
        )
        raise ActionError(f"{service.name} rejected the search command: {exc}") from exc
    finally:
        client.close()

    return _log(
        tracked,
        "trigger_search",
        status=ActionStatus.SUCCESS,
        detail=f"Asked {service.name} to search for {tracked.display_title}.",
        params=payload,
        service_name=service.name,
    )
