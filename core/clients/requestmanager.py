"""Request-manager clients: Seerr (primary), Overseerr and Jellyseerr (legacy).

Seerr is the mainline project that Overseerr and Jellyseerr users are migrating to, so it
is the reference implementation here and the other two are expressed as deltas from it.

The API *shape* is shared across all three, but the wire values are not identical -- see
`MEDIA_STATUS_BY_VARIANT`. Assuming they are identical is how you get a confidently wrong
verdict, so every divergence is encoded rather than assumed away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator

from django.utils.dateparse import parse_datetime

from core.clients.base import BaseClient, ProbeResult, ServiceError
from core.models import (
    MediaAvailability,
    MediaType,
    RequestState,
    ServiceVariant,
)

logger = logging.getLogger(__name__)


# MediaRequestStatus is consistent across the three products (verified against each
# repo's server/constants/media.ts on 2026-08-04). Note the published OpenAPI schema
# documents only the first three -- FAILED is the one that matters most to us, because
# it is what the request manager sets when the push to Sonarr/Radarr errored.
REQUEST_STATE_BY_VALUE = {
    1: RequestState.PENDING,
    2: RequestState.APPROVED,
    3: RequestState.DECLINED,
    4: RequestState.FAILED,
    5: RequestState.COMPLETED,
}

_SEERR_MEDIA_STATUS = {
    1: MediaAvailability.UNKNOWN,
    2: MediaAvailability.PENDING,
    3: MediaAvailability.PROCESSING,
    4: MediaAvailability.PARTIALLY_AVAILABLE,
    5: MediaAvailability.AVAILABLE,
    6: MediaAvailability.BLOCKLISTED,
    7: MediaAvailability.DELETED,
}

# Overseerr's enum has no BLOCKLISTED member, so its 6 is DELETED. Mapping Overseerr's 6
# through the Seerr table would report a deleted item as blocklisted and send the user
# chasing a blocklist that does not exist.
_OVERSEERR_MEDIA_STATUS = {
    1: MediaAvailability.UNKNOWN,
    2: MediaAvailability.PENDING,
    3: MediaAvailability.PROCESSING,
    4: MediaAvailability.PARTIALLY_AVAILABLE,
    5: MediaAvailability.AVAILABLE,
    6: MediaAvailability.DELETED,
}

MEDIA_STATUS_BY_VARIANT: dict[str, dict[int, str]] = {
    ServiceVariant.SEERR: _SEERR_MEDIA_STATUS,
    ServiceVariant.JELLYSEERR: _SEERR_MEDIA_STATUS,
    ServiceVariant.OVERSEERR: _OVERSEERR_MEDIA_STATUS,
}


@dataclass(slots=True)
class ServiceKeys:
    """The 4K-aware join keys resolved from a request's media object."""

    service_id: int | None
    external_service_id: int | None
    external_service_slug: str
    rating_key: str
    availability: str


def resolve_service_keys(
    media: dict[str, Any], is_4k: bool, variant: str
) -> ServiceKeys:
    """Pick the right half of every doubled field on a request manager's media object.

    Seerr stores each join key twice -- `serviceId`/`serviceId4k`,
    `externalServiceId`/`externalServiceId4k`, `ratingKey`/`ratingKey4k`,
    `status`/`status4k` -- and `MediaRequest.is4k` decides which half applies. A 4K
    request and a 1080p request for the same title share one Media row and routinely
    point at *different* Sonarr/Radarr instances.

    This is the only place in the codebase that chooses between the pairs, so the choice
    cannot be open-coded (and got wrong) at individual call sites.
    """
    suffix = "4k" if is_4k else ""
    status_map = MEDIA_STATUS_BY_VARIANT.get(variant, _SEERR_MEDIA_STATUS)
    raw_status = media.get(f"status{suffix}")
    return ServiceKeys(
        service_id=media.get(f"serviceId{suffix}"),
        external_service_id=media.get(f"externalServiceId{suffix}"),
        external_service_slug=media.get(f"externalServiceSlug{suffix}") or "",
        rating_key=str(media.get(f"ratingKey{suffix}") or ""),
        availability=status_map.get(raw_status, MediaAvailability.UNKNOWN),
    )


@dataclass(slots=True)
class NormalisedRequest:
    """A request-manager record reduced to what the rest of the app needs."""

    remote_id: int
    media_type: str
    requested_at: datetime
    updated_at: datetime | None
    requested_by: str
    request_state: str
    is_4k: bool
    tmdb_id: int | None
    tvdb_id: int | None
    imdb_id: str
    keys: ServiceKeys
    seasons: list[int] = field(default_factory=list)
    title: str = ""
    year: int | None = None
    raw: dict = field(default_factory=dict)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return parse_datetime(str(value))


