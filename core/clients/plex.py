"""Plex Media Server client.

Two join paths, both used:

1. the request manager's stored `ratingKey`, which is authoritative when present;
2. a path match from the *arr's imported file path to `Media[].Part[].file`.

Path matching needs the configured `PathMapping` rewrites because the two apps mount the
same storage at different points. Crucially, running *both* joins is what lets us tell
"Plex has not scanned yet" apart from "your path mapping is wrong": if the ratingKey
resolves but no path matches, the file is there and the mapping is the problem.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from core.clients.base import BaseClient, ProbeResult, ServiceError

logger = logging.getLogger(__name__)

# Plex library item types.
TYPE_MOVIE = 1
TYPE_EPISODE = 4


@dataclass(slots=True)
class PlexItem:
    rating_key: str
    title: str
    type: str
    library_section_id: str
    added_at: int
    paths: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


def normalise_path(path: str) -> str:
    """Case-fold and strip trailing separators for comparison.

    Plex on Windows and on a case-insensitive macOS volume will disagree with the *arr on
    case, which would otherwise read as a mismatch.
    """
    return posixpath.normpath(path.replace("\\", "/")).lower().rstrip("/")


class PlexClient(BaseClient):
    api_prefix = ""

    def default_headers(self) -> dict[str, str]:
        # Plex answers XML unless you ask for JSON explicitly.
        return {
            "Accept": "application/json",
            "User-Agent": "Sleutharr",
            "X-Plex-Token": self.service.api_key,
            "X-Plex-Client-Identifier": "sleutharr",
            "X-Plex-Product": "Sleutharr",
        }

    def _auth_message(self, response) -> str:
        return (
            f"Plex rejected the token (HTTP {response.status_code}). "
            "Check X-Plex-Token."
        )

    # -- health ----------------------------------------------------------------

    def probe(self) -> ProbeResult:
        # /identity is reachable without a token, so a failure here is a network problem
        # while a failure on /library/sections is an auth problem. Separating them makes
        # the health page actionable instead of just red.
        identity = self.get_json("/identity") or {}
        container = identity.get("MediaContainer") or {}
        version = str(container.get("version") or "")
        sections = self.get_json("/library/sections") or {}
        dirs = (sections.get("MediaContainer") or {}).get("Directory") or []
        return ProbeResult(
            ok=True,
            detail=f"Plex {version} - {len(dirs)} librar{'y' if len(dirs) == 1 else 'ies'}",
            version=version,
        )

    # -- library ---------------------------------------------------------------

    def sections(self) -> list[dict]:
        data = self.get_json("/library/sections") or {}
        dirs = (data.get("MediaContainer") or {}).get("Directory") or []
        return [d for d in dirs if isinstance(d, dict)]

    def item(self, rating_key: str) -> PlexItem | None:
        if not rating_key:
            return None
        try:
            data = self.get_json(f"/library/metadata/{rating_key}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
        rows = (data or {}).get("MediaContainer", {}).get("Metadata") or []
        if not rows:
            return None
        return self._parse_item(rows[0])

    def iter_section_items(
        self, section_key: str, item_type: int, page_size: int = 500
    ) -> Iterator[PlexItem]:
        """Page through a library section.

        Pagination is via X-Plex-Container-Start / X-Plex-Container-Size headers.
        """
        start = 0
        while True:
            data = self.get_json(
                f"/library/sections/{section_key}/all",
                params={"type": item_type},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(page_size),
                },
            )
            container = (data or {}).get("MediaContainer") or {}
            rows = container.get("Metadata") or []
            if not rows:
                return
            for row in rows:
                if isinstance(row, dict):
                    yield self._parse_item(row)
            total = container.get("totalSize", container.get("size"))
            start += len(rows)
            if total is None or start >= int(total) or len(rows) < page_size:
                return

    @staticmethod
    def _parse_item(row: dict) -> PlexItem:
        paths: list[str] = []
        for media in row.get("Media") or []:
            if not isinstance(media, dict):
                continue
            for part in media.get("Part") or []:
                if isinstance(part, dict) and part.get("file"):
                    paths.append(str(part["file"]))
        return PlexItem(
            rating_key=str(row.get("ratingKey") or ""),
            title=str(row.get("title") or row.get("grandparentTitle") or ""),
            type=str(row.get("type") or ""),
            library_section_id=str(row.get("librarySectionID") or ""),
            added_at=int(row.get("addedAt") or 0),
            paths=paths,
            raw=row,
        )

    # -- path index ------------------------------------------------------------

    def build_path_index(self) -> dict[str, PlexItem]:
        """Map every known Plex file path to its item.

        Items whose `Media`/`Part` is absent (unavailable files, some agents) are skipped
        rather than recorded as empty -- an absent path is not evidence of a mismatch.
        """
        index: dict[str, PlexItem] = {}
        for section in self.sections():
            key = str(section.get("key") or "")
            stype = str(section.get("type") or "")
            if not key:
                continue
            if stype == "movie":
                item_type = TYPE_MOVIE
            elif stype == "show":
                item_type = TYPE_EPISODE
            else:
                continue
            try:
                for item in self.iter_section_items(key, item_type):
                    for path in item.paths:
                        index[normalise_path(path)] = item
            except ServiceError as exc:
                logger.warning(
                    "Plex section %s (%s) failed to index: %s", key, stype, exc
                )
        return index

    def item_url(self, rating_key: str) -> str:
        if not rating_key:
            return f"{self.service.url}/web"
        return (
            f"{self.service.url}/web/index.html#!/server/-/details"
            f"?key=%2Flibrary%2Fmetadata%2F{rating_key}"
        )


@dataclass(slots=True)
class PathMatchResult:
    """Outcome of matching *arr paths against the Plex path index."""

    matched_path: str = ""
    item: PlexItem | None = None
    #: A Plex path with the same basename under a different directory. Positive evidence
    #: that the mapping is wrong rather than the file being absent.
    basename_candidate: str = ""
    #: The *arr path (after rewriting) that we looked for and did not find.
    attempted: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.item is not None


def match_paths(
    arr_paths: Iterable[str],
    index: dict[str, PlexItem],
    mappings: Iterable,
) -> PathMatchResult:
    """Translate *arr paths through the mapping table and look them up in Plex.

    When the exact path misses, we look for the same *filename* elsewhere in the index.
    A basename hit means the file is genuinely in Plex under a different prefix -- which
    is a mapping bug, and we can then name the exact prefix pair that would fix it.
    """
    mappings = list(mappings)
    attempted: list[str] = []
    basename_candidate = ""

    by_basename: dict[str, str] | None = None

    for raw_path in arr_paths:
        if not raw_path:
            continue
        translated = raw_path
        for mapping in mappings:
            translated = mapping.apply(translated)
        attempted.append(translated)

        hit = index.get(normalise_path(translated))
        if hit is not None:
            return PathMatchResult(
                matched_path=translated, item=hit, attempted=attempted
            )

        if by_basename is None:
            by_basename = {}
            for key in index:
                by_basename.setdefault(posixpath.basename(key), key)
        candidate = by_basename.get(posixpath.basename(normalise_path(translated)))
        if candidate and not basename_candidate:
            basename_candidate = candidate

    return PathMatchResult(
        basename_candidate=basename_candidate, attempted=attempted
    )


def suggest_mapping(arr_path: str, plex_path: str) -> tuple[str, str] | None:
    """Derive the prefix pair that would make `arr_path` resolve to `plex_path`.

    Both paths end in the same filename; the differing head is the mapping. Returning the
    concrete pair turns "check your path mappings" into a change the user can paste in.
    """
    a = arr_path.replace("\\", "/").rstrip("/").split("/")
    b = plex_path.replace("\\", "/").rstrip("/").split("/")

    common = 0
    while common < min(len(a), len(b)) and a[len(a) - 1 - common].lower() == b[
        len(b) - 1 - common
    ].lower():
        common += 1
    if common == 0:
        return None

    # Back off the greedy match while it would leave either side with an empty prefix.
    # A directory name that happens to appear on both sides (".../media/movies/X" vs
    # "/movies/X") otherwise gets consumed too, yielding a technically-valid but
    # confusing suggestion like "/data/media -> /". Prefer the pair a human would write.
    def prefixes(n: int) -> tuple[str, str]:
        return "/".join(a[: len(a) - n]), "/".join(b[: len(b) - n])

    while common > 1:
        source, target = prefixes(common)
        if source and target:
            break
        common -= 1

    source, target = prefixes(common)
    source = source or "/"
    target = target or "/"
    if source == target:
        return None
    return source, target
