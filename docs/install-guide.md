# Installing Sleutharr on Unraid

A step-by-step guide, no template needed. Takes about five minutes.

**What it does:** you request something in Seerr/Overseerr/Jellyseerr/Ombi, it never
turns up, and you have no idea which of five apps went wrong. Sleutharr follows that one
request across all of them and tells you where it broke and what to do about it.

It only reads from your services. The one thing it can change is a stuck download, and
only when you click a button and confirm.

---

## Before you start

You need:

- **Unraid** with Docker enabled (any recent version).
- **Your server's IP address.** You almost certainly know it — it is what you type to
  reach the Unraid web UI, something like `192.168.1.10`. Write it down; you will use it
  several times. If you are not sure, it is shown in Unraid under **Settings → Network
  Settings → IPv4 address**.
- **A request manager** (Seerr, Overseerr, Jellyseerr or Ombi) and at least one of Sonarr
  or Radarr. Without those there is nothing to trace.

---

## Step 1 — add the container

In the Unraid web UI go to the **Docker** tab and click **ADD CONTAINER** at the bottom.

Turn on **Advanced View** using the toggle at the top right. You need it for a couple of
the fields.

Fill in the top section exactly like this:

| Field | What to put |
|---|---|
| **Name** | `Sleutharr` |
| **Repository** | `ghcr.io/dsopinka/sleutharr:latest` |
| **Network Type** | `Bridge` |
| **WebUI** | `http://[IP]:[PORT:8080]/` |

Leave everything else on that section alone.

> The `[IP]` and `[PORT:8080]` in the WebUI box are not placeholders you replace — Unraid
> fills them in itself, so the WebUI button works even if you change the port later.
> Type them literally.

### Add the port

Click **Add another Path, Port, Variable, Label or Device**, then set:

| Field | Value |
|---|---|
| Config Type | `Port` |
| Name | `WebUI` |
| Container Port | `8080` |
| Host Port | `8080` |
| Connection Type | `TCP` |

**About those two ports.** They are different things and it matters:

- **Container Port** is the port *inside* the container. It is always `8080`. Do not
  change it.
- **Host Port** is the port *on your server* — the one you actually type into a browser.

So with the values above you would open `http://192.168.1.10:8080`.

**If port 8080 is already taken** (qBittorrent and SABnzbd both like it), just pick
another free one for the **Host Port** — `9191`, `8095`, anything unused. Leave the
Container Port at `8080`. If you set the Host Port to `9191` you would then open
`http://192.168.1.10:9191`. Unraid will warn you if you pick a port already in use.

### Add the storage

Click **Add another Path, Port, Variable...** again:

| Field | Value |
|---|---|
| Config Type | `Path` |
| Name | `Config` |
| Container Path | `/config` |
| Host Path | `/mnt/user/appdata/sleutharr` |
| Access Mode | `Read/Write` |

This is where the database lives, so your settings and history survive restarts and
updates. Sleutharr never touches your media, so **no media shares need mounting** — it
only ever compares folder *names*, never opens files.

### Add three variables

Same button, three times:

| Name | Key | Value |
|---|---|---|
| PUID | `PUID` | `99` |
| PGID | `PGID` | `100` |
| TZ | `TZ` | your timezone, e.g. `America/New_York` |

