"""Rule 10: it arrived, and the request manager has not noticed.

Sleutharr checks the media server itself rather than believing what the request manager
says about it, and those two can disagree. When they do, this application is holding the
better evidence: it has matched the imported file to an item the media server actually
returned, while the request manager is still reporting the request as outstanding.

Reported from a live instance. Radarr had imported the film, Plex was serving it, and
Sleutharr's own facts said "In media server: yes" against the exact path -- while Seerr
still said Processing, so the request sat on the dashboard with no diagnosis at all. The
cause was visible in Seerr's own log: its recently-added Plex scan was erroring out, so
it never marked anything available. Its nightly full scan worked, which is why the state
eventually corrected itself overnight and looked like nothing more than slowness.

Deferring to the request manager here would hide a real fault in it, and hide it in the
most confusing way possible -- as an item that is plainly finished sitting in a list of
things that are not.
"""

from __future__ import annotations

from datetime import timedelta

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class ArrivedButNotReflected(Rule):
    code = "ARRIVED_NOT_REFLECTED"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        request = ctx.request

        # Only ever fires on positive evidence. `media_server_found` is also set by the
        # path-mismatch join, which proves the server knows the title but not that the
        # file resolves -- so the specific "we matched this path" event is required.
        if not request.media_server_found:
            return None

        matched = [
            e
            for e in ctx.of_type(EventType.MEDIA_SERVER_AVAILABLE)
            if e.dedupe_key.endswith(":found")
        ]
        if not matched:
            return None

        # An empty or unreachable library cannot vouch for a presence any more than for
        # an absence, and a stale match would keep asserting an arrival that may since
        # have been deleted.
        if not ctx.can_speak_for(ctx.media_server):
            return None

        # Request managers scan on their own schedule, and a few minutes behind is
        # normal operation rather than a fault. The same grace window rule 7 uses for
        # the opposite direction.
        latest = matched[-1]
        grace = float(ctx.setting("plex_grace_minutes") or 60)
        if ctx.now - latest.occurred_at < timedelta(minutes=grace):
            return None

        server = ctx.media_server_name
        manager = request.service.name if request.service else "your request manager"
        path = request.media_server_matched_path or ""
        hours = (ctx.now - latest.occurred_at).total_seconds() / 3600

        where = f"\n\n{server} has it at {path}." if path else ""

        return self.verdict(
            f"This has already arrived. {server} has been serving it for {hours:.1f}h, "
            f"but {manager} still lists the request as outstanding, which is why it is "
            f"still on this list.{where}",
            next_step=(
                f"Nothing is wrong with the file — watch it. The thing to fix is "
                f"{manager}: it is not picking up what {server} already has. Check its "
                f"logs for a failing library scan. On Seerr and Overseerr this is "
                f"usually the recently-added scan erroring out while the nightly full "
                f"scan still succeeds, which makes it look like nothing worse than a "
                f"delay — the request corrects itself overnight and breaks again the "
                f"next day."
            ),
            link=ctx.media_server_url(),
            evidence=[e for e in [latest, ctx.imported] if e],
        )
