"""Media server clients: Plex, Jellyfin, Emby.

All three answer the same two questions -- "do you have this item?" and "what path do you
know it by?" -- so they sit behind one interface and the Plex-specific nesting stays in
the Plex class.

Running the id join and the path join independently is what separates "the server has not
scanned yet" from "your path mapping is wrong". Plex offers a rating key for the id join;
Jellyfin and Emby offer ProviderIds, which is arguably better because it maps straight to
tmdb/tvdb without parsing a guid string.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from core.clients.base import BaseClient, ProbeResult, ServiceError
from core.models import ServiceVariant

logger = logging.getLogger(__name__)

# Plex library item types.
PLEX_TYPE_MOVIE = 1
PLEX_TYPE_EPISODE = 4


@dataclass(slots=True)
class MediaItem:
    """One item in a media server library, normalised across products."""

    item_id: str
    title: str
    item_type: str
    paths: list[str] = field(default_factory=list)
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    added_at: int = 0
    raw: dict = field(default_factory=dict)


def normalise_path(path: str) -> str:
    """Case-fold and strip trailing separators for comparison.

    A server on Windows or a case-insensitive macOS volume will disagree with the *arr on
    case, which would otherwise read as a mismatch.
    """
    return posixpath.normpath(path.replace("\\", "/")).lower().rstrip("/")


@dataclass(slots=True)
class PathMatchResult:
    """Outcome of matching *arr paths against the media server's path index."""

    matched_path: str = ""
    item: MediaItem | None = None
    #: A server path with the same basename under a different directory. Positive evidence
    #: that the mapping is wrong rather than the file being absent.
    basename_candidate: str = ""
    #: The *arr paths (after rewriting) that we looked for and did not find.
    attempted: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.item is not None


class MediaServerClient(BaseClient):
    """Interface every media server implements."""

    #: Human label for messages.
    product: str = "media server"

    def item(self, item_id: str) -> MediaItem | None:
        """Look up one item by its server-native id."""
        raise NotImplementedError

    def iter_items(self) -> Iterator[MediaItem]:
        """Every movie and episode in the library."""
        raise NotImplementedError

    def item_url(self, item_id: str) -> str:
        raise NotImplementedError

    def build_path_index(self) -> dict[str, MediaItem]:
        """Map every known file path to its item.

        Items with no path are skipped rather than recorded as empty -- an absent path is
        not evidence of a mismatch, just of an item the server cannot see on disk.
        """
        index: dict[str, MediaItem] = {}
        for item in self.iter_items():
            for path in item.paths:
                if path:
                    index[normalise_path(path)] = item
        return index


class PlexClient(MediaServerClient):
    api_prefix = ""
    product = "Plex"

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

    def probe(self) -> ProbeResult:
        # /identity needs no token, so a failure there is a network problem while a
        # failure on /library/sections is an auth problem. Separating them makes the
        # health page actionable instead of just red.
        identity = self.get_json("/identity") or {}
        container = identity.get("MediaContainer") or {}
        version = str(container.get("version") or "")
        dirs = self.sections()
        return ProbeResult(
            ok=True,
            detail=f"Plex {version} - {len(dirs)} librar{'y' if len(dirs) == 1 else 'ies'}",
            version=version,
        )

    def sections(self) -> list[dict]:
        data = self.get_json("/library/sections") or {}
        dirs = (data.get("MediaContainer") or {}).get("Directory") or []
        return [d for d in dirs if isinstance(d, dict)]

    def item(self, item_id: str) -> MediaItem | None:
        if not item_id:
            return None
        try:
            data = self.get_json(f"/library/metadata/{item_id}")
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
        rows = (data or {}).get("MediaContainer", {}).get("Metadata") or []
        return self._parse_item(rows[0]) if rows else None

    def iter_items(self) -> Iterator[MediaItem]:
        for section in self.sections():
            key = str(section.get("key") or "")
            stype = str(section.get("type") or "")
            if not key:
                continue
            if stype == "movie":
                item_type = PLEX_TYPE_MOVIE
            elif stype == "show":
                item_type = PLEX_TYPE_EPISODE
            else:
                continue
            try:
                yield from self._iter_section(key, item_type)
            except ServiceError as exc:
                logger.warning("Plex section %s (%s) failed to index: %s", key, stype, exc)

    def _iter_section(
        self, section_key: str, item_type: int, page_size: int = 500
    ) -> Iterator[MediaItem]:
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
    def _parse_item(row: dict) -> MediaItem:
        # Capitalisation matters: Media and Part are capitalised, file is not.
        paths: list[str] = []
        for media in row.get("Media") or []:
            if not isinstance(media, dict):
                continue
            for part in media.get("Part") or []:
                if isinstance(part, dict) and part.get("file"):
                    paths.append(str(part["file"]))
        return MediaItem(
            item_id=str(row.get("ratingKey") or ""),
            title=str(row.get("title") or row.get("grandparentTitle") or ""),
            item_type=str(row.get("type") or ""),
            paths=paths,
            added_at=int(row.get("addedAt") or 0),
            raw=row,
        )

    def item_url(self, item_id: str) -> str:
        if not item_id:
            return f"{self.service.url}/web"
        return (
            f"{self.service.url}/web/index.html#!/server/-/details"
            f"?key=%2Flibrary%2Fmetadata%2F{item_id}"
        )


