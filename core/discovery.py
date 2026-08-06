"""Work out someone's setup for them instead of making them type it in.

Almost everything Sleutharr needs is already written down inside the services it talks
to. A configured Seerr knows every Sonarr and Radarr you use, their URLs, their API keys,
and -- crucially -- the two things nobody can guess: which `serviceId` each one is, and
which handles the 4K lane. Each *arr in turn knows every download client, including the
name the *arr calls it by, which is the field the usenet join depends on.

So the whole chain is derivable from one starting point. The user pastes one URL and one
API key; everything downstream is proposed for them.

Nothing here writes to an upstream service, and nothing is saved without the user
choosing it. Discovery proposes; the user disposes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from core.clients.arr import arr_client
from core.clients.base import ServiceError
from core.clients.requestmanager import request_manager_client
from core.models import (
    ServiceInstance,
    ServiceKind,
    ServiceVariant,
)

logger = logging.getLogger(__name__)

#: Maps an *arr's download-client `implementation` string onto our variant.
IMPLEMENTATION_TO_VARIANT = {
    "sabnzbd": ServiceVariant.SABNZBD,
    "nzbget": ServiceVariant.NZBGET,
    "qbittorrent": ServiceVariant.QBITTORRENT,
    "transmission": ServiceVariant.TRANSMISSION,
    "deluge": ServiceVariant.DELUGE,
}


@dataclass
class Candidate:
    """A service we found and could add, or one that is already configured."""

    kind: str
    variant: str
    name: str
    base_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    remote_service_id: int | None = None
    is_4k: bool = False
    arr_client_name: str = ""
    #: Where we learnt about it, shown so the user can sanity-check the chain.
    found_via: str = ""
    #: Set when an enabled service with this URL already exists.
    existing_id: int | None = None
    #: Populated when we could find the service but not its credentials.
    needs_key: bool = False
    note: str = ""

    @property
    def is_new(self) -> bool:
        return self.existing_id is None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.variant_display})"

    @property
    def kind_display(self) -> str:
        return dict(ServiceKind.choices).get(self.kind, self.kind)

    @property
    def variant_display(self) -> str:
        return dict(ServiceVariant.choices).get(self.variant, self.variant)

    @property
    def key(self) -> str:
        """Stable identifier used as the checkbox value when applying."""
        return f"{self.kind}|{self.base_url}"


@dataclass
class DiscoveryResult:
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Library roots per *arr, used to propose path mappings later.
    root_folders: dict[str, list[str]] = field(default_factory=dict)

    @property
    def new_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.is_new]


def _normalise(url: str) -> str:
    """Compare URLs by host:port, ignoring scheme noise and trailing slashes."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    return f"{parsed.hostname or ''}:{parsed.port or ''}".lower().strip(":")


def _existing_by_url() -> dict[str, ServiceInstance]:
    return {
        _normalise(s.base_url): s for s in ServiceInstance.objects.all()
    }


def _build_url(hostname: str, port: int | None, use_ssl: bool, base_path: str = "") -> str:
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{hostname}"
    if port:
        url = f"{url}:{port}"
    base_path = (base_path or "").strip("/")
    if base_path:
        url = f"{url}/{base_path}"
    return url


def _field_value(fields: list, name: str, default=None):
    """Pull a value out of an *arr's name/value `fields` array."""
    for item in fields or []:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            return item.get("value", default)
    return default


def discover_from(service: ServiceInstance) -> DiscoveryResult:
    """Find everything reachable from one already-configured service."""
    result = DiscoveryResult()
    existing = _existing_by_url()

    if service.kind == ServiceKind.REQUEST_MANAGER:
        _from_request_manager(service, result, existing)
    elif service.kind in (ServiceKind.SONARR, ServiceKind.RADARR):
        _from_arr(service, result, existing)
    else:
        result.errors.append(
            f"{service.name} has nothing to discover from. Start from your request "
            f"manager or a Sonarr/Radarr instance."
        )
    return result


