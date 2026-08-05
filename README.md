# Sleutharr

**"I requested this — where did it die?"**

Sleutharr stitches a single media request into one timeline across
Seerr/Overseerr/Jellyseerr, Sonarr/Radarr, your download client and Plex, then tells you
where the chain broke and what to do about it.

It is **read-only**. Sleutharr never writes to any *arr app, download client or Plex.

![Dashboard](docs/screenshots/dashboard.png)

---

## Why this exists

The *arr stack is excellent at each individual job and gives you nothing that spans them.
When a request never arrives, the answer is somewhere in five different UIs, and the
symptom rarely matches the cause:

- Sonarr says "no results" — because the item is unmonitored.
- The download client shows 100% — because the import failed on a hardlink error.
- The *arr says "imported" — because Plex mounts that folder at a different path.

Sleutharr's output is a **single verdict per request**, with the evidence that supports it
and the specific next action. Anything that does not serve that verdict is out of scope.

### Related projects

- **[arr-dashboard](https://github.com/Kha-kis/arr-dashboard)** aggregates the same
  upstream data across many instances, presented per-service. Sleutharr deliberately does
  not duplicate that scope — it presents one causal timeline per *request* and a verdict.
- **[Toolbarr](https://github.com/Notifiarr/toolbarr)** repairs Starr databases.
  Complementary: Toolbarr fixes, Sleutharr diagnoses.

---

## The diagnoses

| Code | Severity | Means |
|---|---|---|
| `NEVER_ADDED` | error | Approved, but no Sonarr/Radarr entry exists. The hand-off failed. |
| `NO_ARR_INSTANCE` | warning | The request is not routed to any configured *arr instance. |
| `DECLINED` | info | Declined in the request manager. Nothing will happen. |
| `UNMONITORED` | warning | The entry exists but monitoring is off, so it will never be searched. |
| `BLOCKLIST_LOOP` | error | Repeated grab → fail cycles with no successful import. |
| `DOWNLOADED_NOT_IMPORTED` | error | The client finished; the import is blocked. Quotes the *arr's error. |
| `DOWNLOAD_CLIENT_ERROR` | error | The client reports `error` or `missingFiles`. |
| `GRABBED_BUT_STALLED` | warning | In the client, no meaningful progress, or zero seeds. |
| `PLEX_PATH_MISMATCH` | warning | Plex has the file, but no path mapping resolves to it. |
| `IMPORTED_NOT_IN_PLEX` | warning | Imported past the grace period, still absent from Plex. |
| `WRONG_QUALITY` | info | Imported below the profile cutoff but marked available. The silent one. |
| `NOT_RELEASED_YET` | info | Nothing grabbed because no release exists yet. Expected, not a fault. |
| `NEVER_SEARCHED` | warning | Monitored and available, but the *arr never ran a search. |
| `NO_RELEASE_FOUND` | warning | Searched, nothing passed the quality profile's filters. |

Rules are evaluated in priority order and **the first match wins**, so you get the root
cause rather than its symptoms. An unmonitored movie reports `UNMONITORED`, not
`NO_RELEASE_FOUND`, even though both technically apply.

![Request detail](docs/screenshots/request-detail.png)

Every diagnosis links to the exact record in the app that owns the fix, and highlights the
timeline events it was derived from.

---

## Install

### Docker Compose

```bash
docker compose up -d
```

Then open `http://localhost:8080` and add your services on the Settings page.

### Unraid

The template is at [`unraid/sleutharr.xml`](unraid/sleutharr.xml). Add it as a private
template, or point Community Applications at this repo.

Defaults follow Unraid conventions: `/config` for appdata, `PUID=99`, `PGID=100`, port
8080.

### From source

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
SLEUTHARR_CONFIG_DIR=./config .venv/bin/python manage.py migrate
SLEUTHARR_CONFIG_DIR=./config .venv/bin/python manage.py runserver 8080
```

### Environment variables

Only deployment concerns are env vars. **Every service URL and API key is configured in
the web UI** and stored in SQLite.

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `1000` | Ownership of files written to `/config`. |
| `TZ` | `UTC` | Affects displayed timestamps. |
| `SLEUTHARR_CONFIG_DIR` | `/config` | Database, secret key, logs. |
| `SLEUTHARR_CSRF_TRUSTED_ORIGINS` | — | Only needed behind an HTTPS reverse proxy. |
| `SLEUTHARR_SCHEDULER` | `1` | Set `0` to run the UI without the poller. |
| `SLEUTHARR_SECRET_KEY` | generated | Persisted to `/config/secret_key` if unset. |

---

## Configuration

### Services

Add one request manager, then each Sonarr/Radarr instance, your download client, and Plex.
Use **Test** on the Health page to verify each one before relying on it.

![Health](docs/screenshots/health.png)

### Multiple *arr instances (4K splits)

If you run separate 4K and 1080p instances, set each instance's **request manager service
id** to the `serviceId` that the request manager uses for it (its position in the request
manager's Sonarr/Radarr settings list, starting at 0), and tick **handles the 4K lane** on
the 4K one.

This matters more than it looks. Seerr stores every join key twice — `serviceId` /
`serviceId4k`, `externalServiceId` / `externalServiceId4k`, `ratingKey` / `ratingKey4k` —
and `is4k` on the request selects which half applies. A 4K request and a 1080p request for
the same title share one media record and routinely point at **different instances**.
Getting this wrong does not produce an error; it produces a confident, wrong diagnosis.

### Path translation

![Settings](docs/screenshots/settings.png)

Radarr may see `/data/media/movies` where Plex sees `/movies`, because the two containers
mount the same storage at different points. No API exposes this — it is deployment
configuration, so you have to tell Sleutharr about it.

You usually do not have to work it out yourself. When Sleutharr can see an item in Plex via
its rating key but no path resolves to it, it reports `PLEX_PATH_MISMATCH` and names the
exact prefix pair that would fix it. Paste that into the mapping table.

---

## How the join works

```
Request manager                 Sonarr / Radarr           Download client        Plex
──────────────────              ───────────────           ───────────────        ────
MediaRequest.is4k ──selects──►  externalServiceId[4k] ──► downloadId ──────────► ratingKey[4k]
                                (movieId / seriesId)      (= infohash)           + path match
   │                                   │                        │                    │
   └── tmdbId / tvdbId ────fallback────┘                        │                    │
                                                                └── path mappings ───┘
```

1. **Request manager → *arr.** `serviceId` picks the instance, `externalServiceId` the
   record — both read from the 4K-aware half of the pair. Falls back to `tmdbId`/`tvdbId`
   when null, which is exactly the case when the push to the *arr failed — and that is
   itself the `NEVER_ADDED` diagnosis.
2. ***arr → history.** Per-entity history is the spine of the timeline.
3. ***arr → download client.** The queue's `downloadId` is the torrent infohash.
4. ***arr → Plex.** Both the stored rating key and a path match, because running both is
   what distinguishes "not scanned yet" from "wrong path mapping".

Full API details, including several places where the published documentation is wrong, are
in [`docs/api-notes.md`](docs/api-notes.md).

---

## Polling

- Every service polls on its own interval (default 60s), paced independently.
- One in-flight request per service, ever.
- Exponential backoff on 5xx and connection failures; auth failures back off hard, because
  a bad API key does not fix itself and hammering qBittorrent with one gets you IP-banned.
- First run backfills to a configurable cutoff (default 90 days), then only reads forward.
- History pages already stored are never re-fetched.

Every event is stored with its **raw API payload**. Diagnosis rules change; re-deriving a
verdict from stored payloads beats re-polling history that may have aged out upstream. That
also means changing a threshold re-diagnoses your whole backlog on the next cycle without
touching any upstream service.

---

## Adding a diagnosis rule

One file, then one line. Rules do no I/O — everything they need is already in the timeline.

```python
# core/rules/r09_my_rule.py
from core.models import EventType, Severity
from core.rules.base import Rule, RuleContext, Verdict


class MyRule(Rule):
    code = "MY_RULE"
    severity = Severity.WARNING

    def evaluate(self, ctx: RuleContext) -> Verdict | None:
        if not ctx.has(EventType.GRABBED):
            return None
        return self.verdict(
            "What is wrong, in one sentence.",
            next_step="The specific thing to do about it.",
            link=ctx.arr_url(),
            evidence=ctx.grabs[-1:],
        )
```

Then add it to `RULES` in `core/rules/__init__.py`, in priority order.

---

## Development

```bash
SLEUTHARR_CONFIG_DIR=./config SLEUTHARR_SCHEDULER=0 .venv/bin/python manage.py test core
```

95 tests, no live calls — client parsing runs against recorded fixtures in
`core/tests/fixtures/`, and the ingestion tests use `httpx.MockTransport`. There are no
test-only dependencies.

To exercise the UI without a real setup:

```bash
SLEUTHARR_CONFIG_DIR=./config .venv/bin/python manage.py seed_demo
```

To check your assumptions against live instances once they are configured:

```bash
SLEUTHARR_CONFIG_DIR=./config .venv/bin/python manage.py probe_services --verify
```

`--verify` prints the actual field names each service returns, including whether the
4K-suffixed keys are present. `docs/api-notes.md` was written from upstream specs and
source, not from a running server; this command is how you close that gap.

### Stack

Python 3.12, Django 5.2, DRF, SQLite in WAL mode, APScheduler in-process, httpx, and
server-rendered templates with HTMX. One container, one process, no broker, no Postgres,
no JS build step. htmx is vendored, so the UI needs no outbound internet.

---

## Limitations

- **Only qBittorrent** is implemented as a download client. Transmission and SABnzbd fit
  behind the existing `DownloadClient` interface but are not written yet. Usenet downloads
  will therefore diagnose from *arr queue data alone, without client-side progress.
- **Plex only.** Jellyfin and Emby are supported by Seerr/Jellyseerr but not joined here.
- **Season-level granularity for TV is coarse.** A partially-grabbed season is diagnosed
  from the series' history as a whole, not per episode.
- **The Docker image has not been built in this environment** (no Docker daemon
  available), so the `Dockerfile` and `docker-entrypoint.sh` are unverified by execution.
  Everything else — migrations, the poll cycle, all 95 tests, and every page under
  gunicorn — was run.
- **No live upstream instance was available** during development. All API behaviour was
  verified against upstream OpenAPI schemas and source code rather than a running server;
  see the `[UNVERIFIED-LIVE]` markers in `docs/api-notes.md` for the specific items worth
  confirming with `probe_services --verify`.

## License

MIT
