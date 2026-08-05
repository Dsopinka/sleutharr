"""Shared HTTP plumbing for every upstream client.

Being a good citizen is a hard requirement: these services run on the same box as the
user's media library and several of them (Plex especially) are easy to hammer. So:

* one in-flight request per service instance, enforced by a per-instance lock;
* exponential backoff on 5xx and connection errors, persisted so a restart does not
  reset it;
* no retry on 4xx, because a wrong API key does not get better by asking again.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx
from django.utils import timezone

from core.models import ServiceInstance

logger = logging.getLogger(__name__)

# One lock per ServiceInstance id. Guards the "one in-flight request per service" rule
# across the scheduler's worker threads and any UI-triggered call.
_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()

MAX_BACKOFF_SECONDS = 15 * 60
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0


def _lock_for(service_id: int) -> threading.Lock:
    with _locks_guard:
        if service_id not in _locks:
            _locks[service_id] = threading.Lock()
        return _locks[service_id]


class ServiceError(Exception):
    """Any failure talking to an upstream service."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AuthError(ServiceError):
    """Credentials rejected. Never retried -- it needs a human."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message, status=status, retryable=False)


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    detail: str
    version: str = ""


class BaseClient:
    """Base for all service clients.

    Subclasses set `kind`, implement `probe()`, and use `self.get()` / `self.post()`.
    """

    #: Path appended to the configured base URL, e.g. "/api/v3".
    api_prefix: str = ""
    #: Number of attempts for a retryable failure within a single call.
    attempts: int = 3

    def __init__(self, service: ServiceInstance):
        self.service = service
        self._client: httpx.Client | None = None

    # -- lifecycle -------------------------------------------------------------

    def __enter__(self) -> "BaseClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def base_url(self) -> str:
        return self.service.url + self.api_prefix

    def default_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": "Sleutharr"}

    @property
    def http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers=self.default_headers(),
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
                verify=self.service.verify_tls,
                follow_redirects=True,
            )
        return self._client

    # -- requests --------------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Perform one upstream call with locking, retries and backoff bookkeeping."""
        lock = _lock_for(self.service.pk)
        delay = 1.0
        last_error: ServiceError | None = None

        with lock:
            for attempt in range(1, self.attempts + 1):
                try:
                    response = self.http.request(method, path, **kwargs)
                except httpx.TimeoutException as exc:
                    last_error = ServiceError(f"Timed out after {READ_TIMEOUT}s: {exc}")
                except httpx.HTTPError as exc:
                    last_error = ServiceError(f"Connection failed: {exc}")
                else:
                    if response.status_code in (401, 403):
                        raise AuthError(
                            self._auth_message(response), status=response.status_code
                        )
                    if response.status_code >= 500:
                        last_error = ServiceError(
                            f"HTTP {response.status_code} from {path}",
                            status=response.status_code,
                        )
                    elif response.status_code >= 400:
                        # 4xx other than auth: a bad request. Retrying cannot help.
                        raise ServiceError(
                            f"HTTP {response.status_code} from {path}: "
                            f"{response.text[:200]}",
                            status=response.status_code,
                            retryable=False,
                        )
                    else:
                        return response

                if attempt < self.attempts:
                    logger.debug(
                        "%s: attempt %d/%d failed (%s), sleeping %.1fs",
                        self.service.name,
                        attempt,
                        self.attempts,
                        last_error,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2

        assert last_error is not None
        raise last_error

    def _auth_message(self, response: httpx.Response) -> str:
        return (
            f"Authentication rejected (HTTP {response.status_code}). "
            "Check the API key."
        )

    def get_json(self, path: str, **kwargs: Any) -> Any:
        response = self.request("GET", path, **kwargs)
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> Any:
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ServiceError(
                f"Expected JSON but got {response.headers.get('content-type', '?')}: "
                f"{response.text[:120]}",
                retryable=False,
            ) from exc

    # -- health bookkeeping ----------------------------------------------------

    def probe(self) -> ProbeResult:  # pragma: no cover - overridden
        raise NotImplementedError

    def record_success(self, version: str = "") -> None:
        now = timezone.now()
        ServiceInstance.objects.filter(pk=self.service.pk).update(
            last_seen_ok=now,
            last_attempt_at=now,
            last_error="",
            consecutive_failures=0,
            backoff_until=None,
            **({"version": version} if version else {}),
        )

    def record_failure(self, error: Exception) -> None:
        """Persist the failure and widen the backoff window.

        Auth failures back off hard: they will not fix themselves, and hammering a
        service with bad credentials is how you get IP-banned by qBittorrent.
        """
        service = ServiceInstance.objects.filter(pk=self.service.pk).first()
        if service is None:
            return
        failures = service.consecutive_failures + 1
        if isinstance(error, ServiceError) and not error.retryable:
            backoff = MAX_BACKOFF_SECONDS
        else:
            backoff = min(2**failures, MAX_BACKOFF_SECONDS)
        now = timezone.now()
        ServiceInstance.objects.filter(pk=self.service.pk).update(
            last_attempt_at=now,
            last_error=str(error)[:2000],
            consecutive_failures=failures,
            backoff_until=now + timezone.timedelta(seconds=backoff),
        )
        logger.warning(
            "%s: %s (failure %d, backing off %ds)",
            service.name,
            error,
            failures,
            backoff,
        )

    def checked_probe(self) -> ProbeResult:
        """Probe and record the outcome. Never raises."""
        try:
            result = self.probe()
        except Exception as exc:  # noqa: BLE001 - health check must not propagate
            self.record_failure(exc)
            return ProbeResult(ok=False, detail=str(exc))
        if result.ok:
            self.record_success(result.version)
        else:
            self.record_failure(ServiceError(result.detail))
        return result

    # -- helpers ---------------------------------------------------------------

    def origin(self) -> str:
        """Scheme://host[:port] of the configured base URL."""
        parsed = urlparse(self.service.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def paginate(
        self, path: str, *, page_size: int = 100, **params: Any
    ) -> Iterator[dict]:  # pragma: no cover - overridden where used
        raise NotImplementedError
