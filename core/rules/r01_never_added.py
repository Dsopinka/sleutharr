"""Rule 1: approved upstream, but no Sonarr/Radarr entity exists."""

from __future__ import annotations

from core.models import EventType, RequestState, Severity
from core.rules.base import Rule, RuleContext, Verdict


class NeverAdded(Rule):
    code = "NEVER_ADDED"
    severity = Severity.ERROR

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        request = ctx.request

        # Still awaiting a human decision -- not a failure.
        if request.request_state == RequestState.PENDING:
            return None
        if request.request_state == RequestState.DECLINED:
            return self.verdict(
                "The request was declined in the request manager.",
                next_step="Nothing will happen unless someone re-requests or approves it.",
                link=ctx.request_manager_url(),
                evidence=ctx.of_type(EventType.DECLINED),
                severity=Severity.INFO,
                code="DECLINED",
            )

        if request.arr_entity_id:
            return None

        # No entity. Was there also no arr instance to route to?
        if request.arr_service is None:
            return self.verdict(
                "This request is not routed to any configured Sonarr/Radarr instance, "
                "so Sleutharr cannot trace it past the request manager.",
                next_step=(
                    "Add the Sonarr/Radarr instance that serves this request on the "
                    "Settings page. If several are configured, set each one's "
                    "'request manager service id' so requests route to the right one."
                ),
                link=ctx.request_manager_url(),
                evidence=ctx.of_type(EventType.REQUESTED),
                code="NO_ARR_INSTANCE",
            )

        # "Nothing matching exists in Sonarr" is only true if Sonarr answered. When it is
        # unreachable the join simply never ran, and on a fresh install it has not run
        # yet -- in both cases every request would be declared never-added at once.
        if not ctx.can_speak_for(request.arr_service):
            return ctx.unreachable_verdict(
                request.arr_service, "whether this ever reached your library"
            )

        service_name = request.arr_service.name
        failed = ctx.of_type(EventType.REQUEST_FAILED)
        missing = ctx.of_type(EventType.NOT_IN_ARR)

        if failed:
            message = (
                f"The request manager reports the request as FAILED and no matching "
                f"entry exists in {service_name}. The hand-off never completed."
            )
            next_step = (
                f"Check the request manager's Sonarr/Radarr settings (root folder, "
                f"quality profile, and that {service_name} is reachable from it), then "
                f"retry the request."
            )
        elif ctx.request_manager_links_entities:
            message = (
                f"Approved, but nothing matching it exists in {service_name} — "
                f"searched by service id, TMDB id and TVDB id."
            )
            next_step = (
                f"Add the title to {service_name} manually, or delete and re-request it "
                f"so the request manager pushes it again. Nothing will ever download "
                f"while the library has no entry."
            )
        else:
            # Ombi records no link between a request and the *arr record it became, so
            # the only evidence available is the id lookup. Say what was actually
            # checked rather than implying a hand-off failure we cannot observe.
            message = (
                f"Approved, but no entry with this TMDB/TVDB id exists in "
                f"{service_name}. {ctx.request.service.name} does not record which "
                f"library record a request became, so this is a best-effort id match."
            )
            next_step = (
                f"Check {service_name} for the title under a different id or spelling. "
                f"If it genuinely is not there, add it manually or re-request it."
            )

        return self.verdict(
            message,
            next_step=next_step,
            link=ctx.request_manager_url(),
            evidence=[*failed, *missing, *ctx.of_type(EventType.APPROVED)],
        )
