"""OAuth2 client-credentials tokens for the KONE APIs.

KONE tokens carry their scopes, and a scope is per resource: a call-giving
scope names one lift group (`callgiving/group:BUILDING:GROUP`), an equipment
status scope names one piece of equipment (`rtm/ken:123`). So the token this
client asks for is built from what has been configured, and it is asked for
again whenever that set changes or the token is close to expiring.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

import aiohttp

from .errors import KoneAuthError, KoneConnectionError, KoneRequestError

DEFAULT_HOSTNAME = "dev.kone.com"

SCOPE_INVENTORY = "application/inventory"

#: Refresh this long before KONE says the token expires, so a WebSocket
#: reconnect or a poll never presents a token that dies mid-request.
REFRESH_MARGIN = 120


def callgiving_scope(building_id: str, group_id: str) -> str:
    """Scope for calling and monitoring the lifts of one group."""
    return f"callgiving/group:{building_id}:{group_id}"


def equipment_status_scope(ken: str) -> str:
    """Scope for Equipment Status API 2.0 on one piece of equipment."""
    return f"rtm/{ken}"


def service_info_scope(ken: str) -> str:
    """Scope for Service Info API 2.0 on one piece of equipment."""
    return f"serviceinfo/{ken}"


class TokenManager:
    """Fetches and caches one access token for a fixed set of scopes."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        hostname: str = DEFAULT_HOSTNAME,
        scopes: Iterable[str] = (SCOPE_INVENTORY,),
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self.hostname = hostname
        self._scopes: tuple[str, ...] = tuple(sorted(set(scopes)))
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def http_base(self) -> str:
        """`https://host`; a hostname given with a scheme is used as is."""
        if "://" in self.hostname:
            return self.hostname.rstrip("/")
        return f"https://{self.hostname}"

    @property
    def ws_base(self) -> str:
        base = self.http_base
        return "ws" + base[4:] if base.startswith("http") else base

    @property
    def token_url(self) -> str:
        return f"{self.http_base}/api/v2/oauth2/token"

    @property
    def scopes(self) -> tuple[str, ...]:
        return self._scopes

    def set_scopes(self, scopes: Iterable[str]) -> None:
        """Change the scopes; the next `token()` fetches a fresh token."""
        new = tuple(sorted(set(scopes)))
        if new != self._scopes:
            self._scopes = new
            self._token = None

    @property
    def expires_at(self) -> float:
        """Monotonic time the current token expires, 0 if there is none."""
        return self._expires_at if self._token else 0.0

    async def token(self, force: bool = False) -> str:
        """A valid token, fetching a new one when needed."""
        async with self._lock:
            if (
                not force
                and self._token is not None
                and time.monotonic() < self._expires_at - REFRESH_MARGIN
            ):
                return self._token
            self._token, lifetime = await self._fetch()
            self._expires_at = time.monotonic() + lifetime
            return self._token

    async def _fetch(self) -> tuple[str, float]:
        try:
            async with self._session.post(
                self.token_url,
                auth=aiohttp.BasicAuth(self._client_id, self._client_secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": " ".join(self._scopes),
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                body = await _json_or_text(resp)
                if resp.status in (400, 401, 403):
                    raise KoneAuthError(
                        f"Token request refused ({resp.status}): {_message(body)}"
                    )
                if resp.status >= 300:
                    raise KoneRequestError(
                        f"Token request failed: {_message(body)}", resp.status
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise KoneConnectionError(f"Cannot reach {self.hostname}: {err}") from err

        if not isinstance(body, dict) or "access_token" not in body:
            raise KoneRequestError("Token response had no access_token")
        return body["access_token"], float(body.get("expires_in") or 3600)


async def _json_or_text(resp: aiohttp.ClientResponse):
    try:
        return await resp.json(content_type=None)
    except ValueError:
        return await resp.text()


def _message(body) -> str:
    if isinstance(body, dict):
        return str(
            body.get("message")
            or body.get("error_description")
            or body.get("error")
            or body
        )
    return str(body)[:200]
