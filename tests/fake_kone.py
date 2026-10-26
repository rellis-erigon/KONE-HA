"""A local stand-in for the KONE API, speaking the shapes in KONE's examples.

It is deliberately small: enough to drive the token, resources, equipment
status and WebSocket paths end to end, and to record what was sent.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from aiohttp import BasicAuth, WSMsgType, web

CLIENT_ID = "test-client"
CLIENT_SECRET = "test-secret"
BUILDING = "9990000951"
GROUP = "1"
KEN = "ken:100001"

TOPOLOGY = {
    "groups": [
        {
            "group_id": 1,
            "lifts": [
                {"lift_id": 1, "lift_name": "A", "floors": [{"floor": 1}, {"floor": 2}, {"floor": 3}]},
                {"lift_id": 2, "lift_name": "B", "floors": [{"floor": 1}, {"floor": 2}, {"floor": 3}]},
            ],
        }
    ],
    "destinations": [
        {"area_id": 1000, "short_name": "G", "exit": True,
         "lifts": [{"lift_id": 1, "floor": 1}, {"lift_id": 2, "floor": 1}]},
        {"area_id": 2000, "short_name": "1",
         "lifts": [{"lift_id": 1, "floor": 2}, {"lift_id": 2, "floor": 2}]},
        {"area_id": 3000, "short_name": "2",
         "lifts": [{"lift_id": 1, "floor": 3}, {"lift_id": 2, "floor": 3}]},
    ],
}


@dataclass
class FakeKone:
    denied_scopes: set[str] = field(default_factory=set)
    token_requests: list[list[str]] = field(default_factory=list)
    ws_received: list[dict] = field(default_factory=list)
    sockets: list[web.WebSocketResponse] = field(default_factory=list)
    availability_state: str = "IN_SERVICE"
    #: Answer Equipment Status as the live sandbox does for an application
    #: with no webhook subscription.
    require_webhook: bool = False
    entrapment: bool = False
    next_session_id: int = 555
    url: str = ""
    _runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/api/v2/oauth2/token", self._token)
        app.router.add_get("/api/v2/application/self/resources", self._resources)
        app.router.add_post("/api/v2/equipment/search/availability", self._availability)
        app.router.add_post("/api/v2/equipment/search/status", self._status)
        app.router.add_get("/stream-v2", self._ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        for ws in list(self.sockets):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # REST -----------------------------------------------------------------

    async def _token(self, request: web.Request) -> web.Response:
        auth = BasicAuth.decode(request.headers.get("Authorization", ""))
        if (auth.login, auth.password) != (CLIENT_ID, CLIENT_SECRET):
            return web.json_response({"message": "invalid client"}, status=401)
        form = await request.post()
        scopes = str(form.get("scope", "")).split()
        self.token_requests.append(scopes)
        if self.denied_scopes & set(scopes):
            return web.json_response({"message": "scope not granted"}, status=403)
        return web.json_response(
            {"access_token": "tok-" + str(len(self.token_requests)), "expires_in": 3600}
        )

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization", "").startswith("Bearer tok-")

    async def _resources(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({}, status=401)
        return web.json_response(
            {
                "buildings": [
                    {"id": f"building:{BUILDING}", "name": "Test Tower", "apiKens": [KEN]}
                ],
                "groups": [{"id": f"group:{BUILDING}:{GROUP}"}],
                "equipments": [{"id": KEN, "description": "Passenger lift A"}],
            }
        )

    async def _availability(self, request: web.Request) -> web.Response:
        if self.require_webhook:
            return web.json_response(
                {"message": "You need to create a webhook subscription to make API call."}, status=400
            )
        body = await request.json()
        return web.json_response(
            [{"equipmentId": k, "state": self.availability_state} for k in body["equipmentIds"]]
        )

    async def _status(self, request: web.Request) -> web.Response:
        if self.require_webhook:
            return web.json_response(
                {"message": "You need to create a webhook subscription to make API call."}, status=400
            )
        body = await request.json()
        return web.json_response(
            [
                {
                    "equipmentId": k,
                    "entrapment": self.entrapment,
                    "outOfOrder": False,
                    "status": "normal",
                    "maintenance": {"status": "none"},
                }
                for k in body["equipmentIds"]
            ]
        )

    # WebSocket ------------------------------------------------------------

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        if not request.query.get("accessToken", "").startswith("tok-"):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(protocols=("koneapi",))
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._handle(ws, json.loads(msg.data))
        finally:
            self.sockets.remove(ws)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, msg: dict) -> None:
        self.ws_received.append(msg)
        kind, call_type = msg.get("type"), msg.get("callType")
        base = {"connectionId": "conn-1", "buildingId": msg.get("buildingId"), "groupId": msg.get("groupId")}

        if kind == "common-api" and call_type == "config":
            await ws.send_json({**base, "type": "ok", "requestId": msg["requestId"], "statusCode": 201})
            await ws.send_json({**base, "requestId": msg["requestId"], "callType": "config", "data": TOPOLOGY})
        elif kind == "common-api" and call_type == "actions":
            await ws.send_json(
                {**base, "requestId": msg["requestId"], "callType": "actions",
                 "data": {"call_types": [{"action_id": 2, "name": "Destination call"}]}}
            )
        elif kind == "site-monitoring":
            await ws.send_json({**base, "type": "ok", "requestId": msg["requestId"], "statusCode": 201})
        elif kind == "lift-call-api-v2" and call_type == "action":
            request_id = msg["payload"]["request_id"]
            await ws.send_json(
                {**base, "requestId": request_id, "statusCode": 201,
                 "data": {"time": "2026-10-25T00:00:00Z", "session_id": self.next_session_id}}
            )

    async def push(self, subtopic: str, data: dict[str, Any]) -> None:
        """Send a monitoring event to every connected client."""
        for ws in list(self.sockets):
            await ws.send_json(
                {
                    "callType": "monitor",
                    "buildingId": f"building:{BUILDING}",
                    "groupId": GROUP,
                    "subtopic": subtopic,
                    "data": data,
                }
            )
        await asyncio.sleep(0.05)
