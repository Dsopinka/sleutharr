"""Diagnosis rules.

Adding a rule is a one-file change: drop a module in this package defining a `Rule`
subclass, and add it to `RULES` below in priority order. First match wins.
"""

from __future__ import annotations

from core.rules.base import Rule, RuleContext, Verdict
from core.rules.r01_never_added import NeverAdded
from core.rules.r02_unmonitored import Unmonitored
from core.rules.r03_blocklist_loop import BlocklistLoop
from core.rules.r04_downloaded_not_imported import DownloadedNotImported
from core.rules.r05_grabbed_but_stalled import GrabbedButStalled
from core.rules.r06_no_release_found import NoReleaseFound
from core.rules.r07_imported_not_in_plex import ImportedNotInPlex
from core.rules.r08_wrong_quality import WrongQuality

# Order matters: first match wins, so the most specific and most upstream causes come
# first. A request that was never added to Sonarr cannot also be "stalled in the client",
# and reporting the downstream symptom would send the user to the wrong app.
RULES: list[Rule] = [
    NeverAdded(),
    Unmonitored(),
    BlocklistLoop(),
    DownloadedNotImported(),
    GrabbedButStalled(),
    ImportedNotInPlex(),
    WrongQuality(),
    NoReleaseFound(),
]

__all__ = ["RULES", "Rule", "RuleContext", "Verdict"]
