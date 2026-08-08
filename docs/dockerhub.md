# Sleutharr

**"I requested this — where did it die?"**

Sleutharr follows a single media request across your request manager, Sonarr/Radarr, your
download client and your media server, then tells you where the chain broke and what to
do about it.

The *arr stack is very good at each individual job and gives you nothing that spans them.
When a request never arrives the answer is somewhere across five web UIs, and the symptom
rarely matches the cause:

- Sonarr says "no results" — because the item is not monitored.
- The download client shows 100% — because the import failed on a hardlink error.
- The *arr says "imported" — because Plex mounts that folder at a different path.

You get one plain-English verdict per request, the evidence behind it, and a link
straight to the app that owns the fix.

## Supported

| Role | Products |
|---|---|
| Request manager | Seerr, Overseerr, Jellyseerr, Ombi |
| Library | Sonarr, Radarr — any number, including 4K splits |
| Download client | qBittorrent, Transmission, Deluge, SABnzbd, NZBGet |
| Media server | Plex, Jellyfin, Emby |

## Quick start

```bash
docker run -d --name sleutharr \
  -p 8080:8080 \
  -e PUID=1000 -e PGID=1000 -e TZ=Etc/UTC \
  -v /path/to/config:/config \
  dsopinka123/sleutharr:latest
```

`latest` always points at a released version. To pin one, use it by number:

```bash
docker pull dsopinka123/sleutharr:2.11.1   # exact release
docker pull dsopinka123/sleutharr:2.11     # newest patch on that minor
```

The same image is published to `ghcr.io/dsopinka/sleutharr` with identical digests, so
either registry gives you the same bytes.

Then open `http://your-server:8080` and add your request manager on the Settings page.
Press **Find my other services** and it reads the rest of your setup out of it.

On Unraid use `PUID=99` and `PGID=100`.

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `1000` | Ownership of files written to `/config` |
| `TZ` | `UTC` | Affects displayed timestamps |
| `SLEUTHARR_CSRF_TRUSTED_ORIGINS` | — | Only needed behind an HTTPS reverse proxy |

Every service URL and API key is configured in the web UI, not through environment
variables.

## Read-only, almost entirely

Sleutharr reads from your services. It never changes settings in Sonarr, Radarr or Plex.

The exceptions are a small set of fixes that only ever run when you click a button and
confirm a dialog spelling out what will happen: removing a stuck download and
blocklisting the release, and retrying a request whose hand-off failed. There is no
automatic remediation, deliberately — diagnosis rules are heuristics, they will
occasionally misfire, and a misfiring rule with a delete key destroys real downloads.

## Documentation

Full docs, install guide and source: **https://github.com/Dsopinka/sleutharr**

## Support

Free and MIT licensed. If it saved you an evening, you can
[buy me a coffee](https://buymeacoffee.com/dsopinka).
