"""evennia-shards: optional split deployment and sharding for Evennia."""

from .config import (
    ROLE_MONOLITH,
    ROLE_ROUTER,
    ROLE_SHARD,
    get_message_timeout,
    get_role,
    get_router_shard_id,
    get_router_url,
    get_shard_id,
    get_shard_url,
)
from .errors import MessageBusError, TicketError
from .handoff import MoveResult, cross_shard_move
from .messagebus import (
    MessageHandler,
    delete_message,
    poll_messages,
    process_inbox,
    send_message,
    start_message_bus,
)
from .messaging import send_cross_shard_message, send_cross_shard_room_message
from .search import ShardSearchResult, shard_aware_global_search
from .tenancy import (
    GLOBAL_SHARD_ID,
    Shard,
    clear_shard_context,
    set_current_shard,
    shard_context,
)
from .tickets import create_ticket, delete_ticket, get_ticket

__version__ = "0.0.1"

__all__ = [
    "ROLE_MONOLITH",
    "ROLE_ROUTER",
    "ROLE_SHARD",
    "get_role",
    "get_shard_id",
    "get_shard_url",
    "get_router_shard_id",
    "get_router_url",
    "get_message_timeout",
    "send_message",
    "poll_messages",
    "delete_message",
    "MessageHandler",
    "process_inbox",
    "start_message_bus",
    "send_cross_shard_message",
    "send_cross_shard_room_message",
    "create_ticket",
    "get_ticket",
    "delete_ticket",
    "shard_aware_global_search",
    "ShardSearchResult",
    "cross_shard_move",
    "MoveResult",
    "MessageBusError",
    "TicketError",
    # Multitenant tenancy primitives (replaced shard_writes_allowed_for
    # / ShardIsolationError from the chokepoint era).
    "GLOBAL_SHARD_ID",
    "Shard",
    "set_current_shard",
    "clear_shard_context",
    "shard_context",
    "__version__",
]
