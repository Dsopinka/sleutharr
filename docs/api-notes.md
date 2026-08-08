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

---

# Round 2: broader service support

Verified 2026-08-05, same standard as above — upstream source or official docs, never
memory.

| Service | Source of truth | Notes |
|---|---|---|
| Ombi | `Ombi.Store/Entities/Requests/*.cs` @ `develop` | v4 API |
| Jellyfin / Emby | Jellyfin API docs + auth gist | `/Items`, `X-Emby-Token` |
| SABnzbd | Official wiki `configuration/5.0/api` | API v2 |
| NZBGet | Sonarr's own `Nzbget.cs` client | JSON-RPC |
| Transmission | `docs/rpc-spec.md` @ `main` | RPC spec v18 |
| Deluge | Sonarr's own `Deluge.cs` client | JSON-RPC |

## 5. `downloadId` is not one thing — it is five

The whole download-client join rests on the *arr's `downloadId`, and it is easy to assume
it is always a torrent infohash. It is not. Read straight from Sonarr's own client
implementations (`src/NzbDrone.Core/Download/Clients/*`), which is the authority on what
value actually lands in the queue record:

| Client | Sonarr source line | `downloadId` is |
|---|---|---|
| qBittorrent | `DownloadId = torrent.Hash.ToUpper()` | infohash, **uppercased** |
| Deluge | `item.DownloadId = torrent.Hash.ToUpper()` | infohash, **uppercased** |
| Transmission | `DownloadId = torrent.HashString.ToUpper()` | infohash, **uppercased** |
| SABnzbd | `queueItem.DownloadId = sabQueueItem.Id` | `nzo_id` string, **case preserved** |
| NZBGet | `item.NzbId.ToString()` (or the `drone` parameter) | **a decimal integer** |

Three consequences, all implemented:

1. All three torrent clients uppercase the hash while the clients themselves report
   lowercase, so the existing `.lower()` normalisation on both sides is correct and now
   applies to Transmission and Deluge too.
2. SABnzbd's `nzo_id` is an opaque string (`SABnzbd_nzo_xxxxx`), not hex. It must not be
   treated as a hash or validated as one.
3. **NZBGet's id is a small integer like `42`, which is only unique within one NZBGet
   instance.** A global "ask every download client about every id" lookup will therefore
   happily match request A's NZBID `42` against a completely unrelated NZB in a *second*
   NZBGet instance, and report confident nonsense about a download that has nothing to do
   with the request.

### ⚠️ Finding 8 — the download join must be scoped per client instance

Because of (3) above, ids are only meaningful relative to the client that issued them.
`QueueResource.downloadClient` carries the client's **name as configured inside the
*arr**, and that is the only thing tying a queue row to a specific client. So:

* `ServiceInstance.arr_client_name` records what this client is called inside Sonarr/Radarr
  (defaulting to the Sleutharr service name when they match).
* The join asks each client only about ids that the *arr attributed to that client, and
  falls back to a global lookup **only** for torrent clients, whose infohashes are
  genuinely globally unique.

This is invisible with a single download client and produces wrong verdicts with two.

## 6. Ombi

Base path `/api/v1` (often behind a `/requests` sub-path — the configured base URL should
include it). Auth header is `ApiKey`, capitalised exactly that way.

* `GET /api/v1/Request/movie` → array of movie requests
* `GET /api/v1/Request/tv` → array of TV requests
* `GET /api/v1/Status` / `/api/v1/Settings/about` for a version probe

Responses are camelCased .NET entities. From `BaseRequest.cs` plus the concrete types:

```
BaseRequest:   title, approved, available, denied, deniedReason, requestedDate,
               markedAsApproved, markedAsAvailable, markedAsDenied,
               requestedUser {userName, userAlias, emailAddress}, requestedByAlias
MovieRequests: theMovieDbId, imdbId, releaseDate, subscribed, qualityOverride,
               rootPathOverride
TvRequests:    tvDbId, externalProviderId, imdbId, title, releaseDate, totalSeasons,
               childRequests[] -> seasonRequests[] -> episodes[]
```

### ⚠️ Finding 9 — Ombi has no service-linkage fields at all

Ombi stores **no** `externalServiceId`, no per-instance `serviceId`, and no `ratingKey`.
There is nothing in an Ombi request that says which Sonarr/Radarr instance received it or
which record it became. So on Ombi the *arr join is **always** the `tmdbId`/`tvdbId`
fallback path, and the media-server join is always path-based.

