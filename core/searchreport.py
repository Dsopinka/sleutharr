"""Explain why nothing is being grabbed, using the *arr's own rejection reasons.

"Check your quality profile" is not an answer. Sonarr and Radarr already evaluate every
release they find and record exactly why each was rejected -- size limits, custom format
scores, language, propers, existing files. Surfacing that turns a vague instruction into
a specific finding: "11 of 14 releases were rejected because they exceed the size limit
on HD-1080p."

This runs an interactive search, which queries every enabled indexer live. It is slow and
it costs the indexers something, so it happens only when a user asks.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from core.clients.arr import arr_client
from core.clients.base import ServiceError

logger = logging.getLogger(__name__)

#: Rejection strings are free text, but they cluster into a small number of real causes.
#: These map the phrasing Sonarr and Radarr actually use onto a plain sentence naming the
#: setting responsible.
#:
#: Order matters and is specific-first. "Quality WEBDL-2160p is larger than the maximum
#: allowed 20 GB" mentions quality but is really about a size limit, so the size patterns
#: have to be tested before the quality one or every size rejection is mislabelled.
REASON_HINTS: list[tuple[tuple[str, ...], str]] = [
    (
        ("larger than the maximum", "smaller than the minimum", "size limit"),
        "The size limits on your quality profile exclude it. Raise or clear the "
        "maximum/minimum size for that quality.",
    ),
    (
        ("custom format",),
        "It scored below the minimum custom format score on your quality profile.",
    ),
    (
        ("not an upgrade", "existing file", "existing episode file"),
        "You already have a file this would not improve on.",
    ),
    (
        ("not wanted", "quality",),
        "That quality is not enabled in your quality profile.",
    ),
    (("language",), "Not in a language your profile accepts."),
    (("proper", "repack"), "Excluded by your propers/repacks preference."),
    (("seeder", "peer"), "Too few seeders for the indexer's minimum."),
    (("age", "retention"), "Older than the retention or age limit."),
    (
        ("release group", "blocklist", "blocked", "release profile"),
        "Blocked by a release profile or blocklist rule.",
    ),
    (("indexer",), "The indexer itself refused it."),
]


def _hint_for(reason: str) -> str:
    lowered = reason.lower()
    for needles, hint in REASON_HINTS:
        if any(needle in lowered for needle in needles):
            return hint
    return ""


@dataclass
class RejectionGroup:
    reason: str
    count: int
    hint: str = ""
    examples: list[str] = field(default_factory=list)


@dataclass
class SearchReport:
    ok: bool = True
    total: int = 0
    accepted: int = 0
    groups: list[RejectionGroup] = field(default_factory=list)
    error: str = ""
    summary: str = ""

    @property
    def all_rejected(self) -> bool:
        return self.total > 0 and self.accepted == 0


def run_search_report(tracked) -> SearchReport:
    """Interactive search for one request, summarised by why things were rejected."""
    service = tracked.arr_service
    if service is None or not tracked.arr_entity_id:
        return SearchReport(
            ok=False, error="This request is not linked to a Sonarr/Radarr record."
        )

    client = arr_client(service)
    try:
        with client:
            releases = client.releases(tracked.arr_entity_id)
        client.record_success()
    except ServiceError as exc:
        client.record_failure(exc)
        return SearchReport(ok=False, error=f"{service.name} could not search: {exc}")
    except Exception as exc:  # noqa: BLE001
        client.record_failure(exc)
        return SearchReport(ok=False, error=f"{service.name} could not search: {exc}")
    finally:
        client.close()

    if not releases:
        return SearchReport(
            ok=True,
            total=0,
            summary=(
                "Your indexers returned nothing at all for this. That points at indexer "
                "coverage rather than your quality settings — the release may simply not "
                "be on the indexers you use."
            ),
        )

    counter: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    accepted = 0

    for release in releases:
        if not isinstance(release, dict):
            continue
        if not release.get("rejected"):
            accepted += 1
            continue
        reasons = release.get("rejections") or ["Rejected for an unstated reason"]
        title = str(release.get("title") or "")[:120]
        for reason in reasons:
            reason = str(reason).strip()
            if not reason:
                continue
            counter[reason] += 1
            examples.setdefault(reason, [])
            if len(examples[reason]) < 3 and title:
                examples[reason].append(title)

    groups = [
        RejectionGroup(
            reason=reason,
            count=count,
            hint=_hint_for(reason),
            examples=examples.get(reason, []),
        )
        for reason, count in counter.most_common(12)
    ]

    total = len(releases)
    if accepted:
        summary = (
            f"{accepted} of {total} releases would be accepted. If nothing has been "
            f"grabbed, the *arr may simply not have searched yet."
        )
    else:
        top = groups[0] if groups else None
        summary = (
            f"All {total} releases found were rejected."
            + (
                f" The most common reason ({top.count} of them): {top.reason}"
                if top
                else ""
            )
        )

    return SearchReport(
        ok=True, total=total, accepted=accepted, groups=groups, summary=summary
    )
