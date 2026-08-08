# Getting into Unraid Community Applications

CA is the only route to a template that appears on its own. Unraid offers templates it
already knows about — your own files in `templates-user/`, or what CA has indexed — and it
never reads anything out of a container image. So until this is listed, every user has to
copy the XML onto their server by hand.

## Where the template stands

Audited against the requirements at <https://ca.unraid.net/submit>. Everything the
submission portal asks for is present:

| Field | Status |
|---|---|
| `<Container version="2">` | ✅ |
| `<Name>` | ✅ `Sleutharr` |
| `<Repository>` | ✅ `ghcr.io/dsopinka/sleutharr:latest` |
| `<Registry>` | ✅ |
| `<Overview>` | ✅ |
| `<Support>` | ⚠️ GitHub issues — see below |
| `<Project>` | ✅ `https://sleutharr.com` |
| `<Category>` | ✅ `MediaApp:Video Tools:Utilities` |
| `<Description>` | ✅ |
| `<WebUI>` | ✅ |
| `<TemplateURL>` | ✅ raw GitHub URL, so template edits need no resubmission |
| `<Icon>` | ✅ 256×256 PNG, verified reachable |
| Config entries | ✅ port, `/config` path, `PUID`/`PGID`/`TZ`, CSRF origins |

Both referenced URLs return 200, and the image pulls anonymously from GHCR and Docker Hub.

## The two things left, neither of them code

**A forum support thread.** GitHub issues satisfies the `<Support>` field, but the
convention moderators look for is a thread in the Unraid forums' *Docker Containers*
section. Create it, then update `<Support>` to point at it.

**A maintenance commitment.** Publishing carries stated obligations: keep the app working
across Unraid releases, answer support requests in that thread, label beta versions
clearly, and tell the moderation team if you stop maintaining it. Apps that stop working
or go unsupported get removed.

That second one is the real decision. A listing puts this in front of people who did not
choose to try an early project, and each of them who hits a bug becomes a support request
addressed to you.

## How to submit, when you decide to

1. Create the forum thread; put its URL in `<Support>` in `unraid/sleutharr.xml`.
2. Go to <https://ca.unraid.net/submit>, point it at `https://github.com/Dsopinka/sleutharr`.
3. Run **Validate**, then **Scan**. Fix anything flagged.
4. Submit for moderation.

## Keep the registry in the template current

`<Repository>` points at GHCR, which CI rebuilds and republishes on every push to `main`.
Docker Hub currently only updates when someone pushes it by hand.

**Whichever registry the template names has to be the one that stays current**, or CA users
will sit on a stale image while the other registry moves ahead. Either leave it on GHCR, or
add the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets so CI publishes both
and the choice stops mattering.

## Readiness, honestly

**Ready:** packaging. Image, two registries, template, icon, CI, docs, install guide,
website, 250 tests.

**Thin:** real-world exposure. It has run against essentially one setup — one Seerr, one
Sonarr, one Radarr, one SABnzbd, one Plex, no 4K split, no second download client. Several
rules have never fired on live data. Every genuine bug found so far surfaced *because* that
one real setup existed, not from the test suite:

- a verdict claiming a file was missing from Plex when no media server was configured;
- healthy 2160p reported as a quality fault;
- a season pack counted as eight separate failures;
- a fully-delivered request that could never be marked done;
- a Plex rating key silently discarded for weeks after a field rename.

**Never pointed at a live instance:** Ombi, Jellyfin, Emby, NZBGet, Transmission, Deluge.

A reasonable sequence is to get two or three other people running it from the public image
first, let the rules fire on somebody else's data, and submit once a release has gone by
without a new class of bug appearing.
