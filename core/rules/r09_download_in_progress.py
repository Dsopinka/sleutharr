"""Rule 9: it is downloading, and that is the whole answer.

Runs last, so it can only ever speak when every rule that looks for a fault has passed.
By that point a download which is measurably moving is not an open question -- it is a
file on its way, and saying so is the honest reading of the evidence.

Reported from a live instance: a 26 GB film sat at "Still checking -- Sleutharr has not
worked this one out yet" while the timeline directly beneath it showed 36%, article
health 100% and eleven minutes remaining. Nothing was wrong with the diagnosis; there
simply was not one, because no rule claims a request that is behaving. The effect is
worse than silence, because it reads as confusion rather than as progress, and it puts a
perfectly healthy download in the list of things needing attention.
"""

from __future__ import annotations

from core.models import Severity
from core.rules.base import Rule, RuleContext, Verdict


def _human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes:.0f} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def _human_eta(seconds: float) -> str:
    if seconds < 90:
        return "less than a minute"
    minutes = seconds / 60
    if minutes < 90:
        return f"about {minutes:.0f} minutes"
    hours = minutes / 60
    if hours < 36:
        return f"about {hours:.0f} hours"
    return f"about {hours / 24:.1f} days"


class DownloadInProgress(Rule):
    code = "DOWNLOAD_IN_PROGRESS"
    severity = Severity.INFO

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        samples = ctx.download_samples
        if not samples:
            return None

        latest = samples[-1]
        facts = latest.facts if isinstance(latest.facts, dict) else {}
        if not facts:
            return None

        # Same guard as every other rule that reads samples: once the client stops
        # answering, the newest reading keeps ageing, and "it is downloading" would
        # become a statement about our records rather than about the download.
        if not ctx.can_speak_for(latest.service):
            return None

        if facts.get("is_complete") or facts.get("is_errored") or facts.get("is_paused"):
            return None
        if facts.get("unhealthy_articles"):
            return None

        progress = float(facts.get("progress") or 0)
        if not 0 < progress < 1:
            return None

        # "Moving" has to be measured, not assumed. A client reporting `downloading`
        # forever is exactly the case rule 5 exists for, and it has already declined to
        # fire -- which may only mean there is not enough history yet to call it stalled.
        # Requiring visible movement keeps this rule from filling that gap with optimism.
        rate = float(facts.get("download_rate") or 0)
        earlier = [
            e for e in samples[:-1] if isinstance(e.facts, dict) and e.facts
        ]
        gained = (
            progress - float(earlier[0].facts.get("progress") or 0) if earlier else 0.0
        )
        if rate <= 0 and gained <= 0:
            return None

        name = str(facts.get("name") or "It")
        left = float(facts.get("left") or 0)

        detail = f"{progress * 100:.0f}% done"
        if left > 0:
            detail += f", {_human_size(left)} to go"
        if rate > 0 and left > 0:
            detail += f", {_human_eta(left / rate)} left at {_human_size(rate)}/s"

        return self.verdict(
            f"{name} is downloading normally — {detail}.",
            next_step=(
                "Nothing to do. It is coming down; this entry will clear itself once "
                "the download finishes and your library imports it."
            ),
            link=ctx.arr_queue_url(),
            evidence=[latest],
        )