def discover_all() -> DiscoveryResult:
    """Run discovery from every service that can act as a starting point.

    Request managers first: they know the most, and their answers carry the serviceId
    and 4K flags that the *arr side cannot tell us.
    """
    combined = DiscoveryResult()
    seen: set[tuple[str, str]] = set()

    sources = list(
        ServiceInstance.objects.filter(
            enabled=True, kind=ServiceKind.REQUEST_MANAGER
        )
    ) + list(
        ServiceInstance.objects.filter(
            enabled=True, kind__in=[ServiceKind.SONARR, ServiceKind.RADARR]
        )
    )

    for source in sources:
        outcome = discover_from(source)
        combined.errors.extend(outcome.errors)
        combined.root_folders.update(outcome.root_folders)
        for candidate in outcome.candidates:
            key = (candidate.kind, _normalise(candidate.base_url))
            if key in seen:
                continue
            seen.add(key)
            combined.candidates.append(candidate)
    return combined


# ------------------------------------------------------------------ request manager


def _from_request_manager(
    service: ServiceInstance, result: DiscoveryResult, existing: dict
) -> None:
    client = request_manager_client(service)
    if service.variant == ServiceVariant.OMBI:
        result.errors.append(
            "Ombi does not expose its Sonarr/Radarr settings, so those have to be "
            "added by hand. Everything else can still be discovered from them."
        )
        return

    try:
        with client:
            for path, kind in (
                ("/settings/radarr", ServiceKind.RADARR),
                ("/settings/sonarr", ServiceKind.SONARR),
            ):
                try:
                    rows = client.get_json(path)
                except ServiceError as exc:
                    result.errors.append(f"{service.name}{path}: {exc}")
                    continue
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        _add_arr_candidate(row, kind, service, result, existing)
    except ServiceError as exc:
        result.errors.append(f"Could not reach {service.name}: {exc}")
    finally:
        client.close()


def _add_arr_candidate(
    row: dict,
    kind: str,
    source: ServiceInstance,
    result: DiscoveryResult,
    existing: dict,
) -> None:
    hostname = str(row.get("hostname") or "").strip()
    if not hostname:
        return
    url = _build_url(
        hostname,
        row.get("port"),
        bool(row.get("useSsl")),
        str(row.get("baseUrl") or ""),
    )
    match = existing.get(_normalise(url))

    result.candidates.append(
        Candidate(
            kind=kind,
            variant=ServiceVariant.NATIVE,
            name=str(row.get("name") or kind.title()),
            base_url=url,
            api_key=str(row.get("apiKey") or ""),
            # `id` here IS the serviceId the request manager uses to route requests.
            # It is the single most error-prone field to set by hand, and the reason
            # discovery is worth building at all.
            remote_service_id=row.get("id"),
            is_4k=bool(row.get("is4k")),
            found_via=f"{source.name} settings",
            existing_id=match.pk if match else None,
            note=(
                f"Root folder {row.get('activeDirectory')}"
                if row.get("activeDirectory")
                else ""
            ),
        )
    )


# ------------------------------------------------------------------------- *arr


def _from_arr(
    service: ServiceInstance, result: DiscoveryResult, existing: dict
) -> None:
    client = arr_client(service)
    try:
        with client:
            try:
                clients = client.get_json("/downloadclient")
            except ServiceError as exc:
                result.errors.append(f"{service.name} download clients: {exc}")
                clients = []

            if isinstance(clients, list):
                for row in clients:
                    if isinstance(row, dict):
                        _add_download_candidate(row, service, result, existing)

            try:
                roots = client.get_json("/rootfolder")
                if isinstance(roots, list):
                    paths = [
                        str(r["path"]) for r in roots if isinstance(r, dict) and r.get("path")
                    ]
                    if paths:
                        result.root_folders[service.name] = paths
            except ServiceError as exc:
                logger.debug("%s root folders: %s", service.name, exc)
    except ServiceError as exc:
        result.errors.append(f"Could not reach {service.name}: {exc}")
    finally:
        client.close()


