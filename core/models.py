"""Sleutharr data model.

Design rule: `TimelineEvent` is the only thing the UI and the rules engine read. Every
poller's job is to turn upstream API responses into TimelineEvents with their raw payload
attached. Diagnoses are always re-derivable from stored events without re-polling, because
rules change far more often than history does.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class ServiceKind(models.TextChoices):
    REQUEST_MANAGER = "request_manager", "Request manager"
    SONARR = "sonarr", "Sonarr"
    RADARR = "radarr", "Radarr"
    DOWNLOAD_CLIENT = "download_client", "Download client"
    MEDIA_SERVER = "media_server", "Media server"


class ServiceVariant(models.TextChoices):
    """Concrete product behind a ServiceKind.

    The kind says what role a service plays in the chain; the variant says which product
    is filling it. Rules only ever reason about kinds, so adding a product never touches
    the diagnosis layer.
    """

    # Request managers
    SEERR = "seerr", "Seerr"
    OVERSEERR = "overseerr", "Overseerr"
    JELLYSEERR = "jellyseerr", "Jellyseerr"
    OMBI = "ombi", "Ombi"

    # Media servers
    PLEX = "plex", "Plex"
    JELLYFIN = "jellyfin", "Jellyfin"
    EMBY = "emby", "Emby"

    # Download clients
    QBITTORRENT = "qbittorrent", "qBittorrent"
    TRANSMISSION = "transmission", "Transmission"
    DELUGE = "deluge", "Deluge"
    SABNZBD = "sabnzbd", "SABnzbd"
    NZBGET = "nzbget", "NZBGet"

    NATIVE = "native", "Native"


#: Which variants make sense for which kind. Drives the settings form so a user cannot
#: pair, say, a Radarr kind with a SABnzbd variant.
VARIANTS_BY_KIND: dict[str, list[str]] = {
    ServiceKind.REQUEST_MANAGER: [
        ServiceVariant.SEERR,
        ServiceVariant.OVERSEERR,
        ServiceVariant.JELLYSEERR,
        ServiceVariant.OMBI,
    ],
    ServiceKind.SONARR: [ServiceVariant.NATIVE],
    ServiceKind.RADARR: [ServiceVariant.NATIVE],
    ServiceKind.DOWNLOAD_CLIENT: [
        ServiceVariant.QBITTORRENT,
        ServiceVariant.TRANSMISSION,
        ServiceVariant.DELUGE,
        ServiceVariant.SABNZBD,
        ServiceVariant.NZBGET,
    ],
    ServiceKind.MEDIA_SERVER: [
        ServiceVariant.PLEX,
        ServiceVariant.JELLYFIN,
        ServiceVariant.EMBY,
    ],
}

#: Variants whose download ids are globally-unique infohashes. NZBGet hands out small
#: integers that are only unique within one instance, and SABnzbd uses opaque per-instance
#: strings, so those must never be looked up across clients. See docs/api-notes.md #8.
GLOBALLY_UNIQUE_ID_VARIANTS = {
    ServiceVariant.QBITTORRENT,
    ServiceVariant.TRANSMISSION,
    ServiceVariant.DELUGE,
}


class ServiceInstance(models.Model):
    """One configured upstream service.

    Multiple Sonarr/Radarr instances are the norm (4K and 1080p splits), so nothing in
    the codebase may assume a single instance of any kind.
    """

    kind = models.CharField(max_length=32, choices=ServiceKind.choices)
    variant = models.CharField(
        max_length=32, choices=ServiceVariant.choices, default=ServiceVariant.NATIVE
    )
    name = models.CharField(max_length=100, help_text="Label shown in the UI.")
    base_url = models.CharField(max_length=500)
    api_key = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=255, blank=True)
    password = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    verify_tls = models.BooleanField(default=True)
    poll_interval = models.PositiveIntegerField(default=60, help_text="Seconds.")

    # How the request manager refers to this instance. Seerr's Media carries a serviceId
    # which is an index into *its* Sonarr/Radarr settings -- that is the only reliable way
    # to route a request to the right arr instance when several are configured.
    remote_service_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="The request manager's serviceId for this Sonarr/Radarr instance.",
    )
    is_4k = models.BooleanField(
        default=False, help_text="This instance handles the request manager's 4K lane."
    )

    # Download clients only. The *arr's queue rows name the client that took each
    # download; that name is the only thing tying a queue row to a specific client, and
    # it is required to scope ids that are not globally unique (NZBGet hands out small
    # integers). Blank means "assume it matches this service's name".
    arr_client_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="What this download client is called inside Sonarr/Radarr.",
    )

    # Health, written by the pollers.
    last_seen_ok = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    version = models.CharField(max_length=64, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    # Set by the backoff logic; the scheduler skips the service until this passes.
    backoff_until = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0 and self.last_seen_ok is not None

    def is_backed_off(self) -> bool:
        return bool(self.backoff_until and self.backoff_until > timezone.now())

    @property
    def client_name(self) -> str:
        """The name the *arr knows this download client by."""
        return self.arr_client_name or self.name

    @property
    def ids_are_globally_unique(self) -> bool:
        return self.variant in GLOBALLY_UNIQUE_ID_VARIANTS


class PathMapping(models.Model):
    """Rewrite an *arr-side filesystem path into the path Plex reports.

    Container mounts differ between apps -- Radarr may see /data/media/movies while Plex
    sees /movies for the same file. No API exposes this; it is deployment config. A wrong
    or missing mapping is one of the most common real causes of "it imported but never
    showed up", so it gets its own model and its own diagnosis.
    """

    source_prefix = models.CharField(
        max_length=500, help_text="Path as the *arr sees it."
    )
    target_prefix = models.CharField(max_length=500, help_text="Path as Plex sees it.")
    note = models.CharField(max_length=200, blank=True)
    order = models.IntegerField(default=0, help_text="Lower is applied first.")

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return f"{self.source_prefix} -> {self.target_prefix}"

    def apply(self, path: str) -> str:
        if path and path.startswith(self.source_prefix):
            return self.target_prefix + path[len(self.source_prefix) :]
        return path


class MediaType(models.TextChoices):
    MOVIE = "movie", "Movie"
    TV = "tv", "TV"


class RequestState(models.TextChoices):
    """Normalised across request-manager variants."""

    PENDING = "pending", "Pending approval"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    FAILED = "failed", "Failed"
    COMPLETED = "completed", "Completed"
    UNKNOWN = "unknown", "Unknown"


class MediaAvailability(models.TextChoices):
    UNKNOWN = "unknown", "Unknown"
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    PARTIALLY_AVAILABLE = "partial", "Partially available"
    AVAILABLE = "available", "Available"
    BLOCKLISTED = "blocklisted", "Blocklisted"
    DELETED = "deleted", "Deleted"


class TrackedRequest(models.Model):
    """A request-manager record, plus the joins we have resolved for it."""

    service = models.ForeignKey(
        ServiceInstance, on_delete=models.CASCADE, related_name="requests"
    )
    remote_id = models.IntegerField(help_text="MediaRequest.id upstream.")

    title = models.CharField(max_length=500, blank=True)
    year = models.IntegerField(null=True, blank=True)
    media_type = models.CharField(max_length=16, choices=MediaType.choices)
    requested_by = models.CharField(max_length=200, blank=True)
    requested_at = models.DateTimeField()
    updated_at_remote = models.DateTimeField(null=True, blank=True)

    request_state = models.CharField(
        max_length=16, choices=RequestState.choices, default=RequestState.UNKNOWN
    )
    availability = models.CharField(
        max_length=16,
        choices=MediaAvailability.choices,
        default=MediaAvailability.UNKNOWN,
    )

    tmdb_id = models.IntegerField(null=True, blank=True)
    tvdb_id = models.IntegerField(null=True, blank=True)
    imdb_id = models.CharField(max_length=32, blank=True)

    # Seerr stores every join key twice -- once for the standard lane and once for 4K --
    # and MediaRequest.is4k selects which pair applies. Resolving the wrong pair silently
    # joins to the wrong Sonarr/Radarr instance, so is_4k is carried everywhere.
    is_4k = models.BooleanField(default=False)

    requested_seasons = models.JSONField(default=list, blank=True)

    # --- resolved join to the owning *arr instance -------------------------------
    arr_service = models.ForeignKey(
        ServiceInstance,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tracked_requests",
    )
    # Radarr movieId / Sonarr seriesId. Null means the join has not resolved -- which is
    # itself the strongest signal for the "never added" diagnosis.
    arr_entity_id = models.IntegerField(null=True, blank=True)
    arr_title_slug = models.CharField(max_length=255, blank=True)
    arr_monitored = models.BooleanField(null=True, blank=True)
    arr_has_file = models.BooleanField(null=True, blank=True)
    arr_quality_profile_id = models.IntegerField(null=True, blank=True)
    arr_quality_profile_name = models.CharField(max_length=128, blank=True)
    # Vertical resolution of the profile's cutoff. Compared against what actually landed,
    # because "cutoff not met" is true for most well-configured libraries and only a drop
    # in resolution is worth reporting.
    arr_cutoff_resolution = models.IntegerField(null=True, blank=True)
    arr_snapshot = models.JSONField(default=dict, blank=True)
    arr_last_synced = models.DateTimeField(null=True, blank=True)

    # --- resolved join to Plex ---------------------------------------------------
    media_server_item_id = models.CharField(max_length=64, blank=True)
    media_server_found = models.BooleanField(null=True, blank=True)
    media_server_matched_path = models.CharField(max_length=1000, blank=True)

    first_seen = models.DateTimeField(auto_now_add=True)
    last_polled = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["service", "remote_id"], name="uniq_request_per_service"
            )
        ]
        ordering = ["requested_at"]
        indexes = [
            models.Index(fields=["media_type", "request_state"]),
            models.Index(fields=["tmdb_id"]),
            models.Index(fields=["tvdb_id"]),
        ]

    def __str__(self) -> str:
        label = self.title or f"request {self.remote_id}"
        return f"{label} ({self.year})" if self.year else label

    @property
    def is_fulfilled(self) -> bool:
        return self.availability == MediaAvailability.AVAILABLE

    @property
    def age_days(self) -> int:
        return (timezone.now() - self.requested_at).days

    @property
    def display_title(self) -> str:
        base = self.title or f"Request #{self.remote_id}"
        if self.year:
            base = f"{base} ({self.year})"
        return f"{base} [4K]" if self.is_4k else base

    @property
    def is_series(self) -> bool:
        return self.media_type == MediaType.TV


class EventType(models.TextChoices):
    """Canonical vocabulary.

    Sonarr and Radarr use different names for the same concepts
    (seriesFolderImported vs movieFolderImported, episodeFileDeleted vs movieFileDeleted)
    and order their enums differently. Everything is normalised here so a rule cannot
    accidentally handle movies but not episodes.
    """

    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    REQUEST_FAILED = "request_failed", "Request failed"
    ADDED_TO_ARR = "added_to_arr", "Added to library"
    NOT_IN_ARR = "not_in_arr", "Not found in library"
    GRABBED = "grabbed", "Grabbed"
    DOWNLOAD_FAILED = "download_failed", "Download failed"
    DOWNLOAD_IGNORED = "download_ignored", "Download ignored"
    IMPORTED = "imported", "Imported"
    FILE_DELETED = "file_deleted", "File deleted"
    FILE_RENAMED = "file_renamed", "File renamed"
    QUEUED = "queued", "In download queue"
    DOWNLOAD_PROGRESS = "download_progress", "Download progress"
    IMPORT_BLOCKED = "import_blocked", "Import blocked"
    MEDIA_SERVER_AVAILABLE = "media_server_available", "Available in Plex"
    MEDIA_SERVER_MISSING = "media_server_missing", "Not found in Plex"
    UNKNOWN = "unknown", "Unknown"


class TimelineEvent(models.Model):
    """One thing that happened, from one service, with its raw payload preserved.

    The raw payload is non-negotiable: diagnosis rules will change, and re-deriving a
    verdict from stored payloads beats re-polling history that may have aged out upstream.
    """

    request = models.ForeignKey(
        TrackedRequest, on_delete=models.CASCADE, related_name="events"
    )
    service = models.ForeignKey(
        ServiceInstance, null=True, blank=True, on_delete=models.SET_NULL
    )
    source_kind = models.CharField(max_length=32, choices=ServiceKind.choices)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    occurred_at = models.DateTimeField()
    summary = models.CharField(max_length=500)
    detail = models.TextField(blank=True)
    raw = models.JSONField(default=dict, blank=True)

    # Natural key for idempotent ingestion: we must never store a history record twice,
    # and pollers re-read overlapping windows by design.
    dedupe_key = models.CharField(max_length=255)

    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["occurred_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "dedupe_key"], name="uniq_event_per_request"
            )
        ]
        indexes = [models.Index(fields=["request", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.occurred_at:%Y-%m-%d %H:%M} {self.event_type}"


class Severity(models.TextChoices):
    OK = "ok", "OK"
    INFO = "info", "Info"
    WARNING = "warning", "Warning"
    ERROR = "error", "Error"


class Diagnosis(models.Model):
    """The current verdict for a request. Exactly one per request; recomputed in place."""

    request = models.OneToOneField(
        TrackedRequest, on_delete=models.CASCADE, related_name="diagnosis"
    )
    code = models.CharField(max_length=64)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    message = models.TextField()
    next_step = models.TextField(blank=True)
    link_url = models.CharField(max_length=1000, blank=True)
    link_label = models.CharField(max_length=120, blank=True)
    evidence = models.ManyToManyField(TimelineEvent, blank=True, related_name="cited_by")
    computed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "diagnoses"
        indexes = [models.Index(fields=["code"]), models.Index(fields=["severity"])]

    def __str__(self) -> str:
        return f"{self.code} for request {self.request_id}"


class IngestCursor(models.Model):
    """Bookkeeping so a poller never re-fetches a history page it has already stored.

    `high_water` is the newest upstream timestamp ingested; `backfill_complete` flips once
    the backwards walk has reached the cutoff or run out of pages.
    """

    service = models.ForeignKey(
        ServiceInstance, on_delete=models.CASCADE, related_name="cursors"
    )
    scope = models.CharField(
        max_length=120, help_text="e.g. 'requests', or 'history:movie:412'."
    )
    high_water = models.DateTimeField(null=True, blank=True)
    last_page = models.IntegerField(default=0)
    backfill_complete = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["service", "scope"], name="uniq_cursor_scope"
            )
        ]

    def __str__(self) -> str:
        return f"{self.service_id}:{self.scope}"


class ActionStatus(models.TextChoices):
    SUCCESS = "success", "Succeeded"
    FAILED = "failed", "Failed"


class ActionLog(models.Model):
    """Record of every write Sleutharr has performed.

    Sleutharr is read-only apart from a small set of explicitly-confirmed remediations.
    Anything that does write gets a durable record here, because "did Sleutharr delete
    that, or did something else?" needs an answer that is not a guess.
    """

    request = models.ForeignKey(
        TrackedRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="actions",
    )
    #: Denormalised so the log survives the request being deleted.
    request_title = models.CharField(max_length=500, blank=True)
    action = models.CharField(max_length=64)
    target_service = models.CharField(max_length=120, blank=True)
    #: Exactly what was sent upstream, so the record is auditable after the fact.
    detail = models.TextField(blank=True)
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=ActionStatus.choices)
    error = models.TextField(blank=True)
    performed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-performed_at"]
        indexes = [models.Index(fields=["-performed_at"])]

    def __str__(self) -> str:
        return f"{self.action} on {self.request_title} ({self.status})"


class AppSetting(models.Model):
    """Small key/value store for tunables the rules read (windows, grace periods)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()

    def __str__(self) -> str:
        return self.key

    _UNSET = object()

    @classmethod
    def get(cls, key: str, default=_UNSET):
        """Stored value, else the caller's default, else the shipped default.

        The sentinel matters: `default=None` would make a legitimately stored or
        defaulted `False` indistinguishable from "no default given", which is exactly
        the case for the boolean flags.
        """
        row = cls.objects.filter(key=key).first()
        if row is not None:
            return row.value
        if default is not cls._UNSET:
            return default
        if key in DEFAULT_SETTINGS:
            return DEFAULT_SETTINGS[key]
        return DEFAULT_FLAGS.get(key)

    @classmethod
    def set(cls, key: str, value) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": value})


DEFAULT_SETTINGS: dict[str, object] = {
    "backfill_days": 90,
    # Rule 2: how long with no grab before we call it "no release found".
    "no_release_days": 2,
    # Rule 3: window over which negligible progress counts as stalled.
    "stall_hours": 6,
    "stall_min_progress_delta": 0.01,
    # Rule 5: how long after import before absence from Plex is a problem.
    "plex_grace_minutes": 60,
    # Rule 8: grab/fail cycles on one title before it is a loop.
    "blocklist_loop_threshold": 3,
}

#: Booleans are kept apart from the numeric tunables so the settings form can render them
#: as checkboxes rather than number inputs.
DEFAULT_FLAGS: dict[str, bool] = {
    # Removing from the queue with blocklist=true already makes Sonarr/Radarr search for
    # a replacement, so a separate search button is usually redundant. Off by default;
    # it exists for the cases where nothing was ever grabbed at all.
    "enable_search_action": False,
}