That is not a defect to work around, it is a capability difference, and it changes what
`NEVER_ADDED` can claim: on Seerr a null `externalServiceId` is strong evidence the push
failed, whereas on Ombi it means nothing because the field never exists. The rule is told
which it is dealing with (`RequestManagerClient.links_to_arr_entity`) and softens its
wording on Ombi rather than asserting something it cannot know.

Ombi also has no 4K lane: `is_4k` is always false, and requests route by media type alone.

## 7. Jellyfin and Emby

Both accept `X-Emby-Token: <key>`, so one client serves both (Jellyfin also accepts
`Authorization: MediaBrowser Token="…"`, but the simpler header is a documented fallback
and is what we send).

* Public probe, no auth: `GET /System/Info/Public`
* Authenticated probe: `GET /System/Info`
* Library scan: `GET /Items?Recursive=true&IncludeItemTypes=Movie,Episode&Fields=Path,ProviderIds&StartIndex=&Limit=`
* Response: `{ Items: [...], TotalRecordCount, StartIndex }`
* Item fields used: `Id`, `Name`, `Type`, `Path`, `ProviderIds { Tmdb, Tvdb, Imdb }`

Two useful differences from Plex:

* The file path is a **flat `Path` string** on the item, not nested under
  `Media[] → Part[] → file`. Episodes carry their own `Path`.
* `ProviderIds` gives a **direct tmdb/tvdb join**, which Plex does not offer without
  parsing its `guid`. So on Jellyfin/Emby the id join and the path join are independent,
  and the path-mismatch diagnosis is just as detectable as it is on Plex via ratingKey —
  arguably more reliably.

Jellyseerr/Seerr store `jellyfinMediaId` / `jellyfinMediaId4k`, giving a third join path
when the pairing is Jellyseerr + Jellyfin.

## 8. SABnzbd

`GET /api?mode=<mode>&output=json&apikey=<key>`. `mode=version` needs no key.

* `mode=queue` → `queue.slots[]`: `nzo_id`, `filename`, `status`, `percentage`, `mb`,
  `mbleft`, `timeleft`
* `mode=history` → `history.slots[]`: `nzo_id`, `name`, `status`, `fail_message`, `storage`

`fail_message` on a history slot is the usenet equivalent of a torrent error string, and is
quoted directly in the stall/failure diagnoses. Sizes are **megabytes as strings**, not
bytes — they need parsing to float before any arithmetic.

Deleting is `mode=queue&name=delete&value=<nzo_id>`, but Sleutharr never calls it: removal
goes through the *arr's queue endpoint so the *arr stays consistent with its own history.

## 9. NZBGet

JSON-RPC over `POST /jsonrpc`, HTTP basic auth with the control username/password.
**Only positional parameters are supported** — named params are rejected.

Methods used: `version`, `listgroups`, `history`.

### ⚠️ Finding 10 — 64-bit sizes arrive split into Hi/Lo halves

NZBGet returns every 64-bit integer as two 32-bit fields: `FileSizeLo`/`FileSizeHi`,
`RemainingSizeLo`/`RemainingSizeHi`. The value is `(Hi << 32) | Lo`.

Reading only the `Lo` half — the obvious mistake, since it is correct for anything under
4 GiB — silently reports wrong sizes for exactly the files this application cares about
(a 12 GB remux reports as ~3.7 GB). Worse, progress computed from a truncated total can
exceed 100% or go negative, which would trip the stall and import rules with nonsense.

Fields used from `listgroups`: `NzbID`, `NZBName`, `Category`, `FileSizeLo/Hi`,
`RemainingSizeLo/Hi`, `ActiveDownloads`, `Status`, `Health`, `DestDir`, `Parameters[]`.
From `history`: `NZBID`, `Name`, `Status`, `DestDir`, `FinalDir`.

`Health` is per-mille (1000 = 100%); a collapsing health value is the usenet analogue of
losing seeds. `Status` values seen include `QUEUED`, `PAUSED`, `DOWNLOADING`,
`FETCHING`, `PP_QUEUED`, `POST_PROCESSING`, and on history `SUCCESS/ALL`, `FAILURE/PAR`,
`FAILURE/UNPACK`, `DELETED/MANUAL`.

Note Sonarr prefers a `drone` post-processing parameter over `NzbID` when present, so the
`downloadId` may not equal the current `NzbID`. Both are checked when matching.