def _add_download_candidate(
    row: dict,
    source: ServiceInstance,
    result: DiscoveryResult,
    existing: dict,
) -> None:
    implementation = str(row.get("implementation") or "").lower()
    variant = IMPLEMENTATION_TO_VARIANT.get(implementation)
    if variant is None:
        # An implementation we do not speak (rTorrent, Flood, a *arr-only pseudo client).
        return
    if not row.get("enable", True):
        return

    fields = row.get("fields") or []
    host = str(_field_value(fields, "host", "") or "").strip()
    if not host:
        return
    port = _field_value(fields, "port")
    use_ssl = bool(_field_value(fields, "useSsl", False))
    url_base = str(_field_value(fields, "urlBase", "") or "")
    url = _build_url(host, port, use_ssl, url_base)
    match = existing.get(_normalise(url))

    api_key = str(_field_value(fields, "apiKey", "") or "")
    username = str(_field_value(fields, "username", "") or "")
    # The *arr will not hand back stored passwords, so those still need typing.
    needs_key = not api_key and not username

    result.candidates.append(
        Candidate(
            kind=ServiceKind.DOWNLOAD_CLIENT,
            variant=variant,
            name=str(row.get("name") or implementation.title()),
            base_url=url,
            api_key=api_key,
            username=username,
            # The *arr's own name for this client. This is what queue rows are matched
            # on, and it is required to keep two usenet clients apart.
            arr_client_name=str(row.get("name") or ""),
            found_via=f"{source.name} download clients",
            existing_id=match.pk if match else None,
            needs_key=needs_key,
            note=(
                "Password not readable from the *arr — add it after saving."
                if variant in (ServiceVariant.QBITTORRENT, ServiceVariant.DELUGE,
                               ServiceVariant.TRANSMISSION)
                and not api_key
                else ""
            ),
        )
    )


# --------------------------------------------------------------------- persistence


def apply_candidate(candidate: Candidate) -> ServiceInstance:
    """Save one discovered service. Never overwrites an existing one's credentials."""
    if candidate.existing_id:
        service = ServiceInstance.objects.get(pk=candidate.existing_id)
        # Fill in only the routing fields, which are the ones users get wrong and which
        # discovery knows better than they do. Credentials and names are left alone.
        changed = []
        if candidate.remote_service_id is not None and (
            service.remote_service_id != candidate.remote_service_id
        ):
            service.remote_service_id = candidate.remote_service_id
            changed.append("remote_service_id")
        if candidate.is_4k and not service.is_4k:
            service.is_4k = True
            changed.append("is_4k")
        if candidate.arr_client_name and not service.arr_client_name:
            service.arr_client_name = candidate.arr_client_name
            changed.append("arr_client_name")
        if changed:
            service.save(update_fields=changed)
        return service

    return ServiceInstance.objects.create(
        kind=candidate.kind,
        variant=candidate.variant,
        name=candidate.name[:100],
        base_url=candidate.base_url,
        api_key=candidate.api_key,
        username=candidate.username,
        password=candidate.password,
        remote_service_id=candidate.remote_service_id,
        is_4k=candidate.is_4k,
        arr_client_name=candidate.arr_client_name[:120],
        enabled=True,
    )


# ------------------------------------------------------------------- identification


