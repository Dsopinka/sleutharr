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
        if not request.arr_entity_id or request.arr_monitored is not False:
            return None

        # Already has the file: monitoring off is then normal and not a fault.
        if request.arr_has_file:
            return None

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
