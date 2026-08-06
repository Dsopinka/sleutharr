"""Rule 6: imported below the profile cutoff, yet marked available upstream.

The silent one. The user sees "available", plays it, and finds a 480p rip. Nothing is
broken enough to raise an error anywhere, so nothing does.

We read the *arr's own `qualityCutoffNotMet` flag rather than comparing qualities
ourselves: the *arr already evaluated the file against its profile including custom
format scores, and reimplementing that comparison would ignore custom formats and
disagree with the app the user is about to go and look at.
"""

from __future__ import annotations

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class WrongQuality(Rule):
    code = "WRONG_QUALITY"
    severity = Severity.INFO

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        imported = ctx.imported
        if imported is None:
            return None

        raw = imported.raw if isinstance(imported.raw, dict) else {}
        if not raw.get("qualityCutoffNotMet"):
            return None

        quality_obj = (raw.get("quality") or {}).get("quality") or {}
        quality = quality_obj.get("name") or "unknown"

        # `qualityCutoffNotMet` is true for most well-configured libraries: a profile
        # whose cutoff is Bluray-2160p flags a perfectly good WEBDL-2160p file. Reporting
        # that as a fault buries the real cases in noise, so the resolution has to have
        # actually dropped. WEBDL vs Bluray at the same resolution is a source preference,
        # not something to send someone to investigate.
        landed = quality_obj.get("resolution")
        wanted = ctx.request.arr_cutoff_resolution
        if landed is not None and wanted is not None and int(landed) >= int(wanted):
            return None
        profile = ctx.request.arr_quality_profile_name or "the configured profile"
        service = ctx.request.arr_service
        name = service.name if service else "the library"

        drop = ""
        if landed is not None and wanted is not None:
            drop = f" You asked for at least {wanted}p and got {landed}p."

        return self.verdict(
            f"Imported at {quality}, which is below the cutoff for {profile}.{drop} "
            f"It is playable, so nothing upstream flags it — but {name} will keep "
            f"searching for an upgrade, and it may never find one.",
            next_step=(
                f"If {quality} is acceptable, lower the cutoff on {profile} so "
                f"{name} stops searching. If it is not, check why better releases are "
                f"being rejected — usually indexer coverage, a custom format score "
                f"below the minimum, or a size limit on the profile."
            ),
            link=ctx.arr_url(),
            evidence=[imported],
        )
