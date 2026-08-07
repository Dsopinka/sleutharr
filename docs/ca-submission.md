# Publishing to Community Applications

Right now the template has to be copied onto the server by hand, because Unraid only
offers templates it already knows about: your own files in
`/boot/config/plugins/dockerMan/templates-user/`, and whatever Community Applications
has indexed. There is no way to make a template "come with" an image otherwise — the
image and the template are separate things, and Unraid never reads anything out of the
image itself.

So the only route to a template that appears on its own is a CA listing.

## What CA requires

Submissions go through the portal at **<https://ca.unraid.net/submit>**, which validates
and scans the XML before a moderator reviews it.

| Field | Status |
|---|---|
| `<Container version="2">` | ✅ |
| `<Name>` | ✅ |
| `<Repository>` | ✅ `ghcr.io/dsopinka/sleutharr:latest` |
| `<Registry>` | ✅ |
| `<Overview>` | ✅ |
| `<Support>` | ✅ GitHub issues |
| `<Project>` | ✅ |
| `<Category>` | ✅ `MediaApp:Video Tools:Utilities` |
| `<Description>` | ✅ |
| `<WebUI>` | ✅ |
| `<TemplateURL>` | ✅ raw GitHub URL |
| `<Icon>` | ✅ `unraid/icon.png`, 256×256 |

The template validates as well-formed XML and every required and recommended field is
populated. The image is public on GHCR and pullable without credentials, built for
`linux/amd64` by CI on every push.

## What is not done, and is not a code problem

Two of the remaining items are commitments rather than tasks:

**A support thread.** CA expects somewhere users can ask for help. GitHub issues satisfies
the `<Support>` field, but the convention — and what moderators look for — is a thread in
the Unraid forums' Docker Containers section. That has to be created by the author.

**Ongoing maintenance.** Publishing carries stated obligations: keep the app working
across Unraid releases, answer support requests, label beta versions clearly, and tell the
moderators if you stop maintaining it. The moderation team removes apps that stop working
or go unsupported.

That second one is the real decision. A listing puts this in front of people who did not
choose to try an early project, and every one of them who hits a bug becomes a support
request. It is worth being deliberate about, not just technically ready.

## An honest readiness assessment

**Ready:** the packaging. Image, registry, template, icon, CI, docs, 234 tests.

**Thin:** real-world exposure. It has run against exactly one setup — one Seerr, one
Sonarr, one Radarr, one SABnzbd, one Plex, no 4K split, no second download client, three
requests. Several rules have never fired on real data, and two of the three bugs found so
far only surfaced because that one real setup existed. A second and third setup will
almost certainly find more.

**Untested combinations:** Ombi, Jellyfin, Emby, NZBGet, Transmission and Deluge are
implemented against their documented APIs and covered by fixtures, but none has ever been
pointed at a live instance.

A reasonable order would be: run it against your own setup for a few weeks, let a couple
of other people try it from the GHCR image first, then submit once the rules have fired on
somebody else's data.

## If you do submit

1. Create the forum support thread and put its URL in `<Support>`.
2. Go to <https://ca.unraid.net/submit>, point it at
   `https://github.com/Dsopinka/sleutharr`, and run **Validate** then **Scan**.
3. Fix anything it flags, then submit for moderation.

Because `<TemplateURL>` points at the raw file on `main`, any later change to the template
is picked up without resubmitting.
