# Installing on Unraid

The image is published to GitHub Container Registry, so Unraid installs it like any other
container — no terminal, no file transfer, no building.

```
ghcr.io/dsopinka/sleutharr:latest
```

Built for `linux/amd64` (Unraid's architecture) by CI on every push to `main`, and only
after the full test suite passes.

---

## Step 1 — add the container

### Using the template

Sleutharr is not in Community Applications yet, so the template does not appear on its
own. Copy [`unraid/sleutharr.xml`](../unraid/sleutharr.xml) into the server's private
template directory, via any SMB share or the file manager:

```
/boot/config/plugins/dockerMan/templates-user/my-sleutharr.xml
```

Then **Docker → Add Container → Template → my-sleutharr**. Fields arrive pre-filled;
review and click **Apply**.

See [Publishing to Community Applications](ca-submission.md) for what it would take for
the template to arrive automatically.

### Or add it by hand

**Docker → Add Container**, switch to *Advanced view*:

| Field | Value |
|---|---|
| Name | `Sleutharr` |
| Repository | `ghcr.io/dsopinka/sleutharr:latest` |
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

`99:100` is Unraid's `nobody:users`, which owns `/mnt/user` — the entrypoint remaps the
container's user to match so the database is not written as root.

Click **Apply**. Unraid pulls the image and starts it. First boot runs migrations and
takes a few seconds.

---

## Step 2 — configure

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

## Step 3 — confirm it is seeing your setup

Once services are configured, this prints the field names each one actually returns:

```bash
docker exec Sleutharr python manage.py probe_services --verify
```

Run it from Unraid's web terminal (the `>_` icon, top right) — no SSH needed.

That is the fastest way to confirm `docs/api-notes.md` against your real instances,
including whether the 4K-suffixed keys are present on your request manager.

---

## Updating

**Docker → Check for Updates → Apply**. That is the whole procedure.

Pushing to `main` rebuilds and republishes `:latest` automatically, so Unraid sees the
new image on its next check. Your data lives in `/mnt/user/appdata/sleutharr` and is
untouched; migrations run automatically on start.

To pin a version instead of tracking `latest`, tag a release (`git tag v2.0.0 && git push
--tags`) and point the Repository field at `ghcr.io/dsopinka/sleutharr:2.0.0`.

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

**Permission errors in the log.** Check `PUID`/`PGID` are `99`/`100` and that the appdata
folder is not owned by root. From the web terminal:

```bash
chown -R 99:100 /mnt/user/appdata/sleutharr
```

**Logs.** Click the container in the Docker tab → **Logs**, or from the web terminal:

```bash
docker logs --tail 100 Sleutharr
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

Not verified: the Unraid UI steps were not executed against a real Unraid server. They are
standard Unraid procedure, but the exact click path may differ slightly by version.