## 10. Transmission

`POST /transmission/rpc`, JSON body `{"method": ..., "arguments": {...}}`.

### ⚠️ Finding 11 — the mandatory 409 handshake

Transmission requires an `X-Transmission-Session-Id` header on every call. The first
request (and any request after the token expires) returns **HTTP 409** with the correct
token in the response headers; the client is expected to store it and retry. A client that
treats 409 as a normal error reports a perfectly healthy Transmission as unreachable
forever. Handled by catching 409, adopting the header and retrying once.

`torrent-get` with an explicit `fields` array. Fields used: `id`, `hashString`, `name`,
`status`, `percentDone`, `rateDownload`, `eta`, `peersSendingToUs`, `leftUntilDone`,
`totalSize`, `downloadDir`, `errorString`, `error`, `isFinished`, `activityDate`,
`doneDate`.

`status` is numeric, confirmed against `rpc-spec.md`:

```
0 stopped   1 check-wait   2 checking   3 download-wait
4 downloading   5 seed-wait   6 seeding
```

(Transmission 2.40 renumbered these; anything documenting 1–16 is describing the old
scheme and does not apply to any currently shipping version.)

Transmission reports no swarm-wide seed count, only `peersSendingToUs` — the connected
count. So the "zero seeds" branch treats Transmission like a tracker that withholds the
scrape: connected-zero **and** rate-zero, never "swarm has 0 seeds".

## 11. Deluge

JSON-RPC over `POST /json` with a cookie session. `auth.login` with the web password
first, then `web.update_ui` / `core.get_torrents_status`.

Like qBittorrent's login, **`auth.login` returns HTTP 200 with `"result": false` on a bad
password** rather than an error status, so the result body must be checked.

Fields used: `hash`, `name`, `state`, `progress` (0–100, *not* 0–1 like qBittorrent),
`num_seeds`, `total_seeds`, `download_payload_rate`, `eta`, `total_remaining`,
`total_size`, `save_path`, `message`.

Note the `progress` scale difference — treating Deluge's 0–100 as a 0–1 fraction makes
every torrent look 100× complete and permanently "finished", which would route every
stalled Deluge download into the wrong diagnosis.

## 12. Write operations (new in v2)

Sleutharr remains read-only by default. Exactly one class of write is implemented, always
behind an explicit confirmation, never automatically:

`DELETE /api/v3/queue/{id}` on Sonarr/Radarr, confirmed against both OpenAPI schemas:

| Param | Default | We send |
|---|---|---|
| `removeFromClient` | `true` | `true` — delete the data from the download client |
| `blocklist` | `false` | `true` — stop the *arr picking the same release again |
| `skipRedownload` | `false` | `false` — let the *arr search for a replacement itself |
| `changeCategory` | `false` | `false` |

Removal goes through the *arr rather than straight to the download client on purpose: the
*arr then updates its own queue and history, blocklists the release, and triggers the
replacement search. Deleting from the client directly would leave the *arr believing the
download is still in flight.

`skipRedownload=false` is why there is no separate "search again" button in the normal
flow — the *arr already does it. A standalone search command
(`POST /api/v3/command` with `MoviesSearch`/`SeriesSearch`) exists behind a setting that
is **off by default**, for the cases where nothing was ever grabbed.

---

## Finding 12 — usenet queue rows share no field names with torrent queue rows

Found from a live SABnzbd instance, not from documentation.

Every download client's queue endpoint returns a flat object describing one transfer, and
it is tempting to treat them as interchangeable. They are not — they overlap on almost
nothing:

| Meaning | qBittorrent | SABnzbd | NZBGet |
|---|---|---|---|
| identifier | `hash` | `nzo_id` | `NZBID` (Hi/Lo split, see Finding 10) |
| title | `name` | `filename` | `NZBName` |
| progress | `progress` (0.0–1.0 float) | `percentage` (0–100 **string**) | derived from `FileSizeLo`/`RemainingSizeLo` |
| state | `state` | `status` | `Status` |
| remaining | `amount_left` (bytes) | `mbleft` (megabytes, string) | `RemainingSizeMB` |
| swarm | `num_seeds`, `num_complete` | *(does not exist)* | *(does not exist)* |

Two consequences worth stating plainly:

