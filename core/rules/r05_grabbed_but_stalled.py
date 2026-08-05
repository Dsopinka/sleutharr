"""Rule 3: grabbed and in the client, but not actually progressing."""

from __future__ import annotations

from datetime import timedelta

from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class GrabbedButStalled(Rule):
    code = "GRABBED_BUT_STALLED"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        samples = ctx.download_samples
        if not samples:
            return None

        latest = samples[-1]
        torrent = latest.raw if isinstance(latest.raw, dict) else {}
        if not torrent:
            return None

        progress = float(torrent.get("progress") or 0)
        state = str(torrent.get("state") or "")
        num_seeds = int(torrent.get("num_seeds") or 0)
        num_complete = int(torrent.get("num_complete", -1) or -1)
        dlspeed = int(torrent.get("dlspeed") or 0)
        name = str(torrent.get("name") or "the release")

        if progress >= 1.0:
            return None  # completion is rule 4's problem, not this one

        if state in {"error", "missingFiles"}:
            return self.verdict(
                f"The download client reports state '{state}' for {name}.",
                next_step=(
                    "Open the download client and inspect the torrent. 'missingFiles' "
                    "means its data was moved or deleted underneath it; 'error' usually "
                    "means the save path is unwritable. Recheck or re-download."
                ),
                link=ctx.arr_queue_url(),
                evidence=[latest],
                severity=Severity.ERROR,
                code="DOWNLOAD_CLIENT_ERROR",
            )

        if state in {"pausedDL", "stoppedDL"}:
            return self.verdict(
                f"{name} is paused in the download client at {progress * 100:.1f}%.",
                next_step="Resume it in the download client.",
                link=ctx.arr_queue_url(),
                evidence=[latest],
            )

        stall_hours = float(ctx.setting("stall_hours") or 6)
        min_delta = float(ctx.setting("stall_min_progress_delta") or 0.01)
        window_start = ctx.now - timedelta(hours=stall_hours)

        # Zero seeds is conclusive on its own -- no need to wait out the window.
        # num_complete of -1 means the tracker withheld the swarm count, which is
        # "unknown", not "zero": treating it as zero would flag healthy private torrents.
        swarm_known = num_complete >= 0
        no_seeds = (num_complete == 0 and num_seeds == 0) if swarm_known else (
            num_seeds == 0 and dlspeed == 0
        )

        if no_seeds:
            swarm = (
                f"{num_complete} seeds in the swarm"
                if swarm_known
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

        # Compare the latest reading against a baseline at least `stall_hours` old.
        # Taking the oldest sample *inside* the window would be backwards: it excludes
        # the very baseline needed to measure a full window of no progress.
        older = [e for e in samples if e.occurred_at <= window_start]
        if not older:
            return None  # not enough history yet to call it stalled

        baseline = older[-1]
        old_raw = baseline.raw if isinstance(baseline.raw, dict) else {}
        old_progress = float(old_raw.get("progress") or 0)
        delta = progress - old_progress
        elapsed = (latest.occurred_at - baseline.occurred_at).total_seconds() / 3600

        if delta > min_delta:
            return None

        stalled_state = state in {"stalledDL", "metaDL"}
        descriptor = "stalled" if stalled_state else "barely moving"
        extra = (
            " It is still fetching metadata, which means it has never connected to a peer."
            if state == "metaDL"
            else ""
        )

        return self.verdict(
            f"{name} has been {descriptor} at {progress * 100:.1f}% for "
            f"{elapsed:.1f}h (+{delta * 100:.2f}% over the window), "
            f"{num_seeds} seed(s) connected.{extra}",
            next_step=(
                "Remove it from the queue and blocklist the release so the *arr searches "
                "again. If many downloads stall at once, check the client's connection "
                "limits, port forwarding, and whether the VPN is still up."
            ),
            link=ctx.arr_queue_url(),
            evidence=[baseline, latest, *ctx.grabs[-1:]],
        )
