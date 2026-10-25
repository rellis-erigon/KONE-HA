"""REST parts of the KONE APIs.

- Application API: which buildings, groups and equipment this application
  has been granted.
- Equipment Status API 2.0: availability, status, movement, door and button
  events per piece of equipment (identified by `ken:NNN`).
- Service Info API 2.0: service orders per piece of equipment.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .auth import TokenManager, _json_or_text, _message
from .errors import KoneAuthError, KoneConnectionError, KoneRequestError
from .models import Resources, parse_resources


class KoneRestClient:
    """Thin async wrapper over KONE's REST endpoints."""

    def __init__(self, session: aiohttp.ClientSession, tokens: TokenManager) -> None:
        self._session = session
        self._tokens = tokens

    @property
    def _base(self) -> str:
        return f"{self._tokens.http_base}/api/v2"

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        token = await self._tokens.token()
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                json=json,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await _json_or_text(resp)
                if resp.status in (401, 403):
                    raise KoneAuthError(f"{path}: {resp.status} {_message(body)}")
                if resp.status >= 300:
                    raise KoneRequestError(f"{path}: {_message(body)}", resp.status)
                return body
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise KoneConnectionError(f"{path}: {err}") from err

    # Application API -------------------------------------------------------

    async def resources(self) -> Resources:
        """Buildings, groups and equipment granted to this application.

        Needs the `application/inventory` scope.
        """
        return parse_resources(await self._request("GET", "/application/self/resources"))

    # Equipment Status API 2.0 ---------------------------------------------

    async def _equipment_search(self, kind: str, kens: list[str]) -> list[dict]:
        body = await self._request(
            "POST", f"/equipment/search/{kind}", json={"equipmentIds": kens}
        )
        return _as_list(body)

    async def availability(self, kens: list[str]) -> list[dict]:
        return await self._equipment_search("availability", kens)

    async def status(self, kens: list[str]) -> list[dict]:
        return await self._equipment_search("status", kens)

    async def movement(self, kens: list[str]) -> list[dict]:
        return await self._equipment_search("movement", kens)

    async def door_events(self, kens: list[str]) -> list[dict]:
        body = await self._request(
            "POST", "/equipment/elevator/door", json={"equipmentIds": kens}
        )
        return _as_list(body)

    async def button_events(self, kens: list[str]) -> list[dict]:
        body = await self._request(
            "POST", "/equipment/elevator/button", json={"equipmentIds": kens}
        )
        return _as_list(body)

    # Service Info API 2.0 -------------------------------------------------

    async def service_orders(self, ken: str) -> list[dict]:
        return _as_list(await self._request("GET", f"/equipment/{ken}/serviceOrders"))

    async def service_order(self, ken: str, work_order: str) -> dict:
        body = await self._request(
            "GET", f"/equipment/{ken}/serviceOrders/{work_order}"
        )
        return body if isinstance(body, dict) else {}


def _as_list(body: Any) -> list[dict]:
    """KONE returns either a bare list or a list under a wrapper key."""
    if isinstance(body, list):
        return [item for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        for key in ("data", "results", "items", "serviceOrders"):
            if isinstance(body.get(key), list):
                return [item for item in body[key] if isinstance(item, dict)]
        return [body]
    return []
