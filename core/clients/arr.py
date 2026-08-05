"""Sonarr and Radarr clients (API v3).

The two apps are close but not interchangeable, and the differences are exactly where
naive integrations break:

* the history event-type vocabularies differ (`seriesFolderImported` vs
  `movieFolderImported`, `episodeFileDeleted` vs `movieFileDeleted`);
* the two enums list their members in a different order, and the underlying C# enums have
  non-contiguous values, so integer event filtering is unsafe -- we filter on the
  serialised string name instead and never send `eventType` as an int;
* a series has no `hasFile`; completeness comes from `statistics`.

Everything is normalised into the canonical `EventType` vocabulary before it reaches the
rules engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from django.utils.dateparse import parse_datetime

from core.clients.base import BaseClient, ProbeResult, ServiceError
from core.models import EventType, ServiceKind

logger = logging.getLogger(__name__)


# Both apps' vocabularies mapped onto one canonical set. Sonarr and Radarr each have a
# folder-import event under a different name plus a shared downloadFolderImported; all
# three mean "the file landed in the library".
CANONICAL_EVENT: dict[str, str] = {
    "grabbed": EventType.GRABBED,
    "downloadfolderimported": EventType.IMPORTED,
    "seriesfolderimported": EventType.IMPORTED,
    "moviefolderimported": EventType.IMPORTED,
    "downloadfailed": EventType.DOWNLOAD_FAILED,
    "downloadignored": EventType.DOWNLOAD_IGNORED,
    "episodefiledeleted": EventType.FILE_DELETED,
    "moviefiledeleted": EventType.FILE_DELETED,
    "episodefilerenamed": EventType.FILE_RENAMED,
    "moviefilerenamed": EventType.FILE_RENAMED,
    "unknown": EventType.UNKNOWN,
}


def canonical_event(raw_event_type: Any) -> str:
    """Map an *arr history eventType onto the canonical vocabulary.

    Sonarr/Radarr serialise this as a string. If an integer ever arrives we deliberately
    return UNKNOWN rather than guessing an ordinal: the two apps order their enums
    differently and the C# values are non-contiguous, so a guess would silently mislabel
    events -- and a mislabelled import is a wrong verdict.
    """
    if not isinstance(raw_event_type, str):
        logger.warning(
            "Non-string history eventType %r; storing as unknown.", raw_event_type
        )
        return EventType.UNKNOWN
    return CANONICAL_EVENT.get(raw_event_type.strip().lower(), EventType.UNKNOWN)


@dataclass(slots=True)
class ArrHistoryEvent:
    remote_id: int
    event_type: str
    raw_event_type: str
    occurred_at: datetime
    source_title: str
    download_id: str
    quality: str
    quality_cutoff_not_met: bool
    data: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class ArrQueueItem:
    remote_id: int
    entity_id: int | None
    title: str
    status: str
    tracked_status: str
    tracked_state: str
    error_message: str
    status_messages: list[str]
    download_id: str
    protocol: str
    size: float
    sizeleft: float
    output_path: str
    raw: dict = field(default_factory=dict)

    @property
    def progress(self) -> float:
        if not self.size:
            return 0.0
        return max(0.0, min(1.0, (self.size - self.sizeleft) / self.size))

    @property
    def blocked_messages(self) -> list[str]:
        """Everything the *arr is complaining about, deduped and ordered."""
        out: list[str] = []
        for msg in [self.error_message, *self.status_messages]:
            msg = (msg or "").strip()
            if msg and msg not in out:
                out.append(msg)
        return out


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return parse_datetime(str(value))


class ArrClient(BaseClient):
    """Shared Sonarr/Radarr behaviour."""

    api_prefix = "/api/v3"

    #: "movie" or "series" -- the per-entity history path segment.
    entity_path: str = ""
    #: Query param naming the entity on the history endpoint.
    entity_id_param: str = ""
    #: Query param suppressing the embedded entity payload.
    include_param: str = ""
    #: Query param that includes queue rows not linked to a library item.
    unknown_items_param: str = ""
    #: Front-end route segment for deep links.
    ui_segment: str = ""
    kind: str = ""

    def default_headers(self) -> dict[str, str]:
        return {**super().default_headers(), "X-Api-Key": self.service.api_key}

    def probe(self) -> ProbeResult:
        data = self.get_json("/system/status")
        if not isinstance(data, dict):
            return ProbeResult(ok=False, detail="Unexpected /system/status response.")
        version = str(data.get("version") or "")
        app = str(data.get("appName") or self.kind.title())
        return ProbeResult(ok=True, detail=f"{app} {version}".strip(), version=version)

    # -- entity ----------------------------------------------------------------

    def get_entity(self, entity_id: int) -> dict | None:
        try:
            data = self.get_json(f"/{self.entity_path}/{entity_id}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def lookup_entity(self, *, tmdb_id: int | None, tvdb_id: int | None) -> dict | None:
        """Fallback join when the request manager has no externalServiceId.

        That is precisely the case when the push to the *arr failed, so this returning
        None is meaningful evidence rather than an error.
        """
        raise NotImplementedError

    # -- history ---------------------------------------------------------------

    def entity_history(self, entity_id: int) -> list[ArrHistoryEvent]:
        """Full history for one entity.

        `/history/movie` and `/history/series` are unpaginated -- they return the whole
        array -- so there is no cursor to keep here.
        """
        params: dict[str, Any] = {
            self.entity_id_param: entity_id,
            self.include_param: "false",
        }
        data = self.get_json(f"/history/{self.entity_path}", params=params)
        if not isinstance(data, list):
            logger.warning(
                "%s: /history/%s returned %s, expected a list",
                self.service.name,
                self.entity_path,
                type(data).__name__,
            )
            return []
        events = []
        for row in data:
            parsed = self.parse_history_row(row)
            if parsed is not None:
                events.append(parsed)
        return events

    @staticmethod
    def parse_history_row(row: dict) -> ArrHistoryEvent | None:
        occurred = _dt(row.get("date"))
        if occurred is None:
            return None
        quality = ((row.get("quality") or {}).get("quality") or {}).get("name") or ""
        return ArrHistoryEvent(
            remote_id=int(row.get("id") or 0),
            event_type=canonical_event(row.get("eventType")),
            raw_event_type=str(row.get("eventType") or ""),
            occurred_at=occurred,
            source_title=str(row.get("sourceTitle") or ""),
            download_id=str(row.get("downloadId") or ""),
            quality=str(quality),
            quality_cutoff_not_met=bool(row.get("qualityCutoffNotMet")),
            data=row.get("data") or {},
            raw=row,
        )

    # -- queue -----------------------------------------------------------------

    def queue(self, page_size: int = 200) -> list[ArrQueueItem]:
        items: list[ArrQueueItem] = []
        page = 1
        while True:
            payload = self.get_json(
                "/queue",
                params={
                    "page": page,
                    "pageSize": page_size,
                    self.unknown_items_param: "true",
                    self.include_param: "false",
                },
            )
            if not isinstance(payload, dict):
                break
            records = payload.get("records") or []
            for row in records:
                items.append(self._parse_queue_row(row))
            total = payload.get("totalRecords")
            if total is None or page * page_size >= int(total) or not records:
                break
            page += 1
        return items

    def _parse_queue_row(self, row: dict) -> ArrQueueItem:
        messages: list[str] = []
        for block in row.get("statusMessages") or []:
            if not isinstance(block, dict):
                continue
            title = (block.get("title") or "").strip()
            inner = [m for m in (block.get("messages") or []) if m]
            if inner:
                messages.extend(str(m).strip() for m in inner)
            elif title:
                messages.append(title)
        return ArrQueueItem(
            remote_id=int(row.get("id") or 0),
            entity_id=row.get(self.entity_id_param),
            title=str(row.get("title") or ""),
            status=str(row.get("status") or ""),
            tracked_status=str(row.get("trackedDownloadStatus") or ""),
            tracked_state=str(row.get("trackedDownloadState") or ""),
            error_message=str(row.get("errorMessage") or ""),
            status_messages=messages,
            download_id=str(row.get("downloadId") or ""),
            protocol=str(row.get("protocol") or ""),
            size=float(row.get("size") or 0),
            sizeleft=float(row.get("sizeleft") or 0),
            output_path=str(row.get("outputPath") or ""),
            raw=row,
        )

    # -- quality profiles ------------------------------------------------------

    def quality_profiles(self) -> dict[int, str]:
        data = self.get_json("/qualityprofile")
        if not isinstance(data, list):
            return {}
        return {
            int(p["id"]): str(p.get("name") or "")
            for p in data
            if isinstance(p, dict) and p.get("id") is not None
        }

    # -- deep links ------------------------------------------------------------

    def entity_url(self, slug: str, entity_id: int | None = None) -> str:
        if slug:
            return f"{self.service.url}/{self.ui_segment}/{slug}"
        return f"{self.service.url}/{self.ui_segment}"

    def queue_url(self) -> str:
        return f"{self.service.url}/activity/queue"

    # -- file paths ------------------------------------------------------------

    def entity_file_paths(self, entity: dict) -> list[str]:
        """Absolute paths of imported files, for the Plex path join."""
        raise NotImplementedError


class RadarrClient(ArrClient):
    entity_path = "movie"
    entity_id_param = "movieId"
    include_param = "includeMovie"
    unknown_items_param = "includeUnknownMovieItems"
    ui_segment = "movie"
    kind = ServiceKind.RADARR

    def lookup_entity(self, *, tmdb_id: int | None, tvdb_id: int | None) -> dict | None:
        if not tmdb_id:
            return None
        data = self.get_json("/movie", params={"tmdbId": tmdb_id})
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None

    def entity_file_paths(self, entity: dict) -> list[str]:
        movie_file = entity.get("movieFile") or {}
        path = movie_file.get("path")
        return [str(path)] if path else []


class SonarrClient(ArrClient):
    entity_path = "series"
    entity_id_param = "seriesId"
    include_param = "includeSeries"
    unknown_items_param = "includeUnknownSeriesItems"
    ui_segment = "series"
    kind = ServiceKind.SONARR

    def lookup_entity(self, *, tmdb_id: int | None, tvdb_id: int | None) -> dict | None:
        if tvdb_id:
            data = self.get_json("/series", params={"tvdbId": tvdb_id})
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict):
                return data
        # Sonarr indexes on tvdbId; tmdbId is present on the resource but not queryable,
        # so fall back to scanning the (usually small) series list.
        if tmdb_id:
            data = self.get_json("/series")
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict) and row.get("tmdbId") == tmdb_id:
                        return row
        return None

    def episode_files(self, series_id: int) -> list[dict]:
        data = self.get_json("/episodefile", params={"seriesId": series_id})
        return data if isinstance(data, list) else []

    def entity_file_paths(self, entity: dict) -> list[str]:
        entity_id = entity.get("id")
        if not entity_id:
            return []
        try:
            files = self.episode_files(int(entity_id))
        except ServiceError as exc:
            logger.debug("episodefile lookup failed for series %s: %s", entity_id, exc)
            return []
        return [str(f["path"]) for f in files if isinstance(f, dict) and f.get("path")]

    def series_completeness(self, entity: dict) -> tuple[int, int]:
        """(files present, episodes expected) -- a series has no `hasFile`."""
        stats = entity.get("statistics") or {}
        return (
            int(stats.get("episodeFileCount") or 0),
            int(stats.get("episodeCount") or 0),
        )


ARR_CLIENT_BY_KIND: dict[str, type[ArrClient]] = {
    ServiceKind.RADARR: RadarrClient,
    ServiceKind.SONARR: SonarrClient,
}


def arr_client(service) -> ArrClient:
    cls = ARR_CLIENT_BY_KIND.get(service.kind)
    if cls is None:
        raise ValueError(f"{service.kind} is not an *arr service")
    return cls(service)
