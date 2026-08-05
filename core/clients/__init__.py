"""Client factory keyed on ServiceKind."""

from __future__ import annotations

from core.clients.base import AuthError, BaseClient, ProbeResult, ServiceError
from core.models import ServiceKind

__all__ = [
    "AuthError",
    "BaseClient",
    "ProbeResult",
    "ServiceError",
    "client_for",
]


def client_for(service) -> BaseClient:
    """Build the appropriate client for any configured service.

    Dispatch is on kind then variant: the kind decides the role in the chain, the variant
    decides the product. Rules only ever see kinds, so adding a product never reaches the
    diagnosis layer.
    """
    from core.clients.arr import arr_client
    from core.clients.download import download_client
    from core.clients.mediaserver import media_server_client
    from core.clients.requestmanager import request_manager_client

    if service.kind == ServiceKind.REQUEST_MANAGER:
        return request_manager_client(service)
    if service.kind in (ServiceKind.SONARR, ServiceKind.RADARR):
        return arr_client(service)
    if service.kind == ServiceKind.DOWNLOAD_CLIENT:
        return download_client(service)
    if service.kind == ServiceKind.MEDIA_SERVER:
        return media_server_client(service)
    raise ValueError(f"No client for service kind {service.kind!r}")
