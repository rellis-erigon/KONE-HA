"""Elevator Call API v2 and Site Monitoring, over KONE's WebSocket.

One connection serves every building and group the token covers. Requests
are matched to their answers by request id; everything else that arrives
(monitoring events, call state) goes to the event callback.

The connection is kept up by `run()`: it reconnects with a growing delay,
reconnects ahead of token expiry, and re-sends every standing monitoring
subscription after each connect and before each one lapses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .auth import TokenManager
from .errors import (
    KoneAuthError,
    KoneConnectionError,
    KoneControlDisabled,
    KoneRequestError,
    KoneTimeout,
)
from .models import prefixed_building_id

_LOGGER = logging.getLogger(__name__)

SUBPROTOCOL = "koneapi"

#: KONE caps a monitoring subscription at 300 s; renew this long before.
MONITOR_DURATION = 300
MONITOR_RENEW_MARGIN = 30

#: Default monitoring topics for every lift in a group.
DEFAULT_SUBTOPICS = ("+/position", "+/doors", "+/status", "+/next_stop_eta")

#: Message types that change how lifts operate. A read-only client refuses
#: to send any of them, whatever the caller asks for.
CONTROL_MESSAGE_TYPES = frozenset({"lift-call-api-v2", "lift-call-api", "lift-call"})

#: Lift call `action` for a destination call (DCS). Other actions, such as
#: landing calls, are site specific; `actions()` lists the site's.
ACTION_DESTINATION = 2

EventCallback = Callable[[dict[str, Any]], None]
StateCallback = Callable[[bool], None]


@dataclass
class _Subscription:
    building_id: str
    group_id: str
    subtopics: tuple[str, ...]
    sub: str
    renew_at: float = 0.0


@dataclass
class _Pending:
    future: asyncio.Future
    expect_data: bool
    messages: list[dict] = field(default_factory=list)


class KoneWebSocket:
    """A self-healing connection to `wss://<host>/stream-v2`."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenManager,
        on_event: EventCallback | None = None,
        on_state: StateCallback | None = None,
        client_name: str = "homeassistant",
        read_only: bool = False,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._on_event = on_event
        self._on_state = on_state
        self._client_name = client_name
        #: When set, nothing that changes lift operation is ever sent.
        self.read_only = read_only
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, _Pending] = {}
        self._subs: dict[tuple[str, str], _Subscription] = {}
        self._connected = asyncio.Event()
        self._subs_changed = asyncio.Event()
        self._stopping = False
        #: Last messages seen, newest last, for diagnostics.
        self.recent: deque[dict] = deque(maxlen=50)

    @property
    def url(self) -> str:
        return f"{self._tokens.ws_base}/stream-v2"

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
        except asyncio.TimeoutError as err:
            raise KoneConnectionError("WebSocket did not connect") from err

    # Connection lifecycle ---------------------------------------------------

    async def run(self) -> None:
        """Keep the connection up until `stop()`; run this as a task."""
        delay = 1.0
        while not self._stopping:
            try:
                await self._connect_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except KoneAuthError as err:
                # Credentials or scopes are wrong; hammering will not fix it.
                _LOGGER.error("KONE WebSocket refused: %s", err)
                delay = 300.0
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                _LOGGER.warning("KONE WebSocket dropped: %s", err)
            finally:
                self._set_disconnected()
            if self._stopping:
                break
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 2, 300.0)

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def _connect_once(self) -> None:
        token = await self._tokens.token()
        try:
            self._ws = await self._session.ws_connect(
                f"{self.url}?accessToken={token}",
                protocols=(SUBPROTOCOL,),
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                # Probably a stale token. Fetching a fresh one raises
                # KoneAuthError if the credentials themselves are bad;
                # otherwise the next attempt uses it.
                await self._tokens.token(force=True)
            raise KoneConnectionError(f"WebSocket handshake {err.status}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise KoneConnectionError(str(err)) from err

        _LOGGER.debug("KONE WebSocket connected")
        self._connected.set()
        if self._on_state:
            self._on_state(True)

        for sub in self._subs.values():
            sub.renew_at = 0.0
        maintainer = asyncio.create_task(
            self._maintain(self._ws, self._tokens.expires_at)
        )
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_text(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        finally:
            maintainer.cancel()
        _LOGGER.debug(
            "KONE WebSocket closed (%s)", self._ws.close_code if self._ws else None
        )

    async def _maintain(
        self, ws: aiohttp.ClientWebSocketResponse, token_expires: float
    ) -> None:
        """Renew subscriptions, and reconnect before the token expires.

        `token_expires` is the expiry of the token this connection was
        opened with; REST polling may have refreshed the shared token since.
        """
        try:
            while not ws.closed:
                now = time.monotonic()
                if token_expires and now > token_expires - 60:
                    _LOGGER.debug("KONE token near expiry; reconnecting")
                    await ws.close()
                    return
                for sub in list(self._subs.values()):
                    if now >= sub.renew_at:
                        try:
                            await self._send_subscription(sub)
                            sub.renew_at = now + MONITOR_DURATION - MONITOR_RENEW_MARGIN
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.warning(
                                "Monitoring %s/%s not renewed: %s",
                                sub.building_id,
                                sub.group_id,
                                err,
                            )
                            sub.renew_at = now + 30
                self._subs_changed.clear()
                try:
                    await asyncio.wait_for(self._subs_changed.wait(), 5)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _set_disconnected(self) -> None:
        was = self._connected.is_set()
        self._connected.clear()
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.set_exception(KoneConnectionError("WebSocket closed"))
        self._pending.clear()
        if was and self._on_state:
            self._on_state(False)

    # Messages --------------------------------------------------------------

    def _handle_text(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        self.recent.append(msg)

        pending_key = _request_key(msg)
        pending = self._pending.get(pending_key) if pending_key else None
        if pending is not None and not pending.future.done():
            if _is_error(msg):
                pending.future.set_exception(
                    KoneRequestError(
                        str(msg.get("message") or msg.get("data") or msg),
                        _int(msg.get("statusCode")),
                    )
                )
                return
            pending.messages.append(msg)
            # A bare acknowledgement is not the answer when data is expected.
            if not pending.expect_data or _has_data(msg):
                pending.future.set_result(msg)
                return
            # Some answers carry data *and* look like events (e.g. a call's
            # session id); let them through to the event callback as well.
        elif msg.get("type") in ("ok", "error") and not _has_data(msg):
            # An acknowledgement for something no longer waited on.
            if _is_error(msg):
                _LOGGER.debug("KONE error for unknown request: %s", msg)
            return

        if self._on_event:
            try:
                self._on_event(msg)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("KONE event handler failed")

    async def request(
        self,
        payload: dict[str, Any],
        key: str,
        expect_data: bool = False,
        timeout: float = 15,
    ) -> dict[str, Any]:
        """Send `payload` and wait for the message answering request `key`."""
        if self._ws is None or self._ws.closed or not self.connected:
            raise KoneConnectionError("WebSocket not connected")
        self._guard(payload)
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = _Pending(future, expect_data)
        try:
            await self._ws.send_str(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as err:
            pending = self._pending.get(key)
            if pending is not None and pending.messages:
                # Acknowledged, but the data never followed: return the ack.
                return pending.messages[-1]
            raise KoneTimeout(f"No answer to {payload.get('callType')}") from err
        finally:
            self._pending.pop(key, None)

    # Elevator Call API v2: common-api ------------------------------------

    async def _common(
        self, building_id: str, group_id: str, call_type: str, **extra: Any
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = {
            "type": "common-api",
            "requestId": request_id,
            "buildingId": prefixed_building_id(building_id),
            "callType": call_type,
            "groupId": str(group_id),
            **extra,
        }
        return await self.request(payload, request_id, expect_data=True)

    async def config(self, building_id: str, group_id: str) -> dict[str, Any]:
        """The group's topology: lifts, floors and destination areas."""
        return await self._common(building_id, group_id, "config")

    async def actions(self, building_id: str, group_id: str) -> dict[str, Any]:
        """The call types (actions) this group accepts."""
        return await self._common(building_id, group_id, "actions")

    async def ping(self, building_id: str, group_id: str) -> dict[str, Any]:
        request_id = new_request_id()
        payload = {
            "type": "common-api",
            "buildingId": prefixed_building_id(building_id),
            "callType": "ping",
            "groupId": str(group_id),
            "payload": {"request_id": request_id},
        }
        return await self.request(payload, str(request_id))

    # Elevator Call API v2: lift calls ------------------------------------

    async def call(
        self,
        building_id: str,
        group_id: str,
        source_area: int,
        destination_area: int | None = None,
        action: int = ACTION_DESTINATION,
        terminal: int = 1,
        extra_call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a lift call. The answer's `data.session_id` identifies it."""
        request_id = new_request_id()
        call: dict[str, Any] = {"action": int(action)}
        if destination_area is not None:
            call["destination"] = int(destination_area)
        if extra_call:
            call.update(extra_call)
        payload = {
            "type": "lift-call-api-v2",
            "buildingId": prefixed_building_id(building_id),
            "callType": "action",
            "groupId": str(group_id),
            "payload": {
                "request_id": request_id,
                "area": int(source_area),
                "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "terminal": int(terminal),
                "call": call,
            },
        }
        return await self.request(payload, str(request_id), expect_data=True)

    async def cancel(
        self, building_id: str, group_id: str, session_id: int
    ) -> dict[str, Any]:
        payload = {
            "type": "lift-call-api-v2",
            "buildingId": prefixed_building_id(building_id),
            "callType": "delete",
            "groupId": str(group_id),
            "payload": {"session_id": int(session_id)},
        }
        # Delete answers carry no request id of ours; fire and forget.
        self._guard(payload)
        if self._ws is None or self._ws.closed:
            raise KoneConnectionError("WebSocket not connected")
        await self._ws.send_str(json.dumps(payload))
        return payload

    def _guard(self, payload: dict[str, Any]) -> None:
        """Refuse lift control on a read-only client.

        Every send goes through here, so this holds however a call is
        reached: entity, service, or code using the client directly.
        """
        if self.read_only and payload.get("type") in CONTROL_MESSAGE_TYPES:
            raise KoneControlDisabled("Lift control is disabled for this connection")

    # Site Monitoring ------------------------------------------------------

    def subscribe(
        self,
        building_id: str,
        group_id: str,
        subtopics: tuple[str, ...] = DEFAULT_SUBTOPICS,
    ) -> None:
        """Monitor a group for as long as this client runs."""
        key = (str(building_id), str(group_id))
        self._subs[key] = _Subscription(
            building_id=str(building_id),
            group_id=str(group_id),
            subtopics=tuple(subtopics),
            sub=f"{self._client_name}-{uuid.uuid4().hex[:8]}",
        )
        self._subs_changed.set()

    def unsubscribe(self, building_id: str, group_id: str) -> None:
        self._subs.pop((str(building_id), str(group_id)), None)

    async def monitor_once(
        self,
        building_id: str,
        group_id: str,
        subtopics: list[str],
        duration: int = MONITOR_DURATION,
    ) -> dict[str, Any]:
        """A one-off subscription, e.g. to one call's `call_state/<id>/+`."""
        return await self._send_subscription(
            _Subscription(
                str(building_id),
                str(group_id),
                tuple(subtopics),
                f"{self._client_name}-{uuid.uuid4().hex[:8]}",
            ),
            duration,
        )

    async def _send_subscription(
        self, sub: _Subscription, duration: int = MONITOR_DURATION
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = {
            "type": "site-monitoring",
            "requestId": request_id,
            "buildingId": prefixed_building_id(sub.building_id),
            "callType": "monitor",
            "groupId": sub.group_id,
            "payload": {
                "sub": sub.sub,
                "duration": duration,
                "subtopics": list(sub.subtopics),
            },
        }
        return await self.request(payload, request_id)


def new_request_id() -> int:
    """Lift-call request ids are integers in KONE's v2 API."""
    return random.randint(1, 999_999_999)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _request_key(msg: dict) -> str | None:
    if msg.get("requestId") is not None:
        return str(msg["requestId"])
    data = msg.get("data")
    if isinstance(data, dict) and data.get("request_id") is not None:
        return str(data["request_id"])
    return None


def _is_error(msg: dict) -> bool:
    if msg.get("type") == "error":
        return True
    status = _int(msg.get("statusCode"))
    return status is not None and status >= 400


def _has_data(msg: dict) -> bool:
    return bool(msg.get("data")) or "callType" in msg
