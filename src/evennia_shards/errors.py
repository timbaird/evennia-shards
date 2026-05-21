# SPDX-License-Identifier: BSD-3-Clause
"""Exceptions raised by evennia-shards."""


class MessageBusError(Exception):
    """Raised on misuse of the cross-shard message bus."""


class TicketError(Exception):
    """Raised when a ticket token is invalid, expired, or already consumed."""
