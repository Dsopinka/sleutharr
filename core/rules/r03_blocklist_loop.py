"""Rule 8 (priority 3): repeated grab -> fail -> blocklist cycles on one title.

Ordered above the stall and import rules because a loop *contains* those symptoms: the
current attempt will look stalled or blocked, but the actual problem is that this has
happened repeatedly and will keep happening.
"""

from __future__ import annotations

from collections import Counter

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class BlocklistLoop(Rule):
    code = "BLOCKLIST_LOOP"
    severity = Severity.ERROR

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        failures = ctx.of_type(EventType.DOWNLOAD_FAILED, EventType.DOWNLOAD_IGNORED)
        threshold = int(ctx.setting("blocklist_loop_threshold") or 3)

        if len(failures) < threshold:
            return None

        # A successful import after the last failure means the loop resolved itself.
        imported = ctx.imported
        if imported is not None and imported.occurred_at > failures[-1].occurred_at:
            return None

        grabs = ctx.grabs
        releases = Counter(
            (e.raw or {}).get("sourceTitle", "") for e in failures if isinstance(e.raw, dict)
        )
        distinct = len([r for r in releases if r])

        if distinct <= 1 and releases:
            release = next(iter(r for r in releases if r), "")
            detail = (
                f"The same release keeps being retried: {release!r}."
                if release
                else "The same release keeps being retried."
            )
            next_step = (
                "Blocklist that release explicitly and force a new search, or add a "
                "custom format / release-profile rule to reject it. Retrying will keep "
                "picking the same broken release otherwise."
            )
        else:
            detail = f"{distinct} different releases have failed."
            next_step = (
                "Several different releases are failing, which usually points at the "
                "download client or the filesystem rather than the releases — check "
                "disk space, the client's error log, and the *arr's remote path "
                "mappings."
            )

        return self.verdict(
            f"Stuck in a grab/fail loop: {len(grabs)} grab(s) and {len(failures)} "
            f"failure(s) with no successful import. {detail}",
            next_step=next_step,
            link=ctx.arr_url(),
            evidence=failures[-6:],
        )
