"""Rule primitives.

A rule takes a request's events and returns a `Verdict` or `None`. It must not perform
I/O: everything it needs is already in the timeline, which is what makes rules cheap to
run on every cycle and trivial to test against fixture timelines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from django.utils import timezone

from core.models import (
    AppSetting,
    DEFAULT_SETTINGS,
    EventType,
    Severity,
    TimelineEvent,
    TrackedRequest,
)


@dataclass(slots=True)
class Verdict:
    """One diagnosis: what is wrong, and the specific thing to do about it."""

    code: str
    severity: str
    message: str
    next_step: str = ""
    link_url: str = ""
    link_label: str = ""
    evidence: list[TimelineEvent] = field(default_factory=list)


class RuleContext:
    """Everything a rule may look at, precomputed once per request."""

    def __init__(
        self,
        request: TrackedRequest,
        events: Sequence[TimelineEvent],
        *,
        now: datetime | None = None,
        settings: dict | None = None,
    ):
        self.request = request
        self.events = list(events)
        self.now = now or timezone.now()
        self._settings = settings if settings is not None else {}

    # -- settings --------------------------------------------------------------

    def setting(self, key: str):
        if key in self._settings:
            return self._settings[key]
        return AppSetting.get(key, DEFAULT_SETTINGS.get(key))

    # -- event access ----------------------------------------------------------

    def of_type(self, *types: str) -> list[TimelineEvent]:
        wanted = set(types)
        return [e for e in self.events if e.event_type in wanted]

    def latest(self, *types: str) -> TimelineEvent | None:
        matches = self.of_type(*types)
        return matches[-1] if matches else None

    def first(self, *types: str) -> TimelineEvent | None:
        matches = self.of_type(*types)
        return matches[0] if matches else None

    def has(self, *types: str) -> bool:
        return bool(self.of_type(*types))

    def since(self, when: datetime, *types: str) -> list[TimelineEvent]:
        return [e for e in self.of_type(*types) if e.occurred_at >= when]

    def age(self, event: TimelineEvent | None) -> timedelta | None:
        return None if event is None else self.now - event.occurred_at

    # -- convenience -----------------------------------------------------------

    @property
    def imported(self) -> TimelineEvent | None:
        return self.latest(EventType.IMPORTED)

    @property
    def grabs(self) -> list[TimelineEvent]:
        return self.of_type(EventType.GRABBED)

    @property
    def download_samples(self) -> list[TimelineEvent]:
        """Download-client progress samples, oldest first."""
        return self.of_type(EventType.DOWNLOAD_PROGRESS)

    def download_facts(self) -> dict:
        """Normalised state of the most recent download-client sample.

        Never the raw payload: field names differ per product, so a rule reading `raw`
        quietly reports zeros for every client it was not written against. Empty when
        the sample predates the facts field, which callers must read as "unknown".
        """
        latest = self.latest(EventType.DOWNLOAD_PROGRESS)
        return latest.facts if latest and isinstance(latest.facts, dict) else {}

    # -- links -----------------------------------------------------------------

    def arr_url(self) -> tuple[str, str]:
        """(url, label) pointing at this item in its owning Sonarr/Radarr."""
        service = self.request.arr_service
        if service is None:
            return "", ""
        segment = "movie" if self.request.media_type == "movie" else "series"
        slug = self.request.arr_title_slug
        if slug:
            return f"{service.url}/{segment}/{slug}", f"Open in {service.name}"
        return service.url, f"Open {service.name}"

    def arr_queue_url(self) -> tuple[str, str]:
        service = self.request.arr_service
        if service is None:
            return "", ""
        return f"{service.url}/activity/queue", f"{service.name} queue"

    @property
    def media_server_configured(self) -> bool:
        """Whether any media server exists to have looked in.

        Rules must not report absence from a library nobody configured.
        """
        from core.models import ServiceInstance, ServiceKind

        return ServiceInstance.objects.filter(
            kind=ServiceKind.MEDIA_SERVER, enabled=True
        ).exists()

    @property
    def media_server_name(self) -> str:
        """Display name of the configured media server, for messages."""
        from core.models import ServiceInstance, ServiceKind

        service = ServiceInstance.objects.filter(
            kind=ServiceKind.MEDIA_SERVER, enabled=True
        ).first()
        if service is None:
            return "your media server"
        return service.get_variant_display() if service.variant else service.name

    @property
    def request_manager_links_entities(self) -> bool:
        """Whether the request manager records which *arr record a request became.

        Seerr and its relatives store an externalServiceId, so its absence is evidence
        the push to the *arr failed. Ombi stores no such field at all, so on Ombi the
        same absence means nothing and a rule must not claim otherwise.
        """
        from core.clients.requestmanager import CLIENT_BY_VARIANT

        service = self.request.service
        if service is None:
            return True
        cls = CLIENT_BY_VARIANT.get(service.variant)
        return getattr(cls, "links_to_arr_entity", True)

    def media_server_url(self) -> tuple[str, str]:
        """(url, label) for the Plex web UI, if a Plex service is configured."""
        from core.models import ServiceInstance, ServiceKind

        service = ServiceInstance.objects.filter(
            kind=ServiceKind.MEDIA_SERVER, enabled=True
        ).first()
        if service is None:
            return "", ""
        if self.request.media_server_item_id:
            return (
                f"{service.url}/web/index.html#!/server/-/details"
                f"?key=%2Flibrary%2Fmetadata%2F{self.request.media_server_item_id}",
                "Open in Plex",
            )
        return f"{service.url}/web", "Open Plex"

    def request_manager_url(self) -> tuple[str, str]:
        service = self.request.service
        if not self.request.tmdb_id:
            return f"{service.url}/requests", f"Open {service.name}"
        segment = "movie" if self.request.media_type == "movie" else "tv"
        return (
            f"{service.url}/{segment}/{self.request.tmdb_id}",
            f"Open in {service.name}",
        )


class Rule:
    """Base class. Subclass, set `code`, implement `evaluate`."""

    #: Stable identifier shown as the badge and searchable in the UI.
    code: str = ""
    #: Default severity for verdicts this rule produces.
    severity: str = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:  # pragma: no cover
        raise NotImplementedError

    # -- helpers for subclasses ------------------------------------------------

    def verdict(
        self,
        message: str,
        *,
        next_step: str = "",
        evidence: Iterable[TimelineEvent] = (),
        link: tuple[str, str] = ("", ""),
        severity: str | None = None,
        code: str | None = None,
    ) -> Verdict:
        return Verdict(
            code=code or self.code,
            severity=severity or self.severity,
            message=message,
            next_step=next_step,
            link_url=link[0],
            link_label=link[1],
            evidence=[e for e in evidence if e is not None],
        )
