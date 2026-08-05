"""Download client integration: qBittorrent, Transmission, Deluge, SABnzbd, NZBGet.

The join key is the *arr's `downloadId`, and the single most important thing to know is
that it is not one kind of value (see docs/api-notes.md #5). Read from Sonarr's own client
implementations:

    qBittorrent / Transmission / Deluge -> infohash, uppercased by the *arr
    SABnzbd                             -> nzo_id, an opaque per-instance string
    NZBGet                              -> a decimal integer, unique only within one host

So ids are compared case-insensitively (safe for all five), and the caller must scope
non-torrent lookups to the client that actually issued the id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.clients.base import AuthError, BaseClient, ProbeResult, ServiceError
from core.models import ServiceVariant

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DownloadItem:
    """Client-side truth about one transfer, torrent or usenet.

    Usenet has no swarm, so `num_seeds`/`num_complete` stay at the "unknown" sentinel and
    `health` carries the equivalent signal.
    """

    download_id: str
    name: str
    state: str
    #: 0.0 - 1.0. Normalised here; clients report wildly different scales.
    progress: float
    size: int = 0
    left: int = 0
    download_rate: int = 0
    eta: int = 0
    save_path: str = ""
    content_path: str = ""
    category: str = ""
    error_message: str = ""
    #: -1 means "not reported", which is different from zero. See has_no_seeds.
    num_seeds: int = -1
    num_complete: int = -1
    #: Usenet only: article availability, 0-100. -1 when not applicable.
    health: float = -1.0
    last_activity: int = 0
    is_usenet: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0 or self.left == 0 and self.size > 0

    @property
    def is_errored(self) -> bool:
        return bool(self.error_message) or self.state in {
            "error",
            "missingFiles",
            "failed",
        }

    @property
    def is_stalled(self) -> bool:
        return self.state in {"stalled", "metadata"} and not self.is_complete

    @property
    def is_paused(self) -> bool:
        return self.state in {"paused", "stopped"}

    @property
    def has_no_seeds(self) -> bool:
        """True only when the swarm is genuinely known to be empty.

        A -1 swarm count means the tracker withheld scrape data (or the client does not
        report it at all, as with Transmission). That is "unknown", not "zero" -- reading
        it as zero would flag every healthy private torrent as dead.
        """
        if self.is_usenet:
            return False
        if self.num_complete < 0:
            return self.num_seeds == 0 and self.download_rate == 0
        return self.num_complete == 0 and self.num_seeds == 0

    @property
    def unhealthy_articles(self) -> bool:
        """Usenet analogue of losing all seeds."""
        return self.is_usenet and 0 <= self.health < 90


class DownloadClient(BaseClient):
    """Interface every download client implements."""

    #: True when download ids are globally-unique infohashes.
    ids_globally_unique: bool = True
    is_usenet: bool = False
    product: str = "download client"

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        """Return {lowercased download id: item} for the ids that exist."""
        raise NotImplementedError

    def all_items(self) -> dict[str, DownloadItem]:
        raise NotImplementedError

    @staticmethod
    def _key(value: Any) -> str:
        return str(value or "").strip().lower()


# --------------------------------------------------------------------- qBittorrent


QBT_STATE_MAP = {
    "error": "error",
    "missingFiles": "missingFiles",
    "uploading": "seeding",
    "pausedUP": "paused",
    "stoppedUP": "paused",
    "queuedUP": "queued",
    "stalledUP": "seeding",
    "checkingUP": "checking",
    "forcedUP": "seeding",
    "allocating": "allocating",
    "downloading": "downloading",
    "metaDL": "metadata",
    "pausedDL": "paused",
    "stoppedDL": "paused",
    "queuedDL": "queued",
    "stalledDL": "stalled",
    "checkingDL": "checking",
    "forcedDL": "downloading",
    "checkingResumeData": "checking",
    "moving": "moving",
}


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
    product = "qBittorrent"

    def __init__(self, service):
        super().__init__(service)
        self._authenticated = False

    def default_headers(self) -> dict[str, str]:
        origin = self.origin()
        return {**super().default_headers(), "Referer": origin, "Origin": origin}

    def login(self) -> None:
        if self._authenticated:
            return
        if not self.service.username:
            # qBittorrent can bypass auth for local subnets.
            self._authenticated = True
            return
        response = self.request(
            "POST",
            "/auth/login",
            data={"username": self.service.username, "password": self.service.password},
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
            self._authenticated = False
            self.http.cookies.clear()
            self.login()
            return super().get_json(path, **kwargs)

    def probe(self) -> ProbeResult:
        self._ensure_auth()
        response = self.request("GET", "/app/version")
        version = (response.text or "").strip()
        if not version:
            return ProbeResult(ok=False, detail="Empty response from /app/version.")
        return ProbeResult(ok=True, detail=f"qBittorrent {version}", version=version)

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        wanted = sorted({self._key(h) for h in download_ids if h})
        if not wanted:
            return {}
        results: dict[str, DownloadItem] = {}
        # `hashes` is a |-separated list. Chunked to keep the URL a sane length.
        for start in range(0, len(wanted), 50):
            chunk = wanted[start : start + 50]
            data = self.get_json("/torrents/info", params={"hashes": "|".join(chunk)})
            if not isinstance(data, list):
                raise ServiceError("/torrents/info did not return a list.")
            for row in data:
                item = self._parse(row)
                if item.download_id:
                    results[item.download_id] = item
        return results

    def all_items(self) -> dict[str, DownloadItem]:
        data = self.get_json("/torrents/info")
        if not isinstance(data, list):
            raise ServiceError("/torrents/info did not return a list.")
        out = {}
        for row in data:
            item = self._parse(row)
            if item.download_id:
                out[item.download_id] = item
        return out

    @classmethod
    def _parse(cls, row: dict) -> DownloadItem:
        def num(key: str, default: float = 0) -> float:
            try:
                return float(row.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        raw_state = str(row.get("state") or "unknown")
        return DownloadItem(
            download_id=cls._key(row.get("hash")),
            name=str(row.get("name") or ""),
            state=QBT_STATE_MAP.get(raw_state, raw_state),
            progress=num("progress"),
            size=int(num("size")),
            left=int(num("amount_left")),
            download_rate=int(num("dlspeed")),
            eta=int(num("eta")),
            save_path=str(row.get("save_path") or ""),
            content_path=str(row.get("content_path") or ""),
            category=str(row.get("category") or ""),
            num_seeds=int(num("num_seeds")),
            num_complete=int(num("num_complete", -1)),
            last_activity=int(num("last_activity")),
            raw=row,
        )


# --------------------------------------------------------------------- Transmission


TRANSMISSION_STATUS = {
    0: "paused",
    1: "checking",
    2: "checking",
    3: "queued",
    4: "downloading",
    5: "queued",
    6: "seeding",
}

TRANSMISSION_FIELDS = [
    "id",
    "hashString",
    "name",
    "status",
    "percentDone",
    "rateDownload",
    "eta",
    "peersSendingToUs",
    "leftUntilDone",
    "totalSize",
    "downloadDir",
    "errorString",
    "error",
    "isFinished",
    "activityDate",
    "doneDate",
]


class TransmissionClient(DownloadClient):
    """Transmission RPC.

    The 409 handshake is mandatory: the first call (and any call after the token expires)
    returns HTTP 409 carrying the correct X-Transmission-Session-Id. Treating that as an
    error reports a perfectly healthy Transmission as permanently unreachable.
    """

    api_prefix = ""
    product = "Transmission"
    rpc_path = "/transmission/rpc"

    def __init__(self, service):
        super().__init__(service)
        self._session_id = ""

    def default_headers(self) -> dict[str, str]:
        headers = {**super().default_headers(), "Content-Type": "application/json"}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id
        return headers

    def rpc(self, method: str, arguments: dict | None = None) -> dict:
        payload = {"method": method, "arguments": arguments or {}}
        auth = None
        if self.service.username:
            auth = (self.service.username, self.service.password)

        for attempt in (1, 2):
            headers = {}
            if self._session_id:
                headers["X-Transmission-Session-Id"] = self._session_id
            try:
                response = self.request(
                    "POST", self.rpc_path, json=payload, headers=headers, auth=auth
                )
            except ServiceError as exc:
                # 409 is the session-id handshake, not a failure. Adopt the token the
                # server just handed us and retry exactly once.
                if exc.status == 409 and attempt == 1 and exc.response is not None:
                    self._session_id = exc.response.headers.get(
                        "X-Transmission-Session-Id", ""
                    )
                    if self._session_id:
                        continue
                raise
            data = self._decode(response)
            if not isinstance(data, dict):
                raise ServiceError("Transmission returned a non-object response.")
            if data.get("result") != "success":
                raise ServiceError(f"Transmission RPC error: {data.get('result')!r}")
            return data.get("arguments") or {}
        raise ServiceError("Transmission session handshake failed.")

    def probe(self) -> ProbeResult:
        data = self.rpc("session-get")
        version = str(data.get("version") or "")
        return ProbeResult(ok=True, detail=f"Transmission {version}".strip(), version=version)

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        wanted = sorted({self._key(h) for h in download_ids if h})
        if not wanted:
            return {}
        # Transmission accepts hashes directly in `ids`.
        data = self.rpc("torrent-get", {"ids": wanted, "fields": TRANSMISSION_FIELDS})
        return self._collect(data)

    def all_items(self) -> dict[str, DownloadItem]:
        return self._collect(self.rpc("torrent-get", {"fields": TRANSMISSION_FIELDS}))

    @classmethod
    def _collect(cls, data: dict) -> dict[str, DownloadItem]:
        out: dict[str, DownloadItem] = {}
        for row in data.get("torrents") or []:
            if isinstance(row, dict):
                item = cls._parse(row)
                if item.download_id:
                    out[item.download_id] = item
        return out

    @classmethod
    def _parse(cls, row: dict) -> DownloadItem:
        status = TRANSMISSION_STATUS.get(int(row.get("status") or 0), "unknown")
        peers = int(row.get("peersSendingToUs") or 0)
        rate = int(row.get("rateDownload") or 0)
        progress = float(row.get("percentDone") or 0)

        # Transmission has no "stalled" status; it is downloading with nobody to talk to.
        if status == "downloading" and peers == 0 and rate == 0 and progress < 1.0:
            status = "stalled"

        return DownloadItem(
            download_id=cls._key(row.get("hashString")),
            name=str(row.get("name") or ""),
            state=status,
            progress=progress,
            size=int(row.get("totalSize") or 0),
            left=int(row.get("leftUntilDone") or 0),
            download_rate=rate,
            eta=int(row.get("eta") or 0),
            save_path=str(row.get("downloadDir") or ""),
            error_message=str(row.get("errorString") or ""),
            num_seeds=peers,
            # Transmission reports only connected peers, never a swarm-wide count, so the
            # swarm figure stays "unknown" rather than being faked from peers.
            num_complete=-1,
            last_activity=int(row.get("activityDate") or 0),
            raw=row,
        )


# --------------------------------------------------------------------------- Deluge


DELUGE_STATE_MAP = {
    "Downloading": "downloading",
    "Seeding": "seeding",
    "Paused": "paused",
    "Checking": "checking",
    "Queued": "queued",
    "Error": "error",
    "Allocating": "allocating",
    "Moving": "moving",
}

DELUGE_FIELDS = [
    "hash",
    "name",
    "state",
    "progress",
    "num_seeds",
    "total_seeds",
    "download_payload_rate",
    "eta",
    "total_remaining",
    "total_size",
    "save_path",
    "message",
    "is_finished",
    "time_since_download",
]


class DelugeClient(DownloadClient):
    """Deluge JSON-RPC over the web UI.

    Like qBittorrent, `auth.login` returns HTTP 200 with `"result": false` on a bad
    password rather than an error status, so the body must be checked.
    """

    api_prefix = ""
    product = "Deluge"
    rpc_path = "/json"

    def __init__(self, service):
        super().__init__(service)
        self._authenticated = False
        self._request_id = 0

    def default_headers(self) -> dict[str, str]:
        return {**super().default_headers(), "Content-Type": "application/json"}

    def rpc(self, method: str, params: list | None = None) -> Any:
        self._request_id += 1
        payload = {"method": method, "params": params or [], "id": self._request_id}
        response = self.request("POST", self.rpc_path, json=payload)
        data = self._decode(response)
        if not isinstance(data, dict):
            raise ServiceError("Deluge returned a non-object response.")
        if data.get("error"):
            raise ServiceError(f"Deluge RPC error: {data['error']}")
        return data.get("result")

    def login(self) -> None:
        if self._authenticated:
            return
        result = self.rpc("auth.login", [self.service.password])
        if result is not True:
            raise AuthError(
                "Deluge rejected the password (it answers HTTP 200 with result=false "
                "on failure)."
            )
        self._authenticated = True

    def probe(self) -> ProbeResult:
        self.login()
        version = self.rpc("daemon.info") or ""
        return ProbeResult(ok=True, detail=f"Deluge {version}".strip(), version=str(version))

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        wanted = sorted({self._key(h) for h in download_ids if h})
        if not wanted:
            return {}
        self.login()
        result = self.rpc("core.get_torrents_status", [{"hash": wanted}, DELUGE_FIELDS])
        return self._collect(result)

    def all_items(self) -> dict[str, DownloadItem]:
        self.login()
        return self._collect(self.rpc("core.get_torrents_status", [{}, DELUGE_FIELDS]))

    @classmethod
    def _collect(cls, result: Any) -> dict[str, DownloadItem]:
        if not isinstance(result, dict):
            return {}
        out: dict[str, DownloadItem] = {}
        for torrent_hash, row in result.items():
            if not isinstance(row, dict):
                continue
            item = cls._parse(torrent_hash, row)
            if item.download_id:
                out[item.download_id] = item
        return out

    @classmethod
    def _parse(cls, torrent_hash: str, row: dict) -> DownloadItem:
        raw_state = str(row.get("state") or "unknown")
        state = DELUGE_STATE_MAP.get(raw_state, raw_state.lower())
        rate = int(row.get("download_payload_rate") or 0)
        seeds = int(row.get("num_seeds") or 0)

        # Deluge reports progress as 0-100, unlike qBittorrent's 0-1. Treating it as a
        # fraction would make every torrent look 100x complete and permanently finished.
        progress = float(row.get("progress") or 0) / 100.0

        if state == "downloading" and rate == 0 and seeds == 0 and progress < 1.0:
            state = "stalled"

        return DownloadItem(
            download_id=cls._key(row.get("hash") or torrent_hash),
            name=str(row.get("name") or ""),
            state=state,
            progress=progress,
            size=int(row.get("total_size") or 0),
            left=int(row.get("total_remaining") or 0),
            download_rate=rate,
            eta=int(row.get("eta") or 0),
            save_path=str(row.get("save_path") or ""),
            error_message=str(row.get("message") or "")
            if raw_state == "Error"
            else "",
            num_seeds=seeds,
            num_complete=int(row.get("total_seeds") or -1),
            raw=row,
        )


# -------------------------------------------------------------------------- SABnzbd


class SabnzbdClient(DownloadClient):
    """SABnzbd's query API.

    Ids are `nzo_id` strings, opaque and unique only within one instance, so lookups must
    be scoped to this client.
    """

    api_prefix = ""
    product = "SABnzbd"
    ids_globally_unique = False
    is_usenet = True

    def _params(self, mode: str, **extra: Any) -> dict[str, Any]:
        return {
            "mode": mode,
            "output": "json",
            "apikey": self.service.api_key,
            **extra,
        }

    def probe(self) -> ProbeResult:
        # mode=version needs no key, so it separates "unreachable" from "bad key".
        version_data = self.get_json("/api", params={"mode": "version", "output": "json"})
        version = str((version_data or {}).get("version") or "")
        queue = self.get_json("/api", params=self._params("queue", limit=1))
        if not isinstance(queue, dict) or "queue" not in queue:
            # SABnzbd answers 200 with {"status": false, "error": "API Key Incorrect"}.
            error = (queue or {}).get("error") if isinstance(queue, dict) else None
            raise AuthError(f"SABnzbd rejected the API key: {error or 'unknown error'}")
        return ProbeResult(ok=True, detail=f"SABnzbd {version}".strip(), version=version)

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        wanted = {self._key(i) for i in download_ids if i}
        if not wanted:
            return {}
        return {k: v for k, v in self.all_items().items() if k in wanted}

    def all_items(self) -> dict[str, DownloadItem]:
        out: dict[str, DownloadItem] = {}

        queue = self.get_json("/api", params=self._params("queue", limit=500)) or {}
        for slot in ((queue.get("queue") or {}).get("slots") or []):
            if isinstance(slot, dict):
                item = self._parse_queue_slot(slot)
                if item.download_id:
                    out[item.download_id] = item

        # History carries the failure reason, which is the usenet equivalent of a torrent
        # error string and the thing worth quoting in a diagnosis.
        history = self.get_json("/api", params=self._params("history", limit=200)) or {}
        for slot in ((history.get("history") or {}).get("slots") or []):
            if isinstance(slot, dict):
                item = self._parse_history_slot(slot)
                if item.download_id and item.download_id not in out:
                    out[item.download_id] = item
        return out

    @classmethod
    def _parse_queue_slot(cls, slot: dict) -> DownloadItem:
        # Sizes are megabytes as strings, not bytes.
        def mb(key: str) -> float:
            try:
                return float(slot.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        total_mb = mb("mb")
        left_mb = mb("mbleft")
        try:
            percentage = float(slot.get("percentage") or 0) / 100.0
        except (TypeError, ValueError):
            percentage = 0.0

        status = str(slot.get("status") or "").lower()
        state = {
            "downloading": "downloading",
            "queued": "queued",
            "paused": "paused",
            "checking": "checking",
            "fetching": "metadata",
        }.get(status, status or "unknown")

        return DownloadItem(
            download_id=cls._key(slot.get("nzo_id")),
            name=str(slot.get("filename") or ""),
            state=state,
            progress=percentage,
            size=int(total_mb * 1024 * 1024),
            left=int(left_mb * 1024 * 1024),
            category=str(slot.get("cat") or ""),
            is_usenet=True,
            raw=slot,
        )

    @classmethod
    def _parse_history_slot(cls, slot: dict) -> DownloadItem:
        status = str(slot.get("status") or "").lower()
        failed = status == "failed"
        return DownloadItem(
            download_id=cls._key(slot.get("nzo_id")),
            name=str(slot.get("name") or ""),
            state="failed" if failed else "completed",
            progress=0.0 if failed else 1.0,
            size=int(slot.get("bytes") or 0),
            left=0,
            save_path=str(slot.get("storage") or ""),
            error_message=str(slot.get("fail_message") or ""),
            category=str(slot.get("category") or ""),
            is_usenet=True,
            raw=slot,
        )


# --------------------------------------------------------------------------- NZBGet


class NzbgetClient(DownloadClient):
    """NZBGet JSON-RPC.

    Ids are small integers unique only within one instance, so lookups must be scoped to
    this client -- a global lookup would happily match an unrelated NZB on another host.
    """

    api_prefix = ""
    product = "NZBGet"
    ids_globally_unique = False
    is_usenet = True
    rpc_path = "/jsonrpc"

    def default_headers(self) -> dict[str, str]:
        return {**super().default_headers(), "Content-Type": "application/json"}

    def rpc(self, method: str, params: list | None = None) -> Any:
        # Only positional parameters are supported; named params are rejected.
        payload = {"method": method, "params": params or [], "id": 1}
        auth = None
        if self.service.username:
            auth = (self.service.username, self.service.password)
        response = self.request("POST", self.rpc_path, json=payload, auth=auth)
        data = self._decode(response)
        if not isinstance(data, dict):
            raise ServiceError("NZBGet returned a non-object response.")
        if data.get("error"):
            raise ServiceError(f"NZBGet RPC error: {data['error']}")
        return data.get("result")

    def probe(self) -> ProbeResult:
        version = self.rpc("version")
        return ProbeResult(
            ok=True, detail=f"NZBGet {version}".strip(), version=str(version or "")
        )

    def items_by_id(self, download_ids: list[str]) -> dict[str, DownloadItem]:
        wanted = {self._key(i) for i in download_ids if i}
        if not wanted:
            return {}
        return {k: v for k, v in self.all_items().items() if k in wanted}

    def all_items(self) -> dict[str, DownloadItem]:
        out: dict[str, DownloadItem] = {}
        for row in self.rpc("listgroups", [0]) or []:
            if isinstance(row, dict):
                item = self._parse_group(row)
                if item.download_id:
                    out[item.download_id] = item
        for row in self.rpc("history", [False]) or []:
            if isinstance(row, dict):
                item = self._parse_history(row)
                if item.download_id and item.download_id not in out:
                    out[item.download_id] = item
        return out

    @staticmethod
    def _int64(row: dict, base: str) -> int:
        """Reassemble a 64-bit value NZBGet split into Hi/Lo halves.

        Reading only the Lo half is correct under 4 GiB and silently wrong above it --
        a 12 GB remux would report as ~3.7 GB, and progress derived from a truncated
        total can exceed 100% and trip the stall and import rules with nonsense.
        """
        try:
            hi = int(row.get(f"{base}Hi") or 0)
            lo = int(row.get(f"{base}Lo") or 0)
        except (TypeError, ValueError):
            return 0
        return (hi << 32) | lo

    @classmethod
    def _download_ids(cls, row: dict) -> list[str]:
        """Every id the *arr might have recorded for this item.

        Sonarr prefers a `drone` post-processing parameter over NZBID when present, so
        both are candidates.
        """
        ids = [cls._key(row.get("NZBID") or row.get("NzbID"))]
        for param in row.get("Parameters") or []:
            if isinstance(param, dict) and str(param.get("Name", "")).lower() == "drone":
                ids.append(cls._key(param.get("Value")))
        return [i for i in ids if i]

    @classmethod
    def _parse_group(cls, row: dict) -> DownloadItem:
        size = cls._int64(row, "FileSize")
        remaining = cls._int64(row, "RemainingSize")
        progress = (size - remaining) / size if size else 0.0

        status = str(row.get("Status") or "").upper()
        state = {
            "QUEUED": "queued",
            "PAUSED": "paused",
            "DOWNLOADING": "downloading",
            "FETCHING": "metadata",
            "PP_QUEUED": "processing",
            "POST_PROCESSING": "processing",
        }.get(status, status.lower() or "unknown")

        # Health is per-mille (1000 = 100%); a collapsing value is the usenet analogue
        # of losing every seed.
        try:
            health = float(row.get("Health", -1)) / 10.0
        except (TypeError, ValueError):
            health = -1.0

        ids = cls._download_ids(row)
        return DownloadItem(
            download_id=ids[0] if ids else "",
            name=str(row.get("NZBName") or ""),
            state=state,
            progress=max(0.0, min(1.0, progress)),
            size=size,
            left=remaining,
            save_path=str(row.get("DestDir") or ""),
            category=str(row.get("Category") or ""),
            health=health,
            is_usenet=True,
            raw={**row, "_sleutharr_ids": ids},
        )

    @classmethod
    def _parse_history(cls, row: dict) -> DownloadItem:
        status = str(row.get("Status") or "").upper()
        failed = status.startswith("FAILURE") or status.startswith("DELETED")
        ids = cls._download_ids(row)
        return DownloadItem(
            download_id=ids[0] if ids else "",
            name=str(row.get("Name") or ""),
            state="failed" if failed else "completed",
            progress=0.0 if failed else 1.0,
            size=cls._int64(row, "FileSize"),
            left=0,
            save_path=str(row.get("FinalDir") or row.get("DestDir") or ""),
            error_message=status if failed else "",
            health=-1.0,
            is_usenet=True,
            raw={**row, "_sleutharr_ids": ids},
        )


DOWNLOAD_CLIENT_BY_VARIANT: dict[str, type[DownloadClient]] = {
    ServiceVariant.QBITTORRENT: QBittorrentClient,
    ServiceVariant.TRANSMISSION: TransmissionClient,
    ServiceVariant.DELUGE: DelugeClient,
    ServiceVariant.SABNZBD: SabnzbdClient,
    ServiceVariant.NZBGET: NzbgetClient,
}


def download_client(service) -> DownloadClient:
    cls = DOWNLOAD_CLIENT_BY_VARIANT.get(service.variant)
    if cls is None:
        logger.warning(
            "Download client variant %r is not implemented; trying qBittorrent.",
            service.variant,
        )
        cls = QBittorrentClient
    return cls(service)