`99` and `100` are Unraid's standard `nobody`/`users` — they stop the database being
written as root. `TZ` just makes the timestamps read correctly; a list of valid values is
[here](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) (use the "TZ
identifier" column).

### Apply

Click **APPLY**. Unraid pulls the image (about 100 MB) and starts it.

**The container will say "unhealthy" for the first minute or so.** That is expected — it
is setting up its database before it starts answering. Give it a minute and it turns
green.

---

## Step 2 — open it

Click the Sleutharr icon in the Docker tab and choose **WebUI**, or go straight to:

```
http://YOUR-SERVER-IP:8080
```

You should get a mostly empty dashboard telling you to add a service.

---

## Step 3 — connect your services

Go to **Settings**.

### Start with your request manager

This is the important one — it is where requests come from, so nothing else works
without it. Open **+ Add a service** and fill in:

- **Address** — `192.168.1.10:5055` (your server's IP, and Seerr's port. Overseerr and
  Jellyseerr also use `5055`; Ombi uses `3579`.)
- **API key** — in Seerr/Overseerr/Jellyseerr this is under **Settings → General → API
  Key**. In Ombi it is **Settings → Configuration → General**.

Press **Check this address**. It should identify what is running there and fill in the
rest for you. Then **Add service**.

### ⚠️ The one thing that catches everyone

**Do not use `localhost` or `127.0.0.1` in any address.**

Inside Docker, `localhost` means *the Sleutharr container itself*, not your server. It
will simply never connect.

```
http://192.168.1.10:7878     ✅ works
http://localhost:7878        ❌ never works
```

Always use your server's real IP — the same one you type to reach Unraid.

### Now let it find the rest

Back on the Settings page, press **Find my other services**.

Sleutharr reads your setup out of the request manager you just added: every Sonarr and
Radarr you use, their addresses and API keys, and then their download clients. Tick what
you want and press **Add selected**.

This is worth doing even if you would rather add things by hand, because it fills in two
settings that are easy to get wrong and impossible to guess — which instance the request
manager routes to, and what your download clients are called inside Sonarr/Radarr.

### Add your media server

Under **Media server**:

- **Plex** — press **Sign in with Plex**, approve it in the window that opens, and pick
  your server from the list. There is no token to find. (This step needs internet access;
  everything else stays on your network.)
- **Jellyfin or Emby** — enter the address and your username and password. The password
  is swapped for an access key and then discarded.

### Check everything works

Go to the **Health** tab and press **Test** on each service. You want green across the
board. Anything red is almost always `localhost` in an address, or a wrong API key.

---

## Step 4 — wait a bit

Sleutharr checks every 60 seconds and reads back through your history on first run, so
give it a few minutes to populate. Then look at the **Dashboard**.

If it says **"Nothing needs attention"**, that is the correct and good answer — it means
nothing you have requested is stuck.

---

## What you are looking at

Each stuck request gets one plain-English verdict, the evidence behind it, and a link
straight to the app that owns the fix. Some examples:

| It says | It means |
|---|---|
| **Never reached your library** | The request never made it from Seerr into Sonarr/Radarr. |
| **Not being watched for** | It is in your library but unmonitored, so it will never download. |
| **Download is stuck** | It is in your download client with no seeds or no progress. |
| **Downloaded, but could not be filed away** | It finished downloading but the import failed — usually permissions or hardlinks. It quotes the actual error. |
| **Folder paths do not line up** | The file is in Plex but at a different path than Sonarr/Radarr think. One click fixes it. |
| **Nothing good enough found yet** | Your indexers have releases but your quality profile is rejecting them. Press **Why is nothing being found?** and it lists the exact reasons. |
| **Not out yet** | It is not released. Nothing is wrong. |

---

## If something goes wrong

**Container is unhealthy and stays that way.** Check the logs: Docker tab → click
Sleutharr → **Logs**.

**Everything on the Health page is red.** Almost always `localhost` in a service address.
Use your server's IP.

**One service is red.** Press **Test** on it for the actual error. A wrong API key says so
plainly.

**Requests appear but never get a verdict.** Sleutharr needs at least one Sonarr or Radarr
to see past the request manager. Check the Health page.

**Nothing appears at all.** Check the request manager is green on the Health page, then
press **Poll now** in the top right.

**Reset and start over.** Remove the container in Unraid, delete
`/mnt/user/appdata/sleutharr`, and add it again. Nothing outside that folder is touched.

---

## Updating

**Docker → Check for Updates → Apply.** Your settings and history are kept.

---

## This is early software

It works, and it is tested — but it has been run against a small number of real setups,
so it will meet combinations it has not seen. In particular Ombi, Jellyfin, Emby, NZBGet,
Transmission and Deluge are built to their documented APIs but have not yet been pointed
at a live instance.

If something looks wrong, **the verdict itself is the useful bug report**: open the
request, screenshot the timeline, and say what you expected instead. Most of the bugs
found so far were exactly that — a verdict that was confidently wrong about a real setup.

Issues: <https://github.com/Dsopinka/sleutharr/issues>

**What it will not do:** it does not touch your media files, it does not change settings
in Sonarr/Radarr/Plex, and it never deletes anything without you clicking a button and
confirming a dialog that spells out what will happen.
