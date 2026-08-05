# API notes

Verified against live specs/sources on **2026-08-04**. Everything below was checked
against the upstream OpenAPI schema or the upstream entity source, not from memory.
Where the published documentation disagrees with the source, **the source wins** and the
discrepancy is called out.

No live instances were configured at build time, so nothing here is confirmed against a
running server. Items marked **[UNVERIFIED-LIVE]** are the ones most worth re-checking
once real credentials exist; `python manage.py probe_services` re-runs those checks and
prints what it actually sees.

| Service | Source of truth | Version checked |
|---|---|---|
| Seerr | `seerr-api.yml` + `server/entity/*.ts` @ `develop` | v3.4.1 (2026-07-30) |
| Overseerr | `overseerr-api.yml` @ `develop` | v1.33.x |
| Jellyseerr | `jellyseerr-api.yml` @ `develop` | v2.x |
| Sonarr | `src/Sonarr.Api.V3/openapi.json` @ `develop` | API v3 |
| Radarr | `src/Radarr.Api.V3/openapi.json` @ `develop` | API v3 |
| qBittorrent | Official wiki `WebUI-API-(qBittorrent-5.0)` | WebUI API v2 (qBt ≥5.0) |
| Plex | `python-plexapi` master (`media.py`, `library.py`, `server.py`) | PMS 1.4x |

---

## 1. Seerr / Overseerr / Jellyseerr

Base path `/api/v1`. Auth header `X-Api-Key` (spec also allows a `connect.sid` cookie;
we only use the API key). Health/version probe: `GET /status`.

### GET /request

Confirmed query params — note this is **`take`/`skip`, not `page`/`pageSize`**:

| Param | Values |
|---|---|
| `take` | number (page size) |
| `skip` | number (offset) |
| `filter` | `all,approved,available,pending,processing,unavailable,failed,deleted,completed` |
| `sort` | `added,modified` (default `added`) |
| `sortDirection` | `asc,desc` (default `desc`) |
| `requestedBy` | user id |
| `mediaType` | `movie,tv,all` |

Response: `{ pageInfo: {page, pages, results}, results: MediaRequest[] }`.

`pageInfo.pages` is a **page count**, and pages are addressed via `skip`, so paginate with
`skip += take` until `skip >= pageInfo.results`. Do not assume a `page` param exists.

### ⚠️ Finding 1 — the published OpenAPI `MediaInfo` schema is incomplete

`seerr-api.yml` documents `MediaInfo` as only `{id, tmdbId, tvdbId, status, requests,
createdAt, updatedAt}`. **Every join key this application depends on is missing from the
published schema.** The real entity (`server/entity/Media.ts`) has them, and they are
present on the wire. Confirmed columns:

```
tmdbId, tvdbId, imdbId, mediaType
status,               status4k
serviceId,            serviceId4k             -- which Sonarr/Radarr instance
externalServiceId,    externalServiceId4k     -- Radarr movieId / Sonarr seriesId
externalServiceSlug,  externalServiceSlug4k   -- titleSlug, for deep links
ratingKey,            ratingKey4k             -- Plex rating key
jellyfinMediaId,      jellyfinMediaId4k
serviceUrl, serviceUrl4k, mediaUrl, mediaUrl4k  (computed, not columns)
```

Implication: do not code against the published schema. `docs/` and our client parse the
entity fields defensively and tolerate their absence.

### ⚠️ Finding 2 — every join key is doubled for 4K, and `is4k` selects the pair

This is the single most important correctness detail in the whole join chain, and it is
not obvious from the request payload. `MediaRequest.is4k` (boolean) decides whether the
request resolves through `serviceId`/`externalServiceId`/`ratingKey`/`status` **or**
through `serviceId4k`/`externalServiceId4k`/`ratingKey4k`/`status4k`.

A 4K request and a 1080p request for the same title share one `Media` row and routinely
point at **different Sonarr/Radarr instances**. Reading the non-4K fields for a 4K request
silently joins to the wrong instance and produces a confidently wrong diagnosis. All key
resolution goes through one helper (`core/clients/requestmanager.py::resolve_service_keys`)
so this can never be open-coded per call site.

### ⚠️ Finding 3 — `MediaStatus` in the published docs is stale and off by one

`seerr-api.yml` describes `MediaInfo.status` as
`1=UNKNOWN, 2=PENDING, 3=PROCESSING, 4=PARTIALLY_AVAILABLE, 5=AVAILABLE, 6=DELETED`.

