"""A small async client for the KONE APIs.

Nothing in this package imports Home Assistant, so it can be lifted out and
used on its own:

    tokens = TokenManager(session, client_id, client_secret, scopes=[...])
    rest = KoneRestClient(session, tokens)
    ws = KoneWebSocket(session, tokens, on_event=print)
"""

from .auth import (
    DEFAULT_HOSTNAME,
    SCOPE_INVENTORY,
    TokenManager,
    callgiving_scope,
    equipment_status_scope,
    service_info_scope,
)
from .errors import (
    KoneAuthError,
    KoneConnectionError,
    KoneError,
    KoneRequestError,
    KoneTimeout,
)
from .models import (
    Area,
    CallState,
    Lift,
    LiftState,
    Resources,
    Topology,
    apply_lift_event,
    lift_id_from_topic,
    parse_resources,
    parse_topology,
)
from .rest import KoneRestClient
from .websocket import ACTION_DESTINATION, DEFAULT_SUBTOPICS, KoneWebSocket

__all__ = [
    "ACTION_DESTINATION",
    "Area",
    "CallState",
    "DEFAULT_HOSTNAME",
    "DEFAULT_SUBTOPICS",
    "KoneAuthError",
    "KoneConnectionError",
    "KoneError",
    "KoneRequestError",
    "KoneRestClient",
    "KoneTimeout",
    "KoneWebSocket",
    "Lift",
    "LiftState",
    "Resources",
    "SCOPE_INVENTORY",
    "TokenManager",
    "Topology",
    "apply_lift_event",
    "callgiving_scope",
    "equipment_status_scope",
    "lift_id_from_topic",
    "parse_resources",
    "parse_topology",
    "service_info_scope",
]
