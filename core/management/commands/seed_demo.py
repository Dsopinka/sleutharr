"""Populate the database with representative data.

Development and documentation only -- it fabricates services and requests so the UI can
be exercised and screenshotted without pointing at a real setup. It is never run
automatically, and `--wipe` only touches rows it created.
"""

from __future__ import annotations

import zlib
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    EventType,
    MediaAvailability,
    MediaType,
    PathMapping,
    RequestState,
    ServiceInstance,
    ServiceKind,
    ServiceVariant,
    TimelineEvent,
    TrackedRequest,
)
from core.rules.engine import diagnose_request

DEMO_PREFIX = "[demo] "


def _stable_id(title: str) -> int:
    """Deterministic across processes, unlike the builtin hash()."""
    return zlib.crc32(title.encode())


class Command(BaseCommand):
    help = "Seed representative demo data (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--wipe", action="store_true", help="Remove demo data first.")

    def handle(self, *args, **options):
        if options["wipe"]:
            count = ServiceInstance.objects.filter(
                name__startswith=DEMO_PREFIX
            ).delete()[0]
            self.stdout.write(f"Removed {count} demo rows.")

        now = timezone.now()

        seerr = self._service(
            ServiceKind.REQUEST_MANAGER, "Seerr", ServiceVariant.SEERR,
            "http://127.0.0.1:5055", version="3.4.1",
        )
        radarr = self._service(
            ServiceKind.RADARR, "Radarr", ServiceVariant.NATIVE,
            "http://127.0.0.1:7878", version="5.14.0",
        )
        radarr4k = self._service(
            ServiceKind.RADARR, "Radarr 4K", ServiceVariant.NATIVE,
            "http://127.0.0.1:7879", version="5.14.0",
            remote_service_id=1, is_4k=True,
        )
        sonarr = self._service(
            ServiceKind.SONARR, "Sonarr", ServiceVariant.NATIVE,
            "http://127.0.0.1:8989", version="4.0.10",
        )
        qbt = self._service(
            ServiceKind.DOWNLOAD_CLIENT, "qBittorrent", ServiceVariant.QBITTORRENT,
            "http://127.0.0.1:8081", version="5.0.3",
        )
        plex = self._service(
            ServiceKind.MEDIA_SERVER, "Plex", ServiceVariant.PLEX,
            "http://127.0.0.1:32400", version="1.41.3",
        )
        self._service(
            ServiceKind.MEDIA_SERVER, "Jellyfin", ServiceVariant.JELLYFIN,
            "http://127.0.0.1:8096", version="10.10.3",
        )
        self._service(
            ServiceKind.DOWNLOAD_CLIENT, "SABnzbd", ServiceVariant.SABNZBD,
            "http://127.0.0.1:8082", version="4.4.1",
            arr_client_name="SABnzbd",
        )
        self._service(
            ServiceKind.DOWNLOAD_CLIENT, "NZBGet", ServiceVariant.NZBGET,
            "http://127.0.0.1:6789", version="24.3",
            arr_client_name="NZBGet",
        )
        # One service deliberately broken, so the Health page shows both states.
        self._service(
            ServiceKind.SONARR, "Sonarr Anime", ServiceVariant.NATIVE,
            "http://127.0.0.1:8990", healthy=False,
        )

        PathMapping.objects.get_or_create(
            source_prefix="/data/media/movies",
            target_prefix="/movies",
            defaults={"note": "Radarr to Plex"},
        )

        # 1. never added -- the push to Radarr failed
        r = self._request(
            seerr, "Nosferatu", 2024, MediaType.MOVIE, "alice", 21,
            arr_service=radarr, entity_id=None,
            state=RequestState.FAILED, availability=MediaAvailability.PENDING,
        )
        self._event(r, EventType.REQUEST_FAILED, now - timedelta(days=21),
                    "Request manager reports the request FAILED",
                    "The push to Sonarr/Radarr did not succeed.",
                    ServiceKind.REQUEST_MANAGER, seerr)

        # 2. unmonitored
        r = self._request(
            seerr, "The Brutalist", 2024, MediaType.MOVIE, "bob", 30,
            arr_service=radarr, entity_id=613, monitored=False,
        )
        self._event(r, EventType.ADDED_TO_ARR, now - timedelta(days=30),
                    "Present in Radarr as #613", "Quality profile: HD-1080p",
                    ServiceKind.RADARR, radarr)

        # 3. stalled in the client, no seeds
        r = self._request(
            seerr, "Conclave", 2024, MediaType.MOVIE, "carol", 6,
            arr_service=radarr, entity_id=622,
        )
        self._event(r, EventType.GRABBED, now - timedelta(days=5, hours=6),
                    "Grabbed Conclave.2024.1080p.WEB-DL-GROUP",
                    "Indexer: Example Indexer", ServiceKind.RADARR, radarr)
        for hours, progress in ((30, 0.31), (12, 0.312), (0.2, 0.3122)):
            self._event(
                r, EventType.DOWNLOAD_PROGRESS, now - timedelta(hours=hours),
                f"Stalled at {progress * 100:.1f}%: Conclave.2024.1080p.WEB-DL-GROUP",
                "state=stalledDL · seeds: 0 connected / 0 in swarm · 0 KiB/s",
                ServiceKind.DOWNLOAD_CLIENT, qbt,
                raw={
                    "hash": "b7e1" + "0" * 36, "name": "Conclave.2024.1080p.WEB-DL-GROUP",
                    "state": "stalledDL", "progress": progress, "num_seeds": 0,
                    "num_complete": 0, "dlspeed": 0, "amount_left": 5_000_000_000,
                    "size": 7_300_000_000,
                },
            )

        # 4. downloaded but import blocked on a permissions error
        r = self._request(
            seerr, "Anora", 2024, MediaType.MOVIE, "alice", 4,
            arr_service=radarr, entity_id=631,
        )
        self._event(r, EventType.GRABBED, now - timedelta(days=4),
                    "Grabbed Anora.2024.1080p.WEB-DL-GROUP",
                    "Indexer: Example Indexer", ServiceKind.RADARR, radarr)
        self._event(
            r, EventType.IMPORT_BLOCKED, now - timedelta(days=3, hours=20),
            "Import blocked: Anora.2024.1080p.WEB-DL-GROUP",
            "Importing failed, path does not exist or is not accessible by Radarr: "
            "/downloads/complete/movies/Anora.2024.1080p.WEB-DL. Ensure the path exists "
            "and the user running Radarr has the correct permissions to access this "
            "file/folder",
            ServiceKind.RADARR, radarr,
            raw={
                "id": 6001,
                "movieId": 631,
                "title": "Anora.2024.1080p.WEB-DL-GROUP",
                "trackedDownloadState": "importBlocked",
                "downloadId": "C3D4" + "0" * 36,
                "downloadClient": "qBittorrent",
            },
        )

        # 5. imported, but the Plex path mapping does not resolve
        r = self._request(
            seerr, "Dune: Part Two", 2024, MediaType.MOVIE, "bob", 12,
            arr_service=radarr, entity_id=412, has_file=True,
        )
        self._event(r, EventType.GRABBED, now - timedelta(days=12),
                    "Grabbed Dune.Part.Two.2024.1080p.WEB-DL-GROUP",
                    "Indexer: Example Indexer", ServiceKind.RADARR, radarr)
        self._event(
            r, EventType.IMPORTED, now - timedelta(days=11, hours=20),
            "Imported Dune.Part.Two.2024.1080p.WEB-DL-GROUP [WEBDL-1080p]",
            "Imported to: /data/media/movies/Dune Part Two (2024)/"
            "Dune Part Two (2024) WEBDL-1080p.mkv",
            ServiceKind.RADARR, radarr,
            raw={"qualityCutoffNotMet": False,
                 "quality": {"quality": {"name": "WEBDL-1080p"}}},
        )
        self._event(
            r, EventType.MEDIA_SERVER_AVAILABLE, now - timedelta(hours=1),
            "In Plex as 'Dune: Part Two', but no configured path mapping resolves to it",
            "The *arr reports:\n  /data/media/movies/Dune Part Two (2024)/"
            "Dune Part Two (2024) WEBDL-1080p.mkv\n\nPlex reports:\n"
            "  /mnt/movies/Dune Part Two (2024)/Dune Part Two (2024) WEBDL-1080p.mkv\n\n"
            "Add a path mapping: /data/media/movies -> /mnt/movies",
            ServiceKind.MEDIA_SERVER, plex,
            dedupe_key="plex:demo:path_mismatch",
            raw={
                "ratingKey": "20481",
                "arrPaths": ["/data/media/movies/Dune Part Two (2024)/"
                             "Dune Part Two (2024) WEBDL-1080p.mkv"],
                "plexPaths": ["/mnt/movies/Dune Part Two (2024)/"
                              "Dune Part Two (2024) WEBDL-1080p.mkv"],
            },
        )

        # 6. wrong quality landed
        r = self._request(
            seerr, "The Substance", 2024, MediaType.MOVIE, "carol", 40,
            arr_service=radarr, entity_id=655, has_file=True, media_server_found=True,
        )
        self._event(
            r, EventType.IMPORTED, now - timedelta(days=39),
            "Imported The.Substance.2024.480p.WEBRip-GROUP [SDTV]", "",
            ServiceKind.RADARR, radarr,
            raw={"qualityCutoffNotMet": True, "quality": {"quality": {"name": "SDTV"}}},
        )

        # 7. blocklist loop
        r = self._request(
            seerr, "Severance", None, MediaType.TV, "dave", 9,
            arr_service=sonarr, entity_id=88, seasons=[2],
        )
        for i in range(3):
            self._event(r, EventType.GRABBED, now - timedelta(days=8 - i * 2),
                        "Grabbed Severance.S02E01.1080p.WEB-DL-GROUP", "",
                        ServiceKind.SONARR, sonarr,
                        raw={"sourceTitle": "Severance.S02E01.1080p.WEB-DL-GROUP"})
            self._event(r, EventType.DOWNLOAD_FAILED,
                        now - timedelta(days=8 - i * 2, hours=-4),
                        "Download failed: Severance.S02E01.1080p.WEB-DL-GROUP",
                        "Torrent failed: no data received", ServiceKind.SONARR, sonarr,
                        raw={"sourceTitle": "Severance.S02E01.1080p.WEB-DL-GROUP"})

        # 8. not released yet -- informational, not a fault
        r = self._request(
            seerr, "Avatar: Fire and Ash", 2026, MediaType.MOVIE, "alice", 15,
            arr_service=radarr, entity_id=701,
            snapshot={
                "isAvailable": False, "minimumAvailability": "released",
                "inCinemas": "2026-12-19T00:00:00Z",
                "digitalRelease": "2027-03-01T00:00:00Z",
            },
        )
        self._event(r, EventType.ADDED_TO_ARR, now - timedelta(days=15),
                    "Present in Radarr as #701", "Quality profile: HD-1080p",
                    ServiceKind.RADARR, radarr)

        # 9. no release found
        r = self._request(
            seerr, "A Real Pain", 2024, MediaType.MOVIE, "dave", 25,
            arr_service=radarr, entity_id=712,
            snapshot={"isAvailable": True, "lastSearchTime": "2026-07-28T10:00:00Z"},
        )
        self._event(r, EventType.ADDED_TO_ARR, now - timedelta(days=25),
                    "Present in Radarr as #712", "Quality profile: HD-1080p",
                    ServiceKind.RADARR, radarr)

        # 10. a 4K request that fulfilled -- proves the dashboard hides finished work
        self._request(
            seerr, "Alien: Romulus", 2024, MediaType.MOVIE, "bob", 60,
            arr_service=radarr4k, entity_id=77, is_4k=True, has_file=True,
            availability=MediaAvailability.AVAILABLE, media_server_found=True,
        )

        for tracked in TrackedRequest.objects.all():
            diagnose_request(tracked)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {TrackedRequest.objects.count()} requests across "
                f"{ServiceInstance.objects.count()} services."
            )
        )

    # -- helpers ---------------------------------------------------------------

    def _service(self, kind, name, variant, url, *, version="", healthy=True,
                 remote_service_id=None, is_4k=False, arr_client_name=""):
        now = timezone.now()
        service, _ = ServiceInstance.objects.update_or_create(
            name=DEMO_PREFIX + name,
            defaults={
                "kind": kind, "variant": variant, "base_url": url,
                "api_key": "demo", "version": version,
                "remote_service_id": remote_service_id, "is_4k": is_4k,
                "arr_client_name": arr_client_name,
                "last_seen_ok": now if healthy else None,
                "last_attempt_at": now,
                "consecutive_failures": 0 if healthy else 4,
                "last_error": "" if healthy else
                    "Connection failed: [Errno 111] Connection refused",
            },
        )
        return service

    def _request(self, service, title, year, media_type, user, days_ago, *,
                 arr_service=None, entity_id=None, monitored=True, has_file=False,
                 state=RequestState.APPROVED, availability=MediaAvailability.PROCESSING,
                 snapshot=None, seasons=None, is_4k=False, media_server_found=None):
        now = timezone.now()
        tracked, _ = TrackedRequest.objects.update_or_create(
            service=service,
            # crc32, not hash(): Python randomises string hashes per process, so hash()
            # would mint a new remote_id on every run and duplicate the whole dataset.
            remote_id=_stable_id(title) % 100000,
            defaults={
                "title": title, "year": year, "media_type": media_type,
                "requested_by": user,
                "requested_at": now - timedelta(days=days_ago),
                "request_state": state, "availability": availability,
                "arr_service": arr_service, "arr_entity_id": entity_id,
                "arr_monitored": monitored, "arr_has_file": has_file,
                "arr_quality_profile_name": "HD-1080p",
                "arr_snapshot": snapshot or {},
                "requested_seasons": seasons or [],
                "is_4k": is_4k, "media_server_found": media_server_found,
                "tmdb_id": _stable_id(title) % 900000,
                "last_polled": now,
            },
        )
        # Event dedupe keys are derived from timestamps that shift on every run, so a
        # reseed would otherwise stack duplicate timelines. Clearing first keeps the
        # command idempotent.
        tracked.events.all().delete()
        return tracked

    def _event(self, request, event_type, when, summary, detail, source_kind,
               service, raw=None, dedupe_key=""):
        TimelineEvent.objects.update_or_create(
            request=request,
            dedupe_key=dedupe_key or f"demo:{event_type}:{when.isoformat()}",
            defaults={
                "service": service, "source_kind": source_kind,
                "event_type": event_type, "occurred_at": when,
                "summary": summary, "detail": detail, "raw": raw or {},
            },
        )