`server/constants/media.ts` actually defines:

```ts
export enum MediaStatus {
  UNKNOWN = 1, PENDING, PROCESSING, PARTIALLY_AVAILABLE,
  AVAILABLE, BLOCKLISTED, DELETED,          // BLOCKLISTED=6, DELETED=7
}
```

**`6` is `BLOCKLISTED`, not `DELETED`; `DELETED` is `7`.** Following the docs would make us
report a blocklisted item as deleted — and blocklisted is a diagnosable state we care about
(rule 8) while deleted is terminal. We follow the source.

`MediaRequestStatus` is likewise under-documented — the spec lists only 3 values, the source
has 5:

```ts
export enum MediaRequestStatus { PENDING = 1, APPROVED, DECLINED, FAILED, COMPLETED }
```

`FAILED = 4` is what Seerr sets when the push to Sonarr/Radarr errored — a direct signal for
rule 1 (never added).

`MediaType` is a string enum: `movie` | `tv`.

### Variant differences

The three implementations share this surface. Known divergences we handle:

* Jellyseerr/Seerr add `jellyfinMediaId`/`jellyfinMediaId4k` and support Emby/Jellyfin;
  Overseerr is Plex-only and has no such field.
* Overseerr's `MediaStatus` has **no `BLOCKLISTED`** member, so on Overseerr `6 == DELETED`.
  Verified by diffing `server/constants/media.ts` across all three repos on 2026-08-04:

  | Variant | Members | `6` means | `7` means |
  |---|---|---|---|
  | Seerr | 7 | `BLOCKLISTED` | `DELETED` |
  | Jellyseerr | 7 | `BLOCKLISTED` | `DELETED` |
  | Overseerr | 6 | `DELETED` | — |

  The status enum is therefore resolved **per variant**, not globally — see
  `core/clients/requestmanager.py::MEDIA_STATUS_BY_VARIANT`. This is a genuine wire-format
  divergence between products that share an API shape, and getting it wrong flips a
  diagnosis (`BLOCKLISTED` is actionable, `DELETED` is terminal).
* `GET /request` is identical across all three.

---

## 2. Sonarr / Radarr (API v3)

Base path `/api/v3`. Auth header `X-Api-Key`. Version probe `GET /api/v3/system/status`.

### Per-entity history — the timeline spine

* Radarr: `GET /api/v3/history/movie?movieId={id}&includeMovie=false`
* Sonarr: `GET /api/v3/history/series?seriesId={id}[&seasonNumber=]&includeSeries=false`

Both are **unpaginated** — they return the entity's full history array. Only the global
`GET /api/v3/history` is paginated (`page`, `pageSize`, `sortKey`, `sortDirection`), and it
also accepts `downloadId`, `movieIds[]`/`seriesIds[]` and `eventType[]` filters.

`HistoryResource` fields (both apps): `id, sourceTitle, languages, quality, customFormats,
customFormatScore, qualityCutoffNotMet, date, downloadId, eventType, data` plus
`movieId`/`movie` (Radarr) or `episodeId, seriesId, episode, series` (Sonarr).

`data` is a loose string→string dict whose keys vary by `eventType`. Useful members seen:
`droppedPath`, `importedPath`, `downloadClient`, `indexer`, `releaseGroup`, `nzbInfoUrl`,
`reason`, `message`. Treat every key as optional.

### ⚠️ Finding 4 — the history event-type enums are *not* the same across the two apps

Sonarr `EpisodeHistoryEventType`:
`unknown, grabbed, seriesFolderImported, downloadFolderImported, downloadFailed,
episodeFileDeleted, episodeFileRenamed, downloadIgnored`

Radarr `MovieHistoryEventType`:
`unknown, grabbed, downloadFolderImported, downloadFailed, movieFileDeleted,
movieFolderImported, movieFileRenamed, downloadIgnored`

Three traps:

1. The import events have **different names** (`seriesFolderImported` vs
   `movieFolderImported`), and both apps *also* have a shared `downloadFolderImported`.
2. The delete events have different names (`episodeFileDeleted` vs `movieFileDeleted`).
3. The two enums **list members in a different order**, and the underlying C# enums have
   non-contiguous numeric values. The OpenAPI schema exposes only the name list, so the
   ordinal↔name mapping cannot be derived from it safely.

Consequences, both implemented:

