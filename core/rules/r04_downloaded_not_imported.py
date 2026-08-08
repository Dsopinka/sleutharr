"""Rule 4: the client says complete, but the *arr never imported it.

This is where hardlink and permission errors live. The *arr already knows exactly what
went wrong and says so in the queue's `errorMessage` / `statusMessages`; the job here is
to surface that verbatim rather than paraphrase it.
"""

from __future__ import annotations

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class DownloadedNotImported(Rule):
    code = "DOWNLOADED_NOT_IMPORTED"
    severity = Severity.ERROR

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        blocked = ctx.of_type(EventType.IMPORT_BLOCKED)
        latest_sample = ctx.latest(EventType.DOWNLOAD_PROGRESS)
        facts = ctx.download_facts()

        # `is_complete` is computed by the client's own parser, so this holds for usenet
        # as well as torrents. Reading raw progress fields here meant SABnzbd and NZBGet
        # downloads never registered as finished at all.
        client_complete = bool(facts and facts.get("is_complete"))

        if not blocked and not client_complete:
            return None

        # If it imported after the block, the problem resolved itself.
        imported = ctx.imported
        if imported is not None:
            newest_problem = max(
                [e.occurred_at for e in blocked] + ([latest_sample.occurred_at] if latest_sample else []),
                default=None,
            )
            if newest_problem is None or imported.occurred_at >= newest_problem:
                return None

        quoted = ""
        if blocked:
            detail = (blocked[-1].detail or "").strip()
            # Strip the trailing provenance line the ingester appends.
            lines = [
                ln
                for ln in detail.splitlines()
                if ln.strip() and not ln.strip().startswith(("trackedDownloadState", "status="))
                and " eventType=" not in ln
            ]
            if lines:
                quoted = "\n".join(f"> {ln.strip()}" for ln in lines[:4])

        if blocked:
            message = (
                "The download finished but the import is blocked. "
                + (f"{ctx.request.arr_service.name} reports:" if ctx.request.arr_service else "The *arr reports:")
            )
            if quoted:
                message = f"{message}\n{quoted}"
            next_step = _next_step_for(quoted)
        else:
            message = (
                "The download client reports this as complete, but no import event has "
                "been recorded and it is not in the queue either."
            )
            next_step = (
                "Check the *arr's Activity → Queue and its log around the completion "
                "time. A completed torrent that leaves the queue without importing "
                "usually means the *arr lost track of it (category or path change), or "
                "the file was removed before import."
            )

        return self.verdict(
            message,
            next_step=next_step,
            link=ctx.arr_queue_url() if blocked else ctx.arr_url(),
            evidence=[*blocked[-3:], latest_sample] if latest_sample else blocked[-3:],
        )


def _next_step_for(quoted: str) -> str:
    """Tailor the advice to what the *arr actually complained about."""
    lowered = quoted.lower()
    if "hardlink" in lowered or "cross-device" in lowered or "cross device" in lowered:
        return (
            "The download directory and the library are on different filesystems (or "
            "different Docker volumes), so hardlinking fails. Mount both under a single "
            "volume — the usual fix is one /data mount shared by the *arr and the "
            "download client — or disable hardlinks and accept the copy."
        )
    if "permission" in lowered or "denied" in lowered or "access to the path" in lowered:
        return (
            "A permissions problem. Check that the *arr's user (PUID/PGID) can write to "
            "the library folder and read the completed-download folder, and that umask "
            "leaves group write on new files."
        )
    if "sample" in lowered:
        return (
            "The *arr judged the file to be a sample. If it is genuine, the release is "
            "mislabelled or unusually short — blocklist it and search again."
        )
    if "no files found" in lowered or "not a valid" in lowered or "unsupported" in lowered:
        return (
            "The completed download contains nothing the *arr will import (wrong "
            "extension, archive, or an empty folder). Blocklist the release and search "
            "again."
        )
    return (
        "Fix the condition quoted above, then use Manual Import in the *arr's queue to "
        "retry without re-downloading."
    )
