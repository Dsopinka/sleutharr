"""Download client integration, behind one interface.

Only qBittorrent is implemented in v1; Transmission and SABnzbd slot in by subclassing
`DownloadClient` and returning `TorrentStatus` objects.

The *arr `downloadId` is the torrent infohash for torrent clients, which is what makes
this join possible at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.clients.base import AuthError, BaseClient, ProbeResult, ServiceError

logger = logging.getLogger(__name__)

# qBittorrent states that mean the transfer is not progressing under its own steam.
STALLED_STATES = {"stalledDL", "metaDL"}
ERROR_STATES = {"error", "missingFiles"}
PAUSED_STATES = {"pausedDL", "pausedUP", "stoppedDL", "stoppedUP"}
COMPLETE_STATES = {
    "uploading",
    "pausedUP",
    "stoppedUP",
    "queuedUP",
    "stalledUP",
    "forcedUP",
    "checkingUP",
}


@dataclass(slots=True)
class TorrentStatus:
    """Client-side truth about one transfer."""

    hash: str
    name: str
    state: str
    progress: float
    num_seeds: int
    num_complete: int
    dlspeed: int
    eta: int
    amount_left: int
    size: int
    added_on: int
    completion_on: int
    last_activity: int
    save_path: str
    content_path: str
    category: str
    ratio: float
    availability: float
    raw: dict = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.state in COMPLETE_STATES

    @property
    def is_errored(self) -> bool:
        return self.state in ERROR_STATES

    @property
    def is_stalled(self) -> bool:
        return self.state in STALLED_STATES and not self.is_complete

    @property
    def is_paused(self) -> bool:
        return self.state in PAUSED_STATES

    @property
    def has_no_seeds(self) -> bool:
        """True only when the swarm is genuinely known to be empty.

        `num_complete` is -1 when the tracker has not reported a seed count. That is
        "unknown", not "zero" -- treating it as zero would manufacture a stalled-no-seeds
        verdict for every torrent on a tracker that withholds scrape data.
        """
        if self.num_complete < 0:
            return self.num_seeds == 0 and self.dlspeed == 0
        return self.num_complete == 0 and self.num_seeds == 0


class DownloadClient(BaseClient):
    """Interface every download client implements."""

    def torrents_by_hash(self, hashes: list[str]) -> dict[str, TorrentStatus]:
        """Return {lowercase infohash: status} for the hashes that exist."""
        raise NotImplementedError


class QBittorrentClient(DownloadClient):
    """qBittorrent WebUI API v2.

    Two traps this class exists to avoid, both confirmed against the official wiki:

    1. A *failed* login still returns HTTP 200 -- the wiki documents only 403 (IP ban)
       and 200 ("all other scenarios"). Success is signalled by a `SID` cookie and an
       `Ok.` body. Checking status alone reports bad credentials as a healthy service.
    2. `Referer`/`Origin` must match the request Host or qBittorrent's CSRF protection
       rejects the call.
    """

    api_prefix = "/api/v2"

    def __init__(self, service):
        super().__init__(service)
        self._authenticated = False

    def default_headers(self) -> dict[str, str]:
        origin = self.origin()
        return {
            **super().default_headers(),
            "Referer": origin,
            "Origin": origin,
        }

    # -- auth ------------------------------------------------------------------

    def login(self) -> None:
        if self._authenticated:
            return
        if not self.service.username:
            # qBittorrent can be configured to bypass auth for local subnets.
            self._authenticated = True
            return
        response = self.request(
            "POST",
            "/auth/login",
            data={
                "username": self.service.username,
                "password": self.service.password,
            },
        )
        body = (response.text or "").strip()
        if "SID" not in self.http.cookies and body.lower() != "ok.":
            raise AuthError(
                "qBittorrent rejected the credentials (it answers HTTP 200 even on "
                f"failure; body was {body!r})."
            )
        self._authenticated = True

    def _ensure_auth(self) -> None:
        if not self._authenticated:
            self.login()

    def get_json(self, path: str, **kwargs: Any) -> Any:
        self._ensure_auth()
        try:
            return super().get_json(path, **kwargs)
        except AuthError:
            # Session expired -- re-login once, then give up.
            self._authenticated = False
            self.http.cookies.clear()
            self.login()
            return super().get_json(path, **kwargs)

    # -- health ----------------------------------------------------------------

    def probe(self) -> ProbeResult:
        self._ensure_auth()
        response = self.request("GET", "/app/version")
        version = (response.text or "").strip()
        if not version:
            return ProbeResult(ok=False, detail="Empty response from /app/version.")
        return ProbeResult(ok=True, detail=f"qBittorrent {version}", version=version)

    # -- torrents --------------------------------------------------------------

    def torrents_by_hash(self, hashes: list[str]) -> dict[str, TorrentStatus]:
        """Look up specific torrents.

        The *arr `downloadId` is uppercase hex; qBittorrent's `hash` field and its
        `hashes` filter are lowercase. Both sides are normalised here -- an unnormalised
        join returns nothing and looks exactly like "the torrent was removed", which
        would fabricate a diagnosis.
        """
        wanted = sorted({h.lower() for h in hashes if h})
        if not wanted:
            return {}

        results: dict[str, TorrentStatus] = {}
        # `hashes` is a |-separated list. Chunked to keep the URL a sane length.
        chunk_size = 50
        for start in range(0, len(wanted), chunk_size):
            chunk = wanted[start : start + chunk_size]
            data = self.get_json(
                "/torrents/info", params={"hashes": "|".join(chunk)}
            )
            if not isinstance(data, list):
                raise ServiceError("/torrents/info did not return a list.")
            for row in data:
                status = self._parse(row)
                if status.hash:
                    results[status.hash] = status
        return results

    def all_torrents(self) -> dict[str, TorrentStatus]:
        data = self.get_json("/torrents/info")
        if not isinstance(data, list):
            raise ServiceError("/torrents/info did not return a list.")
        out = {}
        for row in data:
            status = self._parse(row)
            if status.hash:
                out[status.hash] = status
        return out

    @staticmethod
    def _parse(row: dict) -> TorrentStatus:
        def num(key: str, default: float = 0) -> float:
            try:
                return float(row.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        return TorrentStatus(
            hash=str(row.get("hash") or "").lower(),
            name=str(row.get("name") or ""),
            state=str(row.get("state") or "unknown"),
            progress=num("progress"),
            num_seeds=int(num("num_seeds")),
            num_complete=int(num("num_complete", -1)),
            dlspeed=int(num("dlspeed")),
            eta=int(num("eta")),
            amount_left=int(num("amount_left")),
            size=int(num("size")),
            added_on=int(num("added_on")),
            completion_on=int(num("completion_on")),
            last_activity=int(num("last_activity")),
            save_path=str(row.get("save_path") or ""),
            content_path=str(row.get("content_path") or ""),
            category=str(row.get("category") or ""),
            ratio=num("ratio"),
            availability=num("availability"),
            raw=row,
        )


def download_client(service) -> DownloadClient:
    from core.models import ServiceVariant

    if service.variant == ServiceVariant.QBITTORRENT:
        return QBittorrentClient(service)
    # qBittorrent is the only implementation in v1; anything else configured as a
    # download client is treated as qBittorrent-compatible rather than silently ignored.
    logger.warning(
        "Download client variant %r is not implemented; trying the qBittorrent client.",
        service.variant,
    )
    return QBittorrentClient(service)