* We **never filter history by integer `eventType`**. We fetch the entity's history and
  filter on the serialised **string** name. This sidesteps the ordinal ambiguity entirely.
* Both vocabularies are normalised into one canonical set in
  `core/ingest/arr.py::CANONICAL_EVENT` — `grabbed`, `imported`, `download_failed`,
  `file_deleted`, `file_renamed`, `download_ignored`, `unknown`. Rules only ever see
  canonical names, so a rule can't accidentally handle movies but not episodes.

### Queue — stall and import-failure evidence

`GET /api/v3/queue?includeUnknownMovieItems=true&includeMovie=false` (Radarr) /
`?includeUnknownSeriesItems=true&includeSeries=false` (Sonarr). Paginated
(`page`, `pageSize`); response is `{page,pageSize,sortKey,sortDirection,totalRecords,records[]}`.

`QueueResource`: `id, movieId|seriesId/episodeId, title, size, sizeleft, timeleft,
estimatedCompletionTime, added, status, trackedDownloadStatus, trackedDownloadState,
statusMessages, errorMessage, downloadId, protocol, downloadClient, indexer, outputPath`.

Enums (identical in both apps):

* `QueueStatus`: `unknown, queued, paused, downloading, completed, failed, warning, delay,
  downloadClientUnavailable, fallback`
* `TrackedDownloadStatus`: `ok, warning, error`
* `TrackedDownloadState`: `downloading, importBlocked, importPending, importing, imported,
  failedPending, failed, ignored`
* `DownloadProtocol`: `unknown, usenet, torrent`

`trackedDownloadState == importBlocked|failedPending|failed` together with `errorMessage` /
`statusMessages[].messages[]` is the evidence rule 4 quotes verbatim — that is where the
hardlink and permission errors actually surface.

### Entity resources — fields the rules depend on

`MovieResource`: `id, title, year, tmdbId, imdbId, titleSlug, monitored, hasFile,
movieFileId, movieFile, qualityProfileId, minimumAvailability, isAvailable, status,
inCinemas, physicalRelease, digitalRelease, releaseDate, lastSearchTime, path,
rootFolderPath, sizeOnDisk, added, statistics`.

`isAvailable`, `digitalRelease` and `minimumAvailability` are what let rule 2 distinguish
"no release exists yet" from "your indexers found nothing" — the single most common false
alarm. `lastSearchTime` shows whether a search was ever actually run.

`SeriesResource`: `id, title, year, tvdbId, tmdbId, imdbId, titleSlug, monitored,
monitorNewItems, qualityProfileId, seasons[], path, rootFolderPath, status, ended, added,
statistics`. Note there is **no `hasFile`** on a series — completeness comes from
`statistics.episodeFileCount` / `episodeCount`, and per-season `seasons[].statistics`.

`MovieFileResource` / `EpisodeFileResource`: `id, relativePath, path, size, dateAdded,
quality, qualityCutoffNotMet, mediaInfo, releaseGroup`.

### ⚠️ Finding 5 — `qualityCutoffNotMet` is already computed for us