#: Probes tried in order against a pasted URL. Each returns (kind, variant, version)
#: when it recognises the service. Ordered cheapest and most distinctive first.
def identify(base_url: str, api_key: str = "", username: str = "", password: str = "") -> dict:
    """Work out what is running at a URL so the user does not have to say.

    Returns {ok, kind, variant, name, version, detail}. Every probe is a plain GET the
    service already answers; nothing is written and nothing is guessed from the port
    number, which people remap all the time.
    """
    import httpx

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "detail": "Enter a URL first."}
    if "://" not in base:
        base = f"http://{base}"

    attempts: list[tuple[str, str, str, dict]] = [
        # (kind, variant, path, headers)
        (ServiceKind.RADARR, ServiceVariant.NATIVE, "/api/v3/system/status",
         {"X-Api-Key": api_key}),
        (ServiceKind.REQUEST_MANAGER, ServiceVariant.SEERR, "/api/v1/status",
         {"X-Api-Key": api_key}),
        (ServiceKind.MEDIA_SERVER, ServiceVariant.PLEX, "/identity",
         {"Accept": "application/json"}),
        (ServiceKind.MEDIA_SERVER, ServiceVariant.JELLYFIN, "/System/Info/Public",
         {"Accept": "application/json"}),
        (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.SABNZBD,
         f"/api?mode=version&output=json", {}),
        (ServiceKind.DOWNLOAD_CLIENT, ServiceVariant.QBITTORRENT, "/api/v2/app/version", {}),
    ]

    with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True) as http:
        for kind, variant, path, headers in attempts:
            try:
                response = http.get(base + path, headers={k: v for k, v in headers.items() if v})
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            found = _interpret(kind, variant, response)
            if found:
                return found

    return {
        "ok": False,
        "detail": (
            "Nothing recognisable answered there. Check the address and port, and that "
            "the API key is right — some services only identify themselves once the key "
            "is valid."
        ),
    }


def _interpret(kind: str, variant: str, response) -> dict | None:
    """Turn a successful probe into an identification, or None if it was not conclusive."""
    text = (response.text or "").strip()

    if kind == ServiceKind.DOWNLOAD_CLIENT and variant == ServiceVariant.QBITTORRENT:
        # Answers a bare version string like "v5.0.3".
        if text.startswith("v") and len(text) < 20:
            return _ok(kind, variant, "qBittorrent", text.lstrip("v"))
        return None

    try:
        data = response.json()
    except ValueError:
        return None

    if kind == ServiceKind.RADARR:
        # Sonarr and Radarr share this endpoint; appName tells them apart.
        if not isinstance(data, dict) or "appName" not in data:
            return None
        app = str(data.get("appName") or "").lower()
        resolved = ServiceKind.SONARR if app == "sonarr" else ServiceKind.RADARR
        return _ok(
            resolved, ServiceVariant.NATIVE, app.title() or "Radarr",
            str(data.get("version") or ""),
        )

    if kind == ServiceKind.REQUEST_MANAGER:
        if not isinstance(data, dict) or "version" not in data:
            return None
        # All three answer here; the version string is the only reliable tell.
        version = str(data.get("version") or "")
        lowered = version.lower()
        if "jellyseerr" in lowered:
            resolved = ServiceVariant.JELLYSEERR
        elif data.get("restartRequired") is not None and version.startswith("1."):
            resolved = ServiceVariant.OVERSEERR
        else:
            resolved = ServiceVariant.SEERR
        return _ok(
            kind, resolved,
            dict(ServiceVariant.choices).get(resolved, "Seerr"), version,
        )

    if kind == ServiceKind.MEDIA_SERVER and variant == ServiceVariant.PLEX:
        container = (data or {}).get("MediaContainer") if isinstance(data, dict) else None
        if not isinstance(container, dict):
            return None
        return _ok(kind, variant, "Plex", str(container.get("version") or ""))

    if kind == ServiceKind.MEDIA_SERVER and variant == ServiceVariant.JELLYFIN:
        if not isinstance(data, dict) or "Version" not in data:
            return None
        product = str(data.get("ProductName") or "").lower()
        resolved = ServiceVariant.EMBY if "emby" in product else ServiceVariant.JELLYFIN
        return _ok(
            kind, resolved,
            str(data.get("ServerName") or data.get("ProductName") or "Jellyfin"),
            str(data.get("Version") or ""),
        )

    if kind == ServiceKind.DOWNLOAD_CLIENT and variant == ServiceVariant.SABNZBD:
        if not isinstance(data, dict) or "version" not in data:
            return None
        return _ok(kind, variant, "SABnzbd", str(data.get("version") or ""))

    return None


def _ok(kind: str, variant: str, name: str, version: str) -> dict:
    label = dict(ServiceVariant.choices).get(variant, variant)
    return {
        "ok": True,
        "kind": kind,
        "variant": variant,
        "name": name,
        "version": version,
        "detail": f"Found {label}{f' {version}' if version else ''}.",
    }