class RequestManagerClient(BaseClient):
    """Common interface. Concrete variants subclass this."""

    api_prefix = "/api/v1"
    variant: str = ServiceVariant.SEERR
    #: Human label used in messages and the settings UI.
    product: str = "Seerr"

    def default_headers(self) -> dict[str, str]:
        return {**super().default_headers(), "X-Api-Key": self.service.api_key}

    # -- health ----------------------------------------------------------------

    def probe(self) -> ProbeResult:
        data = self.get_json("/status")
        if not isinstance(data, dict):
            return ProbeResult(ok=False, detail="Unexpected /status response.")
        version = str(data.get("version") or "")
        return ProbeResult(ok=True, detail=f"{self.product} {version}".strip(), version=version)

    # -- requests --------------------------------------------------------------

    def iter_requests(
        self, *, page_size: int = 50, filter_: str = "all", newest_first: bool = True
    ) -> Iterator[NormalisedRequest]:
        """Walk GET /request.

        Pagination is `take`/`skip`, not page/pageSize -- and `pageInfo.pages` is a page
        count while the offset is in records, so the loop terminates on
        `pageInfo.results` (the total record count), not on `pages`.
        """
        skip = 0
        seen = 0
        while True:
            payload = self.get_json(
                "/request",
                params={
                    "take": page_size,
                    "skip": skip,
                    "filter": filter_,
                    "sort": "added",
                    "sortDirection": "desc" if newest_first else "asc",
                },
            )
            if not isinstance(payload, dict):
                raise ServiceError("GET /request did not return an object.")
            results = payload.get("results") or []
            if not results:
                return
            for item in results:
                parsed = self.parse_request(item)
                if parsed is not None:
                    yield parsed
            seen += len(results)
            total = (payload.get("pageInfo") or {}).get("results")
            if total is not None and seen >= int(total):
                return
            if len(results) < page_size:
                return
            skip += page_size

    def parse_request(self, item: dict[str, Any]) -> NormalisedRequest | None:
        media = item.get("media") or {}
        requested_at = _dt(item.get("createdAt"))
        if requested_at is None:
            logger.debug("Skipping request %s with no createdAt", item.get("id"))
            return None

        is_4k = bool(item.get("is4k"))
        keys = resolve_service_keys(media, is_4k, self.variant)

        media_type = item.get("type") or media.get("mediaType") or ""
        media_type = (
            MediaType.TV if str(media_type).lower() == "tv" else MediaType.MOVIE
        )

        requested_by = item.get("requestedBy") or {}
        user_label = (
            requested_by.get("displayName")
            or requested_by.get("username")
            or requested_by.get("plexUsername")
            or requested_by.get("email")
            or ""
        )

        seasons = [
            s.get("seasonNumber")
            for s in (item.get("seasons") or [])
            if isinstance(s, dict) and s.get("seasonNumber") is not None
        ]

        return NormalisedRequest(
            remote_id=int(item["id"]),
            media_type=media_type,
            requested_at=requested_at,
            updated_at=_dt(item.get("updatedAt")),
            requested_by=str(user_label)[:200],
            request_state=REQUEST_STATE_BY_VALUE.get(
                item.get("status"), RequestState.UNKNOWN
            ),
            is_4k=is_4k,
            tmdb_id=media.get("tmdbId"),
            tvdb_id=media.get("tvdbId"),
            imdb_id=str(media.get("imdbId") or "")[:32],
            keys=keys,
            seasons=sorted(seasons),
            raw=item,
        )

    # -- title lookup ----------------------------------------------------------

    def fetch_title(self, media_type: str, tmdb_id: int) -> tuple[str, int | None]:
        """Resolve a display title.

        The request payload carries only ids, not a title -- a dashboard of tmdb ids
        would be useless, so this is worth the extra call. Failures are non-fatal.
        """
        path = "/movie" if media_type == MediaType.MOVIE else "/tv"
        try:
            data = self.get_json(f"{path}/{tmdb_id}")
        except ServiceError as exc:
            logger.debug("Title lookup failed for %s/%s: %s", path, tmdb_id, exc)
            return "", None
        if not isinstance(data, dict):
            return "", None
        title = data.get("title") or data.get("name") or ""
        date = data.get("releaseDate") or data.get("firstAirDate") or ""
        year = None
        if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
            year = int(date[:4])
        return str(title)[:500], year

    # -- deep links ------------------------------------------------------------

    def request_url(self, tmdb_id: int | None, media_type: str) -> str:
        if not tmdb_id:
            return f"{self.service.url}/requests"
        segment = "movie" if media_type == MediaType.MOVIE else "tv"
        return f"{self.service.url}/{segment}/{tmdb_id}"


class SeerrClient(RequestManagerClient):
    variant = ServiceVariant.SEERR
    product = "Seerr"


class OverseerrClient(RequestManagerClient):
    variant = ServiceVariant.OVERSEERR
    product = "Overseerr"


class JellyseerrClient(RequestManagerClient):
    variant = ServiceVariant.JELLYSEERR
    product = "Jellyseerr"


CLIENT_BY_VARIANT: dict[str, type[RequestManagerClient]] = {
    ServiceVariant.SEERR: SeerrClient,
    ServiceVariant.OVERSEERR: OverseerrClient,
    ServiceVariant.JELLYSEERR: JellyseerrClient,
}


def request_manager_client(service) -> RequestManagerClient:
    """Build the right client for a configured request-manager instance."""
    cls = CLIENT_BY_VARIANT.get(service.variant, SeerrClient)
    return cls(service)
