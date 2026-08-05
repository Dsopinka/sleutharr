"""Rule 2: monitored and searched, but nothing has ever been grabbed.

Runs last because it is the residual case: everything more specific has been ruled out.

The important distinction is between "your indexers found nothing" and "nothing exists
yet" — a film still in cinemas has no release to find, and telling the user to check
their indexers would waste their time. Radarr already computes this as `isAvailable`
against `minimumAvailability`, so we use its answer.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils.dateparse import parse_datetime

from core.models import EventType, MediaType, Severity
from core.rules.base import Rule, RuleContext, Verdict


def _date(value):
    if not value:
        return None
    parsed = parse_datetime(str(value))
    # Sonarr/Radarr use year 1 as a null sentinel.
    return parsed if parsed and parsed.year > 1900 else None


class NoReleaseFound(Rule):
    code = "NO_RELEASE_FOUND"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        request = ctx.request
        if not request.arr_entity_id:
            return None
        if ctx.grabs or ctx.imported:
            return None
        if request.arr_has_file:
            return None

        wait_days = float(ctx.setting("no_release_days") or 2)
        age = ctx.now - request.requested_at
        if age < timedelta(days=wait_days):
            return None

        snapshot = request.arr_snapshot or {}
        profile = request.arr_quality_profile_name or "the configured profile"
        service = request.arr_service
        name = service.name if service else "the library"
        days = age.days

        # --- not released yet -------------------------------------------------
        if request.media_type == MediaType.MOVIE:
            is_available = snapshot.get("isAvailable")
            digital = _date(snapshot.get("digitalRelease"))
            physical = _date(snapshot.get("physicalRelease"))
            in_cinemas = _date(snapshot.get("inCinemas"))
            minimum = snapshot.get("minimumAvailability") or "released"

            if is_available is False:
                parts = []
                if in_cinemas:
                    parts.append(f"in cinemas {in_cinemas:%d %b %Y}")
                if digital:
                    parts.append(f"digital {digital:%d %b %Y}")
                if physical:
                    parts.append(f"physical {physical:%d %b %Y}")
                dates = "; ".join(parts) if parts else "no release dates known"

                return self.verdict(
                    f"Nothing has been grabbed in {days} days because the film has not "
                    f"reached the availability {name} requires "
                    f"(minimumAvailability={minimum}). Dates: {dates}. This is expected, "
                    f"not a fault.",
                    next_step=(
                        f"Wait for the release date. If you want it as soon as any "
                        f"release exists, lower minimumAvailability on this movie in "
                        f"{name} — but expect cam and telesync releases."
                    ),
                    link=ctx.arr_url(),
                    evidence=ctx.of_type(EventType.ADDED_TO_ARR),
                    severity=Severity.INFO,
                    code="NOT_RELEASED_YET",
                )

            never_searched = not _date(snapshot.get("lastSearchTime"))
        else:
            never_searched = False

        # --- searched, found nothing -----------------------------------------
        if never_searched:
            return self.verdict(
                f"Monitored and available for {days} days, but {name} has no record of "
                f"ever having searched for it.",
                next_step=(
                    f"Trigger a manual search in {name}. If nothing happens, check that "
                    f"at least one indexer is enabled and that the *arr's scheduled "
                    f"RSS/search tasks are running."
                ),
                link=ctx.arr_url(),
                evidence=ctx.of_type(EventType.ADDED_TO_ARR),
                code="NEVER_SEARCHED",
            )

        ignored = ctx.of_type(EventType.DOWNLOAD_IGNORED)
        extra = ""
        if ignored:
            extra = (
                f" {len(ignored)} release(s) were found but ignored — see the timeline "
                f"for the reason."
            )

        return self.verdict(
            f"Monitored for {days} days with no release grabbed. Filtering against "
            f"{profile}.{extra}",
            next_step=(
                f"Run an interactive search in {name} and read the rejection reasons — "
                f"they name the exact filter that excluded each release. Most often it "
                f"is {profile} being narrower than what indexers actually carry, a "
                f"custom-format minimum score, or a size limit."
            ),
            link=ctx.arr_url(),
            evidence=[*ctx.of_type(EventType.ADDED_TO_ARR), *ignored[-3:]],
        )