class JellyfinClient(MediaServerClient):
    """Jellyfin, and Emby via the same surface.

    Both accept `X-Emby-Token`. Jellyfin also takes the more elaborate
    `Authorization: MediaBrowser Token="..."` form, but the simple header is a documented
    fallback and keeps one implementation serving both products.
    """

    api_prefix = ""
    product = "Jellyfin"

    #: Item types we care about. Everything else in the library is noise for our purpose.
    item_types = "Movie,Episode"

    def default_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": "Sleutharr",
            "X-Emby-Token": self.service.api_key,
        }

    def _auth_message(self, response) -> str:
        return (
            f"{self.product} rejected the API key (HTTP {response.status_code}). "
            "Check the key and that it has library access."
        )

    def probe(self) -> ProbeResult:
        # The public endpoint needs no key, so reaching it proves the network path while
        # the authenticated one proves the key -- two distinct failures, two messages.
        public = self.get_json("/System/Info/Public") or {}
        version = str(public.get("Version") or "")
        product = str(public.get("ServerName") or self.product)
        info = self.get_json("/System/Info") or {}
        if not isinstance(info, dict):
            return ProbeResult(ok=False, detail="Unexpected /System/Info response.")
        return ProbeResult(
            ok=True, detail=f"{product} {version}".strip(), version=version
        )

    def item(self, item_id: str) -> MediaItem | None:
        if not item_id:
            return None
        try:
            data = self.get_json(
                "/Items",
                params={"ids": item_id, "fields": "Path,ProviderIds", "recursive": "true"},
            )
        except ServiceError as exc:
            if exc.status == 404:
                return None
            raise
        rows = (data or {}).get("Items") or []
        return self._parse_item(rows[0]) if rows else None

    def series_provider_ids(self) -> dict[str, tuple[int | None, int | None]]:
        """Map each series' item id to the series' own tmdb/tvdb ids.

        Needed because an episode's `ProviderIds` are the *episode's* ids, in a numbering
        space of their own -- see `_parse_item`. The series is the only place the id a
        request actually carries can be read from.
        """
        out: dict[str, tuple[int | None, int | None]] = {}
        for row in self._walk("Series"):
            item = self._parse_item(row)
            if item.item_id:
                out[item.item_id] = (item.tmdb_id, item.tvdb_id)
        return out

    def _walk(self, item_types: str, page_size: int = 500) -> Iterator[dict]:
        """Page through /Items, refusing to stop quietly on a response we cannot read.

        A silent `return` here would hand back a short library and no indication that it
        was short -- and a short library is read downstream as files the server does not
        have, which is a diagnosis about the user's setup rather than about our own
        failure to finish reading.
        """
        start = 0
        while True:
            data = self.get_json(
                "/Items",
                params={
                    "recursive": "true",
                    "includeItemTypes": item_types,
                    "fields": "Path,ProviderIds",
                    "startIndex": start,
                    "limit": page_size,
                    "enableImages": "false",
                    "enableUserData": "false",
                },
            )
            if not isinstance(data, dict):
                raise ServiceError(
                    f"{self.product} returned {type(data).__name__} rather than an "
                    f"object for /Items at offset {start}. Treating that as an empty "
                    "library would report every file as missing."
                )
            rows = data.get("Items") or []
            if not rows:
                return
            for row in rows:
                if isinstance(row, dict):
                    yield row
            total = data.get("TotalRecordCount")
            start += len(rows)
            if total is None or start >= int(total) or len(rows) < page_size:
                return

    def iter_items(self, page_size: int = 500) -> Iterator[MediaItem]:
        # Fetched up front rather than per episode: it is one call for the whole library,
        # and an episode alone cannot say what series id it belongs to in tvdb terms.
        series_ids = self.series_provider_ids()
        for row in self._walk(self.item_types, page_size):
            yield self._parse_item(row, series_ids)

    @staticmethod
    def _parse_item(
        row: dict, series_ids: dict[str, tuple[int | None, int | None]] | None = None
    ) -> MediaItem:
        # Unlike Plex, the path is a flat string on the item itself.
        paths = [str(row["Path"])] if row.get("Path") else []
        for source in row.get("MediaSources") or []:
            if isinstance(source, dict) and source.get("Path"):
                path = str(source["Path"])
                if path not in paths:
                    paths.append(path)

        providers = row.get("ProviderIds") or {}
        # Provider keys are inconsistently cased across versions and products.
        lowered = {str(k).lower(): v for k, v in providers.items()}

        def as_int(key: str) -> int | None:
            value = lowered.get(key)
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        item_type = str(row.get("Type") or "")
        tmdb_id, tvdb_id = as_int("tmdb"), as_int("tvdb")

        if item_type == "Episode":
            # Confirmed against a live Jellyfin 10.11.11: an episode's ProviderIds are
            # the *episode's* ids. "From Pole to Pole" reports Tvdb=306329 while Planet
            # Earth, the series, reports Tvdb=79257 -- and a request only ever carries
            # the series id, because that is what Seerr stores and what Sonarr looks
            # series up by.
            #
            # Comparing the two is not merely a join that fails to match. Both are bare
            # integers drawn from overlapping ranges, so a request for one show can match
            # an episode of a completely different one, and be reported as present in the
            # library. An episode therefore takes its series' ids or none at all.
            tmdb_id, tvdb_id = (series_ids or {}).get(
                str(row.get("SeriesId") or ""), (None, None)
            )

        return MediaItem(
            item_id=str(row.get("Id") or ""),
            title=str(row.get("Name") or ""),
            item_type=item_type,
            paths=paths,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            raw=row,
        )

    def item_url(self, item_id: str) -> str:
        if not item_id:
            return f"{self.service.url}/web/index.html"
        return f"{self.service.url}/web/index.html#!/details?id={item_id}"