Present on `HistoryResource`, `MovieFileResource` and `EpisodeFileResource`. Rule 6 ("wrong
quality landed") does **not** need to fetch quality profiles and compare cutoffs by hand —
the *arr already evaluated the file against its own profile, including custom-format score.
Reimplementing that comparison would be both redundant and wrong (it would ignore custom
formats). We read the flag and only fetch `GET /api/v3/qualityprofile/{id}` to *name* the
profile in the message.

Lookup for the fallback join (when `externalServiceId` is null):

* Radarr: `GET /api/v3/movie?tmdbId={id}` returns the matching library movie(s).
* Sonarr: `GET /api/v3/series?tvdbId={id}`.

Deep links into the owning UI: `/{app}/movie/{titleSlug}` (Radarr), `/{app}/series/{titleSlug}`
(Sonarr).

---

## 3. qBittorrent (WebUI API v2)

All methods `/api/v2/{group}/{method}`. `GET` for reads, `POST` for mutations (we only read,
plus the login POST).

### ⚠️ Finding 6 — two auth traps that silently break integrations

`POST /api/v2/auth/login` with form body `username`/`password`. Then:

1. **A failed login still returns HTTP 200.** The wiki documents only `403` (IP banned) and
   `200` ("all other scenarios"). Success is signalled by the body being `Ok.` and by a
   `Set-Cookie: SID=…`. Checking `response.status_code` alone reports bad credentials as a
   healthy connection. We check for the SID cookie and treat a missing one as auth failure.
2. **`Referer`/`Origin` must match the request's `Host`.** The wiki: *"Set `Referer` or
   `Origin` header to the exact same domain and port as used in the HTTP query `Host`
   header."* Without it qBittorrent's CSRF protection rejects the request. We set both from
   the configured base URL on every call.

The SID cookie is reused across polls and re-established on a `403`.

### GET /api/v2/torrents/info

Params: `filter, category, tag, sort, reverse, limit, offset, hashes`.
**`hashes` is a `|`-separated list** (not comma).

### ⚠️ Finding 7 — infohash case mismatch

*arr `downloadId` for torrents is the infohash in **uppercase** hex; qBittorrent's `hash`
field and its `hashes` filter are **lowercase**. Matching without normalising returns an
empty result and looks exactly like "torrent was removed from the client" — i.e. it would
manufacture a false rule-3/rule-4 diagnosis. We `.lower()` on both sides of the join.

Torrent fields used: `hash, name, state, progress, num_seeds, num_complete, num_leechs,
num_incomplete, dlspeed, upspeed, eta, amount_left, completed, size, total_size,
added_on, completion_on, last_activity, seen_complete, save_path, content_path, category,
tags, ratio, availability, tracker`.

`state` values: `error, missingFiles, uploading, pausedUP, queuedUP, stalledUP, checkingUP,
forcedUP, allocating, downloading, metaDL, pausedDL, queuedDL, stalledDL, checkingDL,
forcedDL, checkingResumeData, moving, unknown`.

Stall signals for rule 3: `state ∈ {stalledDL, metaDL}`, `num_seeds == 0`, `dlspeed == 0`,
and `last_activity` going stale. `error`/`missingFiles` are hard failures. Note
`num_complete` is `-1` when the tracker hasn't reported a swarm count — that is "unknown",
not "zero seeds", and must not trip the zero-seed branch.

`content_path` / `save_path` are the client-side paths, which are usually a *third* mount
namespace distinct from both the *arr's and Plex's. Relevant to the path-mapping table.

**[UNVERIFIED-LIVE]** Transmission and SABnzbd sit behind the same `DownloadClient`
interface but only qBittorrent is implemented in v1; see README.

---

## 4. Plex Media Server

Auth header `X-Plex-Token`. Send `Accept: application/json` or you get XML back.
Unauthenticated reachability probe: `GET /identity`. Authenticated probe: `GET /library/sections`.

Endpoints used:

* `GET /library/sections` → `MediaContainer.Directory[]` (`key`, `type`, `title`)
* `GET /library/sections/{key}/all` → `MediaContainer.Metadata[]`; `type=1` movies,
  `type=4` episodes
* `GET /library/metadata/{ratingKey}` → single item

Pagination is via the **headers** `X-Plex-Container-Start` / `X-Plex-Container-Size`
(they also work as query params). `MediaContainer.totalSize` is the full count when
paginating; `size` is the size of the current window.

File path nesting, confirmed against `python-plexapi`'s `MediaPart` (`TAG = 'Part'`,
attribute `file` = "The path to this file on disk"):

```
MediaContainer → Metadata[] → Media[] → Part[] → file
```

Capitalisation matters: `Media` and `Part` are capitalised in the JSON, `file` is not.

**[UNVERIFIED-LIVE]** `Part.file` is reliably present on movie and episode items in
`/library/sections/{key}/all` responses, but Plex omits `Media`/`Part` for items whose files
are unavailable, and some agents return items with an empty `Media` array. Our path index
skips such items rather than treating them as a path mismatch.

### Why path translation is a first-class concern

Radarr may see `/data/media/movies/...` while Plex sees `/movies/...` for the same file,
because the two containers mount the same host directory at different points. There is no
API that reveals this mapping — it is deployment configuration. So:

* We build a normalised path index of Plex parts, apply the configured `PathMapping` rewrites
  to the *arr-side path, and match on the result.
* When the ratingKey join says the item **is** in Plex but the path join fails, that is
  positive evidence of a bad mapping rather than a missing file — and it is reported as its
  own diagnosis (`PLEX_PATH_MISMATCH`) instead of the misleading "not in Plex".
* Basename matching is used as a corroborating signal only: if the *arr path's filename
  appears in Plex under a different directory, we report the specific prefix pair that would
  fix it.
