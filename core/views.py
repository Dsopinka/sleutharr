"""Server-rendered views. HTMX handles the interactive bits; there is no build step."""

from __future__ import annotations

import json
import logging

from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.actions import (
    ActionError,
    describe_remove,
    remove_from_queue,
    search_enabled,
    trigger_search,
)
from core.clients import ServiceError, client_for
from core.models import (
    ActionLog,
    AppSetting,
    DEFAULT_FLAGS,
    DEFAULT_SETTINGS,
    Diagnosis,
    MediaAvailability,
    PathMapping,
    ServiceInstance,
    ServiceKind,
    ServiceVariant,
    Severity,
    TimelineEvent,
    TrackedRequest,
    VARIANTS_BY_KIND,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- dashboard


def _unfulfilled() -> "models.QuerySet[TrackedRequest]":
    """Every request that has not landed in the media server yet.

    The dashboard is the product: it answers "what did I ask for that never arrived",
    so fulfilled requests are excluded unless the user searches for them explicitly.
    """
    return (
        TrackedRequest.objects.exclude(availability=MediaAvailability.AVAILABLE)
        .select_related("diagnosis", "service", "arr_service")
        .order_by("requested_at")
    )


def dashboard(request):
    qs = _unfulfilled()

    severity = request.GET.get("severity", "")
    code = request.GET.get("code", "")
    if severity:
        qs = qs.filter(diagnosis__severity=severity)
    if code:
        qs = qs.filter(diagnosis__code=code)

    requests = list(qs)
    counts = (
        Diagnosis.objects.filter(request__in=_unfulfilled())
        .values("code", "severity")
        .annotate(n=Count("id"))
        .order_by("-n")
    )

    context = {
        "requests": requests,
        "diagnosis_counts": counts,
        "active_severity": severity,
        "active_code": code,
        "total_unfulfilled": len(requests),
        "undiagnosed": sum(1 for r in requests if not hasattr(r, "diagnosis")),
        "no_services": not ServiceInstance.objects.filter(enabled=True).exists(),
    }
    if request.headers.get("HX-Request"):
        return render(request, "core/_request_table.html", context)
    return render(request, "core/dashboard.html", context)


def request_detail(request, pk: int):
    tracked = get_object_or_404(
        TrackedRequest.objects.select_related("service", "arr_service", "diagnosis"), pk=pk
    )
    events = list(tracked.events.select_related("service").order_by("occurred_at", "id"))

    diagnosis = getattr(tracked, "diagnosis", None)
    evidence_ids = set()
    if diagnosis:
        evidence_ids = set(diagnosis.evidence.values_list("id", flat=True))

    # One lane per service so the causal chain reads left-to-right by responsibility.
    lanes = [
        (ServiceKind.REQUEST_MANAGER, "Request"),
        (ServiceKind.RADARR if tracked.media_type == "movie" else ServiceKind.SONARR,
         "Radarr" if tracked.media_type == "movie" else "Sonarr"),
        (ServiceKind.DOWNLOAD_CLIENT, "Download"),
        (ServiceKind.MEDIA_SERVER, "Media server"),
    ]

    for event in events:
        event.is_evidence = event.id in evidence_ids
        event.raw_json = json.dumps(event.raw, indent=2, sort_keys=True, default=str)

    return render(
        request,
        "core/request_detail.html",
        {
            "tracked": tracked,
            "events": events,
            "diagnosis": diagnosis,
            "lanes": lanes,
            "remove": describe_remove(tracked),
            "search_enabled": search_enabled(),
            "actions": tracked.actions.all()[:10],
        },
    )


def search(request):
    from core.rules import RULES

    query = (request.GET.get("q") or "").strip()
    results: list[TrackedRequest] = []
    if query:
        results = list(
            TrackedRequest.objects.select_related("diagnosis", "service")
            .filter(
                Q(title__icontains=query)
                | Q(requested_by__icontains=query)
                | Q(diagnosis__code__icontains=query)
            )
            .order_by("-requested_at")[:200]
        )
    context = {
        "query": query,
        "requests": results,
        "known_codes": sorted({r.code for r in RULES if r.code}),
        "is_search": True,
    }
    if request.headers.get("HX-Request"):
        return render(request, "core/_request_table.html", context)
    return render(request, "core/search.html", context)


# ------------------------------------------------------------------------------ health


def health(request):
    services = list(ServiceInstance.objects.all())
    for service in services:
        service.backed_off = service.is_backed_off()
    return render(
        request,
        "core/health.html",
        {
            "services": services,
            "now": timezone.now(),
        },
    )


@require_POST
def probe_service(request, pk: int):
    """Test one service and re-render its health row."""
    service = get_object_or_404(ServiceInstance, pk=pk)
    try:
        with client_for(service) as client:
            result = client.checked_probe()
    except Exception as exc:  # noqa: BLE001 - surfaced in the UI, never a 500
        logger.warning("Probe of %s failed: %s", service.name, exc)
        result = None

    service.refresh_from_db()
    service.backed_off = service.is_backed_off()
    return render(
        request,
        "core/_health_row.html",
        {"service": service, "probe": result},
    )


# ---------------------------------------------------------------------------- settings


SERVICE_FIELDS = (
    "name",
    "base_url",
    "api_key",
    "username",
    "password",
    "remote_service_id",
)


def settings_page(request):
    tunables = {k: AppSetting.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    flags = {k: AppSetting.get(k, v) for k, v in DEFAULT_FLAGS.items()}
    labels = dict(ServiceVariant.choices)
    return render(
        request,
        "core/settings.html",
        {
            "services": ServiceInstance.objects.all(),
            "mappings": PathMapping.objects.all(),
            "kinds": ServiceKind.choices,
            "variants": ServiceVariant.choices,
            # Drives the form's dependent dropdown so a kind cannot be paired with a
            # variant that makes no sense for it.
            "variants_by_kind": {
                kind: [{"value": v, "label": labels[v]} for v in variants]
                for kind, variants in VARIANTS_BY_KIND.items()
            },
            "tunables": tunables,
            "flags": flags,
            "recent_actions": ActionLog.objects.all()[:20],
        },
    )


@require_POST
def service_save(request, pk: int | None = None):
    service = get_object_or_404(ServiceInstance, pk=pk) if pk else ServiceInstance()

    service.kind = request.POST.get("kind") or service.kind
    service.variant = request.POST.get("variant") or ServiceVariant.NATIVE
    service.name = (request.POST.get("name") or "").strip()
    service.base_url = (request.POST.get("base_url") or "").strip()
    service.username = (request.POST.get("username") or "").strip()
    service.enabled = request.POST.get("enabled") == "on"
    service.verify_tls = request.POST.get("verify_tls") == "on"
    service.is_4k = request.POST.get("is_4k") == "on"
    service.arr_client_name = (request.POST.get("arr_client_name") or "").strip()[:120]

    # Blank secrets mean "unchanged" -- the form renders them masked, so submitting the
    # form must not wipe a working key.
    api_key = (request.POST.get("api_key") or "").strip()
    if api_key:
        service.api_key = api_key
    password = request.POST.get("password") or ""
    if password:
        service.password = password

    try:
        service.poll_interval = max(10, int(request.POST.get("poll_interval") or 60))
    except ValueError:
        service.poll_interval = 60

    remote_id = (request.POST.get("remote_service_id") or "").strip()
    service.remote_service_id = int(remote_id) if remote_id.isdigit() else None

    if not service.name or not service.base_url:
        return HttpResponse("Name and base URL are required.", status=400)

    # Editing a service invalidates its backoff -- the user has just changed the thing
    # that was probably broken, so make the next poll immediate.
    service.consecutive_failures = 0
    service.backoff_until = None
    service.save()

    return redirect("settings")


@require_POST
def service_delete(request, pk: int):
    get_object_or_404(ServiceInstance, pk=pk).delete()
    return redirect("settings")


@require_POST
def mapping_save(request):
    source = (request.POST.get("source_prefix") or "").strip()
    target = (request.POST.get("target_prefix") or "").strip()
    if source and target:
        PathMapping.objects.create(
            source_prefix=source,
            target_prefix=target,
            note=(request.POST.get("note") or "").strip()[:200],
            order=int(request.POST.get("order") or 0),
        )
    return redirect("settings")


@require_POST
def mapping_delete(request, pk: int):
    get_object_or_404(PathMapping, pk=pk).delete()
    return redirect("settings")


@require_POST
def tunables_save(request):
    for key, default in DEFAULT_SETTINGS.items():
        raw = request.POST.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw) if isinstance(default, float) else int(raw)
        except ValueError:
            continue
        AppSetting.set(key, value)
    # Checkboxes are absent from the POST when unticked, so every flag is written on
    # every save rather than only the ones present.
    for key in DEFAULT_FLAGS:
        AppSetting.set(key, request.POST.get(key) == "on")
    return redirect("settings")


# ----------------------------------------------------------------------------- actions


@require_POST
def action_remove(request, pk: int):
    """Remove a queue item. Destructive, so it is POST-only and confirmed in the UI."""
    tracked = get_object_or_404(TrackedRequest, pk=pk)
    try:
        queue_id = int(request.POST.get("queue_id") or 0)
    except ValueError:
        queue_id = 0
    if not queue_id:
        return _action_result(request, tracked, error="No queue item was selected.")

    try:
        entry = remove_from_queue(tracked, queue_id)
    except ActionError as exc:
        return _action_result(request, tracked, error=str(exc))
    return _action_result(request, tracked, message=entry.detail)


@require_POST
def action_search(request, pk: int):
    tracked = get_object_or_404(TrackedRequest, pk=pk)
    try:
        entry = trigger_search(tracked)
    except ActionError as exc:
        return _action_result(request, tracked, error=str(exc))
    return _action_result(request, tracked, message=entry.detail)


def _action_result(request, tracked, *, message: str = "", error: str = ""):
    """Re-render the action panel in place, or fall back to a redirect."""
    from core.rules.engine import diagnose_request

    if message:
        # The verdict was derived from state we just changed; recompute it now rather
        # than leaving a stale diagnosis on screen until the next poll.
        diagnose_request(tracked)
        tracked.refresh_from_db()

    context = {
        "tracked": tracked,
        "remove": describe_remove(tracked),
        "search_enabled": search_enabled(),
        "action_message": message,
        "action_error": error,
        "actions": tracked.actions.all()[:10],
    }
    if request.headers.get("HX-Request"):
        return render(request, "core/_actions.html", context)
    return redirect("request_detail", pk=tracked.pk)


@require_POST
def run_poll_now(request):
    """Kick a full poll cycle from the UI."""
    from core.scheduler import run_all_now

    run_all_now()
    return redirect(request.META.get("HTTP_REFERER") or "dashboard")


# --------------------------------------------------------------------------------- api


def api_requests(request):
    """Small JSON view; the UI is server-rendered but this makes the data scriptable."""
    payload = []
    for tracked in _unfulfilled()[:500]:
        diagnosis = getattr(tracked, "diagnosis", None)
        payload.append(
            {
                "id": tracked.pk,
                "title": tracked.display_title,
                "media_type": tracked.media_type,
                "requested_by": tracked.requested_by,
                "requested_at": tracked.requested_at.isoformat(),
                "age_days": tracked.age_days,
                "availability": tracked.availability,
                "diagnosis": (
                    {
                        "code": diagnosis.code,
                        "severity": diagnosis.severity,
                        "message": diagnosis.message,
                        "next_step": diagnosis.next_step,
                    }
                    if diagnosis
                    else None
                ),
            }
        )
    return JsonResponse({"results": payload, "count": len(payload)})
