# KONE Elevators for Home Assistant

A Home Assistant custom integration for the [KONE APIs](https://dev.kone.com/).
Anyone with a KONE API Portal application can use it with their own client ID
and secret. Add as many buildings as your application has been granted, and
each one shows up as a device with its lift groups, lifts and equipment under it.

> **Status: 0.1.0, not yet tested against a live KONE sandbox.** The message
> shapes come from KONE's official
> [kone-api-examples](https://github.com/konecorp/kone-api-examples) and the
> API portal docs, and the integration has been tested end to end against a
> local fake of the API (`tests/fake_kone.py`). Some response field names,
> particularly in the `config` topology and the monitoring events, may differ on
> a real site. The parsers try several spellings and skip anything they don't
> recognise rather than guess. If something shows as unknown, download
> diagnostics (see [Troubleshooting](#troubleshooting)) and open an issue.

## What you get

| Device | Entities | Source |
|---|---|---|
| **Building** | parent device for everything below | config |
| **Lift group** | `From floor` / `To floor` selects, `Call lift` and `Cancel call` buttons, `Last call` sensor (status, session ID, allocated lift, list of floors and site actions), `Connected`, `Refresh floors` | Elevator Call API v2 (WebSocket) |
| **Lift** | `Floor` (shows the button label, e.g. `G`, `12`), `Direction`, `Motion`, `Door`, `Fault`, `Mode` | Site Monitoring (WebSocket, push) |
| **Equipment** (`ken:…`) | `Availability`, `Maintenance`, `Entrapment`, `Out of order`, plus optional `Service orders` | Equipment Status API 2.0 / Service Info API 2.0 (REST, polled) |

Lift data is pushed live over a single WebSocket per KONE application. It
reconnects with backoff, reconnects before the token expires, and renews
monitoring subscriptions before KONE's 300-second limit runs out. Equipment status
is polled, every 5 minutes by default, to stay inside API quotas.

## Requirements

1. A KONE API Portal account and an application (sandbox or production) at
   <https://dev.kone.com/>.
2. The application must be **granted** the buildings, lift groups and
   equipment you want to use, and subscribed to the APIs you need:
   - lift calls and live lift state: **Elevator Call API** (scope
     `callgiving/group:BUILDING:GROUP`)
   - equipment status: **Equipment Status API 2.0** (scope `rtm/ken:NNN`)
   - service orders: **Service Info API 2.0** (scope `serviceinfo/ken:NNN`)

   The integration builds the scope list from what you configure and asks
   for one token covering all of it. If KONE refuses a scope, the setup
   form tells you rather than half-loading.
3. Home Assistant 2026.2 or newer.

## Install

**HACS:** add `https://github.com/rellis-erigon/KONE-HA` as a custom
repository (category *Integration*), install **KONE Elevators**, restart.

**Manual:** copy `custom_components/kone` into your Home Assistant
`config/custom_components/` folder and restart.

## Set up

*Settings → Devices & services → Add integration → KONE Elevators*

1. **Credentials:** client ID, client secret, API host (`dev.kone.com`
   unless KONE gave you another), and **Allow lift control** (off by
   default, see [Read-only mode](#read-only-mode)).
2. **Buildings:** tick the buildings your application can see, or type
   building IDs if one isn't listed.
3. **For each building:** a name, the lift group IDs (comma separated,
   usually `1`), the terminal ID to place calls from, and the equipment
   (`ken:` numbers) to poll. Leave groups empty for a status-only building, or
   equipment empty for a calls-only one.

Afterwards, go to **Configure** on the integration to:

- **Add a building**: adds new devices without touching the existing ones
- **Edit a building**: change groups, terminal or equipment
- **Remove buildings**
- **Settings**: poll interval, live monitoring on/off, service order
  polling, and **Allow lift control**

Each KONE application is one config entry. If you have several sets of
credentials, add the integration once per set.

## Read-only mode

**Allow lift control** is **off by default**. While it is off, nothing this
integration does can change how a lift operates:

- the **Call lift** and **Cancel call** buttons and the **From/To floor**
  pickers are not created (if control was on before, they are deleted
  from the entity registry, so they don't linger on dashboards)
- the `kone.call_lift` and `kone.cancel_call` actions are refused with an
  error
- the API client itself refuses to send any lift-call message. This is the
  last line of defence: it holds however a call is attempted.

Monitoring, equipment status, diagnostics and **Refresh floors** keep
working: they only read. Turn control on or off at any time under
*Configure → Settings*. The integration reloads, and the buttons appear or
go away.

KONE scopes don't separate monitoring from calling (both need
`callgiving/group:…`). For a guarantee enforced on KONE's side as well,
use a KONE application that hasn't been granted the Elevator Call API, and
set up equipment status only.

## Calling a lift

*Requires **Allow lift control**.*

From a dashboard, pick **From floor** and **To floor** on the group device
and press **Call lift**.

From automations and scripts:

```yaml
action: kone.call_lift
data:
  device_id: <lift group device>   # or building_id + group_id
  from_floor: "G"                  # button label or area ID
  to_floor: "12"
response_variable: call            # call.session_id
```

```yaml
action: kone.cancel_call            # defaults to the group's last call
data:
  device_id: <lift group device>
```

`action` defaults to `2`, a destination call (DCS). Landing calls and other
call types use site-specific action codes. The group's **Last call** sensor
lists the site's actions in its `actions` attribute. Pass `action:` (and
leave out `to_floor` for a landing call) to use them.

> ⚠️ These are real lifts. In a production building a call moves a real car.
> Test against a KONE sandbox application first.

## Troubleshooting

- **Diagnostics:** *Settings → Devices & services → KONE → ⋮ → Download
  diagnostics* gives the raw topology, the parsed lift state, the token scopes
  and the last 50 WebSocket messages. Secrets and tokens are redacted.
- **Debug logging:**
  ```yaml
  logger:
    logs:
      custom_components.kone: debug
  ```
- **`scope_refused`:** the application hasn't been granted that group or
  piece of equipment in the API Portal, or isn't subscribed to the API.
- **Floor shows a number instead of a label:** the floor index wasn't in
  the group's topology. Press **Refresh floors**, and if it still happens,
  include diagnostics in an issue.

## Using the API client on its own

`custom_components/kone/api/` doesn't import Home Assistant. It is a small
asyncio client (aiohttp only) that you can use elsewhere:

```python
from custom_components.kone.api import (
    TokenManager, KoneRestClient, KoneWebSocket, callgiving_scope,
)

tokens = TokenManager(session, CLIENT_ID, CLIENT_SECRET,
                      scopes=["application/inventory", callgiving_scope("9990000951", "1")])
ws = KoneWebSocket(session, tokens, on_event=print)
task = asyncio.create_task(ws.run())
await ws.wait_connected(30)
topology = await ws.config("9990000951", "1")
ws.subscribe("9990000951", "1")              # live position/doors/status
await ws.call("9990000951", "1", source_area=1000, destination_area=3000)
```

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The tests run the real integration against `tests/fake_kone.py`, a local
aiohttp server that speaks KONE's token, resources, equipment-status and
WebSocket protocols. No KONE credentials are needed.

## Licence

MIT. Not affiliated with or endorsed by KONE Corporation. "KONE" is a
trademark of KONE Corporation.
