"""Rule 5: grabbed and in the client, but not actually progressing.

This rule reads `event.facts`, never `event.raw`. An earlier version parsed the raw
payload with qBittorrent's field names, which meant every SABnzbd and NZBGet download
read back as 0% with zero seeds and got told to blocklist itself. Usenet has no swarm to
run out of, so the seed logic below is guarded to torrents alone.
"""

from __future__ import annotations

from datetime import timedelta

from core.models import Severity
from core.rules.base import Rule, RuleContext, Verdict


class GrabbedButStalled(Rule):
    code = "GRABBED_BUT_STALLED"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        samples = ctx.download_samples
        if not samples:
            return None

        latest = samples[-1]
        facts = latest.facts if isinstance(latest.facts, dict) else {}
        if not facts:
            # Ingested before facts were recorded, so there is nothing this rule can
            # honestly say. Staying quiet beats guessing at a payload we cannot read.
            return None

        progress = float(facts.get("progress") or 0)
        state = str(facts.get("state") or "")
        name = str(facts.get("name") or "the release")
        is_usenet = bool(facts.get("is_usenet"))

        if facts.get("is_complete") or progress >= 1.0:
            return None  # completion is rule 4's problem, not this one

        if facts.get("is_errored"):
            detail = str(facts.get("error_message") or "").strip()
            return self.verdict(
                f"The download client reports state '{state}' for {name}."
                + (f"\n{detail}" if detail else ""),
                next_step=(
                    "Open the download client and inspect this item. For usenet that "
                    "usually means the articles were incomplete or unpacking failed; "
                    "for torrents, 'missingFiles' means its data was moved or deleted "
                    "underneath it and 'error' usually means the save path is "
                    "unwritable."
                ),
                link=ctx.arr_queue_url(),
                evidence=[latest],
                severity=Severity.ERROR,
                code="DOWNLOAD_CLIENT_ERROR",
            )

        if facts.get("is_paused"):
            return self.verdict(
                f"{name} is paused in the download client at {progress * 100:.1f}%.",
                next_step="Resume it in the download client.",
                link=ctx.arr_queue_url(),
                evidence=[latest],
            )

        # Usenet only: the articles themselves are missing from the provider. This is the
        # one usenet case where blocklisting really is the right move, because no amount
        # of waiting will reconstruct them.
        if facts.get("unhealthy_articles"):
            health = float(facts.get("health") or 0)
            return self.verdict(
                f"{name} is at {progress * 100:.1f}% but only {health:.0f}% of its "
                f"articles are still available on your provider. It cannot complete.",
                next_step=(
                    "Remove it from the queue and blocklist the release so the *arr "
                    "picks a different one. Low article health means the post has "
                    "partly expired or was taken down; a different release will fix it, "
                    "a retry of this one will not."
                ),
                link=ctx.arr_queue_url(),
                evidence=[latest],
            )

        # Torrents only. `has_no_seeds` is computed by the client parser, which knows
        # the difference between "the tracker said zero" and "the tracker said nothing".
        if not is_usenet and facts.get("has_no_seeds"):
            num_complete = int(facts.get("num_complete", -1) or -1)
            swarm = (
                f"{num_complete} seeds in the swarm"
                if num_complete >= 0
                else "no connected seeds and no reported swarm count"
            )
            return self.verdict(
                f"{name} is at {progress * 100:.1f}% with {swarm}. It cannot finish.",
                next_step=(
                    "Remove it from the queue and blocklist the release so the *arr "
                    "picks a different one. If this keeps happening, the indexer is "
                    "returning dead releases."
                ),
                link=ctx.arr_queue_url(),
                evidence=[latest],
            )

        stall_hours = float(ctx.setting("stall_hours") or 6)
        min_delta = float(ctx.setting("stall_min_progress_delta") or 0.01)
        window_start = ctx.now - timedelta(hours=stall_hours)

        # Compare the latest reading against a baseline at least `stall_hours` old.
        # Taking the oldest sample *inside* the window would be backwards: it excludes
        # the very baseline needed to measure a full window of no progress.
        older = [
            e
            for e in samples
            if e.occurred_at <= window_start and isinstance(e.facts, dict) and e.facts
        ]
        if not older:
            return None  # not enough history yet to call it stalled

        baseline = older[-1]
        old_progress = float(baseline.facts.get("progress") or 0)
        delta = progress - old_progress
        elapsed = (latest.occurred_at - baseline.occurred_at).total_seconds() / 3600

        if delta > min_delta:
            return None

        stalled_state = state in {"stalled", "metadata"}
        descriptor = "stalled" if stalled_state else "barely moving"

        if is_usenet:
            # No swarm, so the causes are entirely different ones: the client is idle or
            # paused by a schedule, or it cannot reach its news server. Sending someone
            # to blocklist a release over this destroys a good release and changes
            # nothing -- the next one will stall in exactly the same place.
            extra = (
                " It is queued but never started, which usually means the client is "
                "paused or something ahead of it in the queue is stuck."
                if state == "queued"
                else ""
            )
            return self.verdict(
                f"{name} has been {descriptor} at {progress * 100:.1f}% for "
                f"{elapsed:.1f}h (+{delta * 100:.2f}% over the window).{extra}",
                next_step=(
                    "This is a usenet download, so the release itself is probably fine "
                    "and blocklisting it would not help. Open the download client and "
                    "check it is not paused, and that its news server shows a working "
                    "connection — a failed server login, an expired subscription or a "
                    "VPN that has dropped will stall every download at once without "
                    "reporting an error against any single one of them."
                ),
                link=ctx.arr_queue_url(),
                evidence=[baseline, latest, *ctx.grabs[-1:]],
                code="DOWNLOADER_NOT_PROGRESSING",
            )

        num_seeds = int(facts.get("num_seeds", -1) or -1)
        extra = (
            " It is still fetching metadata, which means it has never connected to a peer."
            if state == "metadata"
            else ""
        )
        return self.verdict(
            f"{name} has been {descriptor} at {progress * 100:.1f}% for "
            f"{elapsed:.1f}h (+{delta * 100:.2f}% over the window), "
            f"{max(num_seeds, 0)} seed(s) connected.{extra}",
            next_step=(
                "Remove it from the queue and blocklist the release so the *arr searches "
                "again. If many downloads stall at once, check the client's connection "
                "limits, port forwarding, and whether the VPN is still up."
            ),
            link=ctx.arr_queue_url(),
            evidence=[baseline, latest, *ctx.grabs[-1:]],
        )
