# Installing on Unraid

Sleutharr is not published to Docker Hub or GHCR, so there is nothing to pull. You get the
image onto the server one of two ways, then add a container that uses it.

Everything below was verified against a real `linux/amd64` image built and run for this
purpose — Unraid is amd64, so that is the architecture that matters. See
[What was verified](#what-was-verified) for exactly how far that testing went.

---

## Step 1 — get the image onto the server

### Option A: build here, ship the image (recommended)

One command from the project directory on your Mac. No build tools needed on Unraid, and
it transfers about 58 MB.

```bash
docker buildx build --platform linux/amd64 -t sleutharr:latest --load .
```

Then push it straight into Unraid's Docker over SSH:

```bash
docker save sleutharr:latest | gzip | ssh root@TOWER 'gunzip | docker load'
```

Replace `TOWER` with your server's hostname or IP. SSH must be enabled (Settings →
Management Access). You will be prompted for the root password.

Confirm it landed:

```bash
ssh root@TOWER 'docker images sleutharr'
```

### Option B: build on the Unraid server

Useful if you would rather not build on a Mac at all, or want to rebuild after editing.
Copy the source over, then build in place:

```bash
rsync -av --exclude .venv --exclude config --exclude .git ./ root@TOWER:/mnt/user/appdata/sleutharr-src/
ssh root@TOWER 'cd /mnt/user/appdata/sleutharr-src && docker build -t sleutharr:latest .'
```

The build takes a few minutes and needs internet on the server to fetch the base image
and pip packages.

> Build it on the array or a cache share as shown, not in `/tmp` or `/root` — those live
> in RAM on Unraid and a build there can exhaust memory.

---

## Step 2 — add the container

### Using the template

Copy [`unraid/sleutharr.xml`](../unraid/sleutharr.xml) to the server's private template
directory:

```bash
scp unraid/sleutharr.xml root@TOWER:/boot/config/plugins/dockerMan/templates-user/my-sleutharr.xml
```

Then in the Unraid web UI: **Docker → Add Container → Template → my-sleutharr**. The
fields arrive pre-filled; review them and click **Apply**.

The template's repository is the local tag `sleutharr:latest`, which is why step 1 has to
happen first. If Unraid reports a pull failure but the image is present in
`docker images`, the container still creates correctly — Unraid uses the local image.

### Or add it by hand

**Docker → Add Container**, switch to *Advanced view*, and fill in:

| Field | Value |
|---|---|
| Name | `Sleutharr` |
| Repository | `sleutharr:latest` |
| Network Type | `Bridge` |
| WebUI | `http://[IP]:[PORT:8080]/` |

Add one **Port**: container `8080` → host `8080` (change the host side if 8080 is taken).

Add one **Path**: container `/config` → host `/mnt/user/appdata/sleutharr`, mode `Read/Write`.

Add three **Variables**:

| Key | Value |
|---|---|
| `PUID` | `99` |
| `PGID` | `100` |
| `TZ` | your timezone, e.g. `America/New_York` |

`99:100` is Unraid's `nobody:users`, which is what owns `/mnt/user` — the entrypoint
remaps the container's user to match so the database is not written as root.

---

## Step 3 — configure

Open `http://TOWER:8080`. Nothing is configured yet, by design: **every service URL and
API key lives in the web UI**, not in environment variables.

1. **Settings → Add a service.** Start with your request manager (Seerr, Overseerr,
   Jellyseerr or Ombi) — it is the source of every request, so nothing works without it.
2. Add each Sonarr and Radarr instance, your download client(s), and your media server.
3. **Health → Test** on each one before relying on it.

Use container-reachable addresses. If your other containers are on Unraid's `bridge`
network, use the server's LAN IP rather than `localhost` — `localhost` inside the
Sleutharr container means Sleutharr itself, not the host:

```
http://192.168.1.10:7878     ✅ works
http://localhost:7878        ❌ resolves to the Sleutharr container
```

If you run your *arr stack on a custom Docker network, put Sleutharr on the same network
and container names work directly (`http://radarr:7878`).

### Things worth setting up front

**Multiple Sonarr/Radarr instances (4K splits).** Set each instance's *request manager
service id* to its position in the request manager's Sonarr/Radarr settings list, starting
at 0, and tick *handles the 4K lane* on the 4K one. Getting this wrong does not error — it
silently attributes 4K requests to the wrong instance.

**More than one usenet client.** Set each one's *name inside Sonarr/Radarr*. SABnzbd and
NZBGet download ids are only unique within one instance (NZBGet uses plain integers like
`42`), so Sleutharr matches queue rows by the client name the *arr reports.

**Path mappings.** Only needed if your media server sees files at a different path than
Radarr/Sonarr do. You do not have to work it out yourself — when Sleutharr finds an item
by id but no path resolves to it, it reports `PATH_MISMATCH` and prints the exact prefix
pair to paste in.

---

## Step 4 — confirm it is seeing your setup

Once services are configured, this prints the field names each one actually returns:

```bash
docker exec Sleutharr python manage.py probe_services --verify
```

That is the fastest way to confirm `docs/api-notes.md` against your real instances,
including whether the 4K-suffixed keys are present on your request manager.

---

## Updating

Rebuild and reship, then recreate the container from the Unraid UI:

```bash
docker buildx build --platform linux/amd64 -t sleutharr:latest --load .
docker save sleutharr:latest | gzip | ssh root@TOWER 'gunzip | docker load'
```

Your data is in `/mnt/user/appdata/sleutharr` and is untouched by this. Migrations run
automatically on start.

---

## Troubleshooting

**Container is "unhealthy" for the first minute.** Expected on first start — it runs
migrations before serving. The healthcheck allows 90 seconds before it starts counting
failures.

**Everything on the Health page is red.** Almost always `localhost` in a service URL, or
an *arr that only listens on a specific interface. Use the LAN IP.

**Requests appear but never get a diagnosis.** Sleutharr needs at least one Sonarr or
Radarr configured to trace past the request manager. Check that the request manager
service id is set if you run several instances.

**Permission errors in the log.** Check `PUID`/`PGID` are `99`/`100` and that
`/mnt/user/appdata/sleutharr` is not owned by root:

```bash
ssh root@TOWER 'ls -ln /mnt/user/appdata/sleutharr && chown -R 99:100 /mnt/user/appdata/sleutharr'
```

**Logs:**

```bash
ssh root@TOWER 'docker logs --tail 100 Sleutharr'
```

---

## What was verified

The `linux/amd64` image was built and run for this guide. Confirmed on it:

- builds clean from the repo;
- starts as `uid=99 gid=100` with `TZ` applied, and writes `/config` with that ownership;
- runs migrations on first boot and reuses the database and secret key on restart;
- serves every page, the JSON API and static files;
- passes all 149 tests **inside the image**;
- reaches `healthy` with no errors in the log.

Not verified: this guide's SSH and Unraid-UI steps were not executed against a real Unraid
server — no such machine was reachable. They are standard Unraid procedure, but the exact
click path may differ slightly by Unraid version.
