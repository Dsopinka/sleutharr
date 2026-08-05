"""Verify connectivity and API assumptions against live instances.

`docs/api-notes.md` was written from upstream specs and sources, not from a running
server. This command is how you close that gap: it probes every configured service and,
with --verify, prints the shape of what actually came back so the notes can be confirmed
or corrected.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from core.clients import client_for
from core.models import ServiceInstance, ServiceKind


class Command(BaseCommand):
    help = "Probe every configured service and report reachability."

    def add_arguments(self, parser):
        parser.add_argument("--name", help="Only probe services whose name contains this.")
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Also fetch one sample record per service and print its field names, "
            "to confirm the assumptions in docs/api-notes.md.",
        )

    def handle(self, *args, **options):
        services = ServiceInstance.objects.all()
        if options.get("name"):
            services = services.filter(name__icontains=options["name"])
        if not services:
            self.stdout.write(self.style.WARNING("No services configured."))
            return

        for service in services:
            label = f"{service.name} [{service.kind}]"
            if not service.enabled:
                self.stdout.write(f"  -  {label}: disabled")
                continue
            try:
                with client_for(service) as client:
                    result = client.checked_probe()
                    if result.ok:
                        self.stdout.write(
                            self.style.SUCCESS(f"  OK {label}: {result.detail}")
                        )
                        if options.get("verify"):
                            self._verify(client, service)
                    else:
                        self.stdout.write(
                            self.style.ERROR(f"FAIL {label}: {result.detail}")
                        )
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"FAIL {label}: {exc}"))

    def _verify(self, client, service: ServiceInstance) -> None:
        """Print the real field names of one sample record."""
        try:
            sample, note = self._sample(client, service)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(f"       (sample fetch failed: {exc})")
            return
        if sample is None:
            self.stdout.write(f"       ({note})")
            return
        self.stdout.write(f"       {note}")
        if isinstance(sample, dict):
            for key in sorted(sample):
                value = sample[key]
                rendered = json.dumps(value, default=str)
                if len(rendered) > 70:
                    rendered = rendered[:67] + "..."
                self.stdout.write(f"         {key}: {rendered}")

    @staticmethod
    def _sample(client, service: ServiceInstance):
        if service.kind == ServiceKind.REQUEST_MANAGER:
            payload = client.get_json("/request", params={"take": 1, "skip": 0})
            results = (payload or {}).get("results") or []
            if not results:
                return None, "no requests to sample"
            media = results[0].get("media") or {}
            # The doubled 4K keys and their presence are the thing most worth confirming.
            present = [k for k in media if k.endswith("4k")]
            return media, (
                f"media object of request #{results[0].get('id')}; "
                f"4K-suffixed keys present: {present or 'NONE'}"
            )

        if service.kind in (ServiceKind.SONARR, ServiceKind.RADARR):
            entities = client.get_json(f"/{client.entity_path}") or []
            if not entities:
                return None, "library is empty"
            entity = entities[0]
            history = client.entity_history(int(entity["id"]))
            note = (
                f"{client.entity_path} #{entity['id']}; "
                f"{len(history)} history events; raw eventTypes: "
                f"{sorted({e.raw_event_type for e in history}) or 'none'}"
            )
            return entity, note

        if service.kind == ServiceKind.DOWNLOAD_CLIENT:
            torrents = client.all_torrents()
            if not torrents:
                return None, "no torrents in the client"
            first = next(iter(torrents.values()))
            return first.raw, f"{len(torrents)} torrents; sample {first.name[:40]!r}"

        if service.kind == ServiceKind.PLEX:
            sections = client.sections()
            note = "sections: " + ", ".join(
                f"{s.get('title')}({s.get('type')})" for s in sections
            )
            return (sections[0] if sections else None), note

        return None, "no sampler for this kind"