**Reading a payload with the wrong product's field names fails silently.** `.get("progress")`
against a SABnzbd slot returns `None`, which becomes `0.0`, which reads as a download
that has never started. It does not raise. Nothing in a test suite built on qBittorrent
fixtures notices.

**Usenet has no swarm, so "zero seeds" is not a state it can be in.** A missing
`num_seeds` must mean "not applicable", never "no seeds left". Conflating the two turns
every in-progress usenet download into a dead torrent, and the remediation that follows
from "dead torrent" is to blocklist the release — which destroys a perfectly good release
and does not touch the actual cause.

The usenet analogue of a dead swarm is **article health**: the post has partly expired or
been taken down and cannot be reconstructed. That genuinely does warrant blocklisting.
Anything else — a paused client, a failed news-server login, an expired subscription, a
dropped VPN — stalls every download at once and blocklisting is exactly the wrong move,
because the replacement release will stall in the same place.

This is why `DownloadItem.facts()` exists and why rules read `TimelineEvent.facts` rather
than `TimelineEvent.raw`. Each product's parser is the only code permitted to know its own
field names. `core/tests/test_rules.py::UsenetIsNotATorrent` asserts no rule reaches into a
raw download payload, so the class of bug cannot come back by way of a new rule.

---

## Finding 13 — an absence is only evidence if something actually looked

Not an API quirk, but the failure mode most likely to produce a confidently wrong
verdict, so it belongs here.

Nearly every diagnosis Sleutharr makes is an assertion about something that is *not*
there: no entity in Sonarr, no grab in the history, no item in Plex. Each of those is
read as a fact about the user's setup. But a service that is unreachable returns exactly
the same empty result as a service that genuinely has nothing — and so does a service on
a fresh install that has not finished its first sync.

Left unguarded, a five-minute Sonarr outage relabels *every* tracked request as "never
reached your library", and a first run declares the same thing about a library that is
perfectly fine.

The guard is `RuleContext.can_speak_for(service)`, which is false when the service is
disabled, currently failing, has never once answered, or has not answered within
`EVIDENCE_STALE_AFTER`. Rules that assert an absence call it first and return
`EVIDENCE_UNAVAILABLE` instead of guessing — which is both true and useful, because an
unreachable service is itself something the user wants to know about.

This applies to the download-sample rules for a subtler reason: samples stop being
written when a client stops answering, so the newest reading keeps ageing and
"no progress for 20 hours" becomes a statement about Sleutharr's records rather than
about the download.

### Client-wide health, verified per product

A usenet client we can reach perfectly well may still have no working news server, which
stalls every download at once while each individual queue row looks blameless. Two
products expose this, and they expose different things:

| | SABnzbd | NZBGet |
|---|---|---|
| endpoint | `mode=status` | `status` (JSON-RPC) |
| server list | `status.servers[]` | `NewsServers[]` |
| name | `servername` | *(not reported — only `ID`)* |
| error text | `servererror`, `""` when fine | *(not reported; log only)* |
| enabled/active | `serveractive` | `Active` |
| optional server | `serveroptional` | *(no equivalent)* |
| paused | `paused` on the `mode=queue` object | `DownloadPaused` |
| warnings | `mode=warnings` → `[{text, type, time}]` | *(log only)* |

Two traps worth naming. SABnzbd's `serveroptional` servers are *meant* to be down
sometimes — flagging those cries wolf. And NZBGet's `Active` means **enabled in the
configuration**, not **currently connected**: reporting a disabled spare block account as
a connection failure would be inventing an error the product never claimed. So NZBGet is
only treated as broken when *every* server is disabled, and the wording says "disabled"
rather than "cannot connect".

Torrent clients return `None` from `provider_health()` — there is no single upstream
whose failure stops everything — and "not applicable" is stored distinctly from
"checked, and fine".

## Finding 14 — series monitoring is not season monitoring

`series.monitored` is a single flag on the whole show. A series can be monitored while
the exact season the user requested has `monitored: false`, in which case nothing will
ever be searched for it — and every downstream rule blames the indexers, sending the user
to audit a search that was never run.

Season shape verified against Sonarr's `SeasonResource`: `seasonNumber` (int) and
`monitored` (bool), inside `seasons[]` on the series object.

The rule stays silent unless it can be sure: an empty `requested_seasons` means a
whole-series request, a snapshot with no `seasons` list means the data was never
captured, and a season number the *arr did not mention is unknown rather than off. It
fires only when every requested season the *arr did report is switched off.
