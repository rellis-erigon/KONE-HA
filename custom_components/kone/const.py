"""Constants for the KONE integration."""

from __future__ import annotations

DOMAIN = "kone"
MANUFACTURER = "KONE"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"
CONF_HOSTNAME = "hostname"

#: Options: {building_id: {name, groups: [..], terminal, kens: [..]}}
OPT_BUILDINGS = "buildings"
OPT_SCAN_INTERVAL = "scan_interval"
OPT_SERVICE_INFO = "service_info"
OPT_MONITORING = "monitoring"
#: Off means read-only: nothing that changes lift operation is sent.
OPT_ALLOW_CONTROL = "allow_control"

B_NAME = "name"
B_GROUPS = "groups"
B_TERMINAL = "terminal"
B_KENS = "kens"

DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 60
DEFAULT_TERMINAL = 1

SERVICE_CALL_LIFT = "call_lift"
SERVICE_CANCEL_CALL = "cancel_call"
SERVICE_REFRESH_TOPOLOGY = "refresh_topology"
