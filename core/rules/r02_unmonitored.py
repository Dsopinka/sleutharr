"""Rule 7 (priority 2): the entity exists but monitoring is off.

Ordered early because it is absolute: an unmonitored item will never be searched, so
every downstream symptom ("no release found", "nothing grabbed") is a consequence rather
than the cause. Reporting the symptom would send the user hunting indexers for nothing.
"""

from __future__ import annotations

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class Unmonitored(Rule):
    code = "UNMONITORED"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        request = ctx.request
        if not request.arr_entity_id:
            return None

        # Already has the file: monitoring off is then normal and not a fault.
        if request.arr_has_file:
            return None

        if request.arr_monitored is not False:
            # The series can be monitored while the season actually asked for is not,
            # in which case nothing will ever be searched for it and every downstream
            # rule blames the indexers. `arr_monitored` is series-level and cannot see
            # this. Season shape verified against Sonarr's SeasonResource:
            # `seasonNumber` (int) and `monitored` (bool).
            return self._unmonitored_season(ctx)

        service = request.arr_service
        name = service.name if service else "the library"
        kind = "series" if request.is_series else "movie"

        return self.verdict(
            f"The {kind} exists in {name} but is not monitored, so it will never be "
            f"searched or grabbed.",
            next_step=(
                f"Turn monitoring on in {name} and trigger a search. If it keeps being "
                f"switched off, check whether an import list or a 'monitor: none' "
                f"setting is managing this item."
            ),
            link=ctx.arr_url(),
            evidence=ctx.of_type(EventType.ADDED_TO_ARR),
        )

    def _unmonitored_season(self, ctx: RuleContext) -> Verdict | None:
        """The requested season is off even though the series is on.

        Silent by design when the data is not there: `requested_seasons` is empty for a
        whole-series request, and a snapshot without a `seasons` list tells us nothing
        either way. Neither is grounds for a verdict.
        """
        request = ctx.request
        wanted = [s for s in (request.requested_seasons or []) if isinstance(s, int)]
        if not wanted:
            return None

        snapshot = request.arr_snapshot or {}
        seasons = snapshot.get("seasons")
        if not isinstance(seasons, list) or not seasons:
            return None

        monitored_by_number = {
            season.get("seasonNumber"): season.get("monitored")
            for season in seasons
            if isinstance(season, dict)
        }

        # Only seasons the *arr actually told us about; an unknown season number is
        # unknown, not unmonitored.
        off = [
            number
            for number in wanted
            if monitored_by_number.get(number) is False
        ]
        if not off or len(off) != len([n for n in wanted if n in monitored_by_number]):
            # Some requested season is still being watched for, so the request as a
            # whole has not been abandoned and this is not the blocking cause.
            return None

        service = request.arr_service
        name = service.name if service else "the library"
        listed = ", ".join(str(n) for n in sorted(off))
        plural = "s" if len(off) > 1 else ""

        return self.verdict(
            f"The series is monitored in {name}, but season{plural} {listed} — the "
            f"part actually requested — {'are' if len(off) > 1 else 'is'} not. Nothing "
            f"will ever be searched for {'them' if len(off) > 1 else 'it'}.",
            next_step=(
                f"Open the series in {name}, tick season{plural} {listed}, and trigger "
                f"a season search. A whole-series 'monitor' toggle does not turn "
                f"individual seasons back on."
            ),
            link=ctx.arr_url(),
            evidence=ctx.of_type(EventType.ADDED_TO_ARR),
            code="SEASON_UNMONITORED",
        )
