"""Rule 5: imported by the *arr, but not visible in the media server.

Two very different causes with identical symptoms, separated here:

* the server genuinely has not scanned yet -- wait, or trigger a scan.
* the server has the item, but our path mapping does not resolve to it -- the mapping is
  wrong, and no amount of scanning will fix it.

If no media server is configured at all, this rule stays silent: nothing looked, so
"it is not there" would be an assertion with no evidence behind it.

The distinguishing evidence is the ratingKey join succeeding while the path join fails.
"""

from __future__ import annotations

from datetime import timedelta

from django.urls import reverse

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class NotInMediaServer(Rule):
    code = "NOT_IN_MEDIA_SERVER"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        imported = ctx.imported
        request = ctx.request

        if imported is None and not request.arr_has_file:
            return None

        # With no media server configured, nothing ever looked -- so "it is not there" is
        # not a finding, it is an absence of evidence. Reporting it anyway sends the user
        # to investigate a library that Sleutharr was never told about.
        if not ctx.media_server_configured:
            return None

        server = ctx.media_server_name

        # A path mismatch is recorded as MEDIA_SERVER_AVAILABLE with a mismatch dedupe key: the
        # server has the item, our mapping just does not reach it.
        #
        # This is checked *before* the media_server_found short-circuit on purpose. The ingester
        # sets media_server_found=True whenever either join succeeds, and in the mismatch case
        # the ratingKey join does succeed -- so returning early on media_server_found would make
        # this branch unreachable and silently disable the diagnosis.
        mismatches = [
            e
            for e in ctx.of_type(EventType.MEDIA_SERVER_AVAILABLE)
            if e.dedupe_key.endswith(":path_mismatch")
        ]
        if mismatches:
            latest = mismatches[-1]
            raw = latest.raw if isinstance(latest.raw, dict) else {}
            server_paths = raw.get("serverPaths") or raw.get("plexPaths") or []
            arr_paths = raw.get("arrPaths") or []
            hint = ""
            if server_paths and arr_paths:
                hint = (
                    f"\n\nThe library file is at {arr_paths[0]} according to the *arr, "
                    f"and at {server_paths[0]} according to {server}."
                )
            return self.verdict(
                f"The file is in {server}, but no configured path mapping translates "
                f"the *arr's path to the path {server} reports. Sleutharr can only tell "
                "because it matched the item another way — anything relying on path "
                "matching (including your own scripts) will be wrong too."
                + hint,
                next_step=(
                    "Add the path mapping shown in the timeline entry on the Settings "
                    f"page. This does not affect {server} itself; it corrects how Sleutharr "
                    "matches files."
                ),
                # The fix is in Sleutharr's own settings, not in an upstream app.
                link=(reverse("settings"), "Path mappings"),
                evidence=[latest, imported] if imported else [latest],
                severity=Severity.WARNING,
                code="PATH_MISMATCH",
            )

        if request.media_server_found:
            return None

        # Everything below asserts the item is absent from the library. That is only a
        # finding if the library was actually searched -- a Plex that is down, or one
        # that has not finished its first sync, produces the same empty result as a Plex
        # that genuinely lacks the file.
        if not ctx.can_speak_for(ctx.media_server):
            return ctx.unreachable_verdict(
                ctx.media_server, f"whether this arrived in {server}"
            )

        missing = ctx.of_type(EventType.MEDIA_SERVER_MISSING)
        grace_minutes = float(ctx.setting("plex_grace_minutes") or 60)

        if imported is not None:
            since_import = ctx.now - imported.occurred_at
            if since_import < timedelta(minutes=grace_minutes):
                # Inside the grace period this is normal: servers scan on their own schedule.
                return None
            hours = since_import.total_seconds() / 3600
            message = (
                f"The *arr imported this {hours:.1f}h ago, but it still does not appear "
                f"in {server}."
            )
        else:
            message = (
                f"The library reports a file on disk, but it does not appear in {server}."
            )

        detail_hint = ""
        if missing:
            raw = missing[-1].raw if isinstance(missing[-1].raw, dict) else {}
            if raw.get("basenameCandidate"):
                detail_hint = (
                    f" A file with the same name does exist in {server} under a different "
                    "directory, which points at a path-mapping problem — see the "
                    "timeline entry for the exact prefixes."
                )

        return self.verdict(
            message + detail_hint,
            next_step=(
                f"Trigger a library scan in {server}. If it still does not appear, check "
                f"that the {server} library folder actually contains the imported file, "
                f"and that {server} has permission to read it. If the file "
                "is there, add a path mapping on the Settings page."
            ),
            link=ctx.media_server_url(),
            evidence=[e for e in [imported, *missing[-1:]] if e],
        )
