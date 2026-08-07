"""Run the rules and persist verdicts."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from core.models import (
    AppSetting,
    DEFAULT_SETTINGS,
    Diagnosis,
    MediaAvailability,
    Severity,
    TrackedRequest,
)
from core.rules.base import RuleContext, Verdict

logger = logging.getLogger(__name__)

FULFILLED_CODE = "FULFILLED"


def evaluate(ctx: RuleContext) -> Verdict | None:
    """First matching rule wins."""
    from core.rules import RULES

    for rule in RULES:
        try:
            verdict = rule.evaluate(ctx)
        except Exception:  # noqa: BLE001 - one broken rule must not blank the dashboard
            logger.exception("Rule %s raised on request %s", rule.code, ctx.request.pk)
            continue
        if verdict is not None:
            return verdict
    return None


def diagnose_request(tracked: TrackedRequest, settings: dict | None = None) -> Diagnosis | None:
    events = list(tracked.events.select_related("service").order_by("occurred_at", "id"))
    ctx = RuleContext(tracked, events, settings=settings)

    if tracked.is_fulfilled:
        verdict = Verdict(
            code=FULFILLED_CODE,
            severity=Severity.OK,
            message="Available. Nothing to diagnose.",
        )
    else:
        verdict = evaluate(ctx)

    if verdict is None:
        # No rule matched. Clearing any stale verdict is important -- a diagnosis that
        # no longer applies is worse than none.
        Diagnosis.objects.filter(request=tracked).delete()
        return None

    return _persist(tracked, verdict)


@transaction.atomic
def _persist(tracked: TrackedRequest, verdict: Verdict) -> Diagnosis:
    diagnosis, _ = Diagnosis.objects.update_or_create(
        request=tracked,
        defaults={
            "code": verdict.code,
            "severity": verdict.severity,
            "message": verdict.message,
            "next_step": verdict.next_step,
            "link_url": verdict.link_url,
            "link_label": verdict.link_label,
            "computed_at": timezone.now(),
        },
    )
    diagnosis.evidence.set([e for e in verdict.evidence if e.pk])
    return diagnosis


def diagnose_all() -> int:
    """Re-derive every open request's verdict from stored events."""
    # Read tunables once; rules are called thousands of times per cycle and each would
    # otherwise hit the settings table.
    settings = {k: AppSetting.get(k, v) for k, v in DEFAULT_SETTINGS.items()}

    count = 0
    queryset = TrackedRequest.objects.exclude(
        availability=MediaAvailability.DELETED
    ).select_related("service", "arr_service")
    for tracked in queryset.iterator(chunk_size=200):
        try:
            diagnose_request(tracked, settings=settings)
            count += 1
        except Exception:  # noqa: BLE001
            logger.exception("Diagnosis failed for request %s", tracked.pk)
    logger.debug("Diagnosed %d requests", count)
    return count
