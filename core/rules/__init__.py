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
from core.rules.r07_not_in_media_server import NotInMediaServer
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
    NotInMediaServer(),
    WrongQuality(),
    NoReleaseFound(),
]

__all__ = ["RULES", "Rule", "RuleContext", "Verdict"]


#: Plain-English label for each diagnosis code.
#:
#: The codes are stable identifiers meant for searching and scripting; they are not
#: something a person should have to decode at a glance. The UI leads with these and
#: keeps the code as secondary text.
DIAGNOSIS_TITLES: dict[str, str] = {
    "NEVER_ADDED": "Never reached your library",
    "NO_ARR_INSTANCE": "Not linked to Sonarr or Radarr",
    "DECLINED": "Someone declined it",
    "UNMONITORED": "Not being watched for",
    "BLOCKLIST_LOOP": "Keeps downloading the same broken file",
    "DOWNLOADED_NOT_IMPORTED": "Downloaded, but could not be filed away",
    "DOWNLOAD_CLIENT_ERROR": "The download client hit an error",
    "GRABBED_BUT_STALLED": "Download is stuck",
    "DOWNLOADER_NOT_PROGRESSING": "Your download client is not making progress",
    "PATH_MISMATCH": "Folder paths do not line up",
    "NOT_IN_MEDIA_SERVER": "Not showing up in your media server",
    "WRONG_QUALITY": "Lower quality than you asked for",
    "NOT_RELEASED_YET": "Not out yet",
    "NEVER_SEARCHED": "Never actually searched for",
    "NO_RELEASE_FOUND": "Nothing good enough found yet",
    "FULFILLED": "Done",
}


def title_for(code: str) -> str:
    """Human label for a diagnosis code, falling back to a readable form of the code."""
    if code in DIAGNOSIS_TITLES:
        return DIAGNOSIS_TITLES[code]
    return code.replace("_", " ").capitalize()