class EmbyClient(JellyfinClient):
    product = "Emby"


MEDIA_SERVER_BY_VARIANT: dict[str, type[MediaServerClient]] = {
    ServiceVariant.PLEX: PlexClient,
    ServiceVariant.JELLYFIN: JellyfinClient,
    ServiceVariant.EMBY: EmbyClient,
}


def media_server_client(service) -> MediaServerClient:
    cls = MEDIA_SERVER_BY_VARIANT.get(service.variant)
    if cls is None:
        # Plex was the only media server before variants existed; anything unlabelled is
        # far more likely to be Plex than anything else.
        logger.warning(
            "Media server variant %r not recognised; assuming Plex.", service.variant
        )
        cls = PlexClient
    return cls(service)


# --------------------------------------------------------------------- path matching


def match_paths(
    arr_paths: Iterable[str],
    index: dict[str, MediaItem],
    mappings: Iterable,
) -> PathMatchResult:
    """Translate *arr paths through the mapping table and look them up.

    When the exact path misses, we look for the same *filename* elsewhere in the index.
    A basename hit means the file really is there under a different prefix -- a mapping
    bug -- and we can then name the exact prefix pair that would fix it.
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
            return PathMatchResult(matched_path=translated, item=hit, attempted=attempted)

        if by_basename is None:
            by_basename = {}
            for key in index:
                by_basename.setdefault(posixpath.basename(key), key)
        candidate = by_basename.get(posixpath.basename(normalise_path(translated)))
        if candidate and not basename_candidate:
            basename_candidate = candidate

    return PathMatchResult(basename_candidate=basename_candidate, attempted=attempted)


def suggest_mapping(arr_path: str, server_path: str) -> tuple[str, str] | None:
    """Derive the prefix pair that would make `arr_path` resolve to `server_path`.

    Both paths end in the same filename; the differing head is the mapping. Returning the
    concrete pair turns "check your path mappings" into a change the user can paste in.
    """
    a = arr_path.replace("\\", "/").rstrip("/").split("/")
    b = server_path.replace("\\", "/").rstrip("/").split("/")

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
