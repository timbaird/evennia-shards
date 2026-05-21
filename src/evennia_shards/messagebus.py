# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard message bus primitives.

Low-level CRUD over the `Message` table. Higher-level pieces — the
polling cycle, dispatch, undeliverable_reply lifecycle, and consumer-
overrideable handler hook — build on these. See
DESIGN/cross-shard-message-bus.md for the full design.
"""

import logging

log = logging.getLogger(__name__)


def send_message(
    kind: str,
    payload: dict,
    to_shard: str,
    from_shard: str | None = None,
):
    """Insert a message row addressed to `to_shard`.

    If `from_shard` is omitted, defaults to the current shard's `SHARD_ID`.
    Returns the created `Message` instance.

    Raises `MessageBusError` if `to_shard == from_shard` — the bus is for
    cross-shard messaging; same-shard sends are almost always a bug or a
    misconfigured `SHARD_ID`. For deferred work on the same shard, use
    Twisted's `reactor.callLater` or call the function directly.
    """
    from .config import get_shard_id
    from .errors import MessageBusError
    from .models import Message

    if from_shard is None:
        from_shard = get_shard_id()
    if to_shard == from_shard:
        raise MessageBusError(
            f"send_message refused: to_shard ({to_shard!r}) equals from_shard "
            f"({from_shard!r}). The bus is for cross-shard messaging only."
        )
    return Message.objects.create(
        kind=kind,
        payload=payload,
        to_shard=to_shard,
        from_shard=from_shard,
    )


def poll_messages(shard_id: str | None = None):
    """Return all `Message` rows addressed to `shard_id`, oldest first.

    If `shard_id` is omitted, defaults to the current shard's `SHARD_ID`.
    Returns a `QuerySet` — the caller composes further filtering, slicing,
    iteration, or counting. Does not delete or mutate; use `delete_message`
    after a row has been processed.
    """
    from .config import get_shard_id
    from .models import Message

    if shard_id is None:
        shard_id = get_shard_id()
    return Message.objects.filter(to_shard=shard_id).order_by("created_at")


def delete_message(message) -> None:
    """Delete a processed message row.

    Thin wrapper over `message.delete()` so the API surface is consistent
    (send / poll / delete) and so future side-effects (metrics, idempotency
    bookkeeping) can land here without consumer changes.
    """
    message.delete()


class MessageHandler:
    """Base class for cross-shard message handlers.

    The polling cycle calls `handle(message)` for each polled message.
    Return truthy to mark the message processed (it will be deleted);
    return falsy to defer (the message stays for the next poll cycle).

    Library-shipped kinds get handled in this base class:

    - `ping` — diagnostic round-trip. Replies with a `ping_received`
      message addressed to the original sender, echoing the payload.
    - `ping_received` — silently consumed. (Useful for inspection: poll
      the inbox before the next cycle to observe replies; the consumer
      can override this method to surface them.)
    - `undeliverable_reply` — silently consumed. Notification that an
      earlier outbound message we sent timed out. Consumers can override
      to surface failures (UI, retry, metrics).
    - `obj_msg` — deliver a player-facing message to an `ObjectDB` row
      on this shard. Payload is `{"pk": <int>, "kwargs": <dict>}`; the
      handler resolves the row and calls `obj.msg(**kwargs)`. Evennia's
      own `Object.msg` then does the local session fanout.
    - `account_msg` — same shape, targeting an `AccountDB` row. Used
      for OOC delivery (tells, system messages, account-level channel
      msgs). Receiver looks up the account and calls
      `account.msg(**kwargs)`.
    - `room_msg` — multicast variant: deliver text to every obj in a
      room's contents on this shard. Payload is
      `{"room_pk": <int>, "text": <str>, "exclude_pks": [<int>, ...]?,
      "from_obj_pk": <int>?}`; receiver looks up the room and calls
      `room.msg_contents(text, exclude=..., from_obj=...)`. Sender
      composes the rendered text. Generic over use case — teleport
      arrival announces, future cross-shard world events, system
      messages — anywhere a foreign room needs to say something.
    - `flush_from_cache` — evict pks from this shard's idmapper. See
      the handler docstring for the cache-invariance use case.

    Consumers extend by subclassing:

        class MyHandler(MessageHandler):
            def handle(self, message):
                if super().handle(message):
                    return True
                if message.kind == "tell":
                    deliver_tell(message.payload)
                    return True
                return False
    """

    def handle(self, message) -> bool:
        if message.kind == "ping":
            return self._handle_ping(message)
        if message.kind == "ping_received":
            return self._handle_ping_received(message)
        if message.kind == "undeliverable_reply":
            return self._handle_undeliverable_reply(message)
        if message.kind == "obj_msg":
            return self._handle_obj_msg(message)
        if message.kind == "account_msg":
            return self._handle_account_msg(message)
        if message.kind == "flush_from_cache":
            return self._handle_flush_from_cache(message)
        if message.kind == "room_msg":
            return self._handle_room_msg(message)
        return False

    def _handle_ping(self, message) -> bool:
        from .config import get_shard_id

        current = get_shard_id()
        # If the ping has no usable return address, consume it silently;
        # the ping is "received" but unreplied.
        if message.from_shard is None or message.from_shard == current:
            return True
        send_message(
            kind="ping_received",
            payload={"original_pk": message.pk, "echo": message.payload},
            to_shard=message.from_shard,
            from_shard=current,
        )
        return True

    def _handle_ping_received(self, message) -> bool:
        # Diagnostic reply — consumed silently in the base. Consumers
        # who want to observe ping replies can override this method.
        return True

    def _handle_undeliverable_reply(self, message) -> bool:
        # Notification that an earlier outbound message we sent timed
        # out without being processed. Consumed silently in the base;
        # consumers who care about delivery failure can override to
        # surface it (UI notification, retry logic, metrics, etc.).
        return True

    def _handle_obj_msg(self, message) -> bool:
        """Deliver a player-facing message to an ObjectDB row.

        Resolves the row by pk and calls ``obj.msg(**kwargs)`` —
        Evennia's own ``Object.msg`` handles session fanout (multiple
        sessions puppeting the same object in MULTISESSION_MODE 2/3).

        Target-gone semantics: if the row no longer exists, log a
        warning and return ``True`` (consumed). The bus is real-time
        only; deferring won't bring a deleted target back, and an
        ``undeliverable_reply`` for routine "character deleted between
        send and receive" is noise. Consumers wanting different
        semantics can override.

        Misroute safety: if the row exists but is owned by another
        shard, ``ObjectDB.objects.get`` raises ``ShardIsolationError``
        via the ``from_db`` chokepoint — caught by ``process_inbox``
        and treated as defer, eventually triggering
        ``undeliverable_reply`` to the sender. Loud failure on
        misroute, by construction.
        """
        from evennia.objects.models import ObjectDB

        pk = message.payload.get("pk")
        kwargs = message.payload.get("kwargs", {})
        try:
            obj = ObjectDB.objects.get(pk=pk)
        except ObjectDB.DoesNotExist:
            log.warning(
                "obj_msg: target ObjectDB pk=%r not found; dropping "
                "(message pk=%s from %r)",
                pk, message.pk, message.from_shard,
            )
            return True
        obj.msg(**kwargs)
        return True

    def _handle_account_msg(self, message) -> bool:
        """Deliver a player-facing message to an AccountDB row.

        Used for OOC delivery — tells, system messages, account-level
        channel msgs. Same shape as ``_handle_obj_msg`` but resolves
        ``AccountDB``. ``Account.msg`` handles fanout to all of the
        account's sessions on this shard.

        Target-gone and misroute semantics match ``_handle_obj_msg``.
        AccountDB has no shard-isolation chokepoint (accounts are
        router-owned and global), so misroute manifests as a
        ``DoesNotExist`` rather than a chokepoint raise.
        """
        from evennia.accounts.models import AccountDB

        pk = message.payload.get("pk")
        kwargs = message.payload.get("kwargs", {})
        try:
            account = AccountDB.objects.get(pk=pk)
        except AccountDB.DoesNotExist:
            log.warning(
                "account_msg: target AccountDB pk=%r not found; dropping "
                "(message pk=%s from %r)",
                pk, message.pk, message.from_shard,
            )
            return True
        account.msg(**kwargs)
        return True

    def _handle_flush_from_cache(self, message) -> bool:
        """Evict pks from this process's ObjectDB idmapper.

        Generic cache-invalidation primitive. The sender publishes
        ``flush_from_cache`` with payload ``{"pks": [int, ...]}`` to
        tell a peer shard "your in-process cached instances of these
        rows are out of date; drop them so the next access reloads
        from the DB."

        Per-pk behaviour:

        - pk is in this process's idmapper → call
          ``instance.flush_from_cache(force=True)``, which removes
          the Python instance from the cache. Next
          ``ObjectDB.objects.get(pk=N)`` misses the cache, hits the
          DB, and constructs a fresh instance. Per-instance state
          (notably ``contents_cache``) is gone; lazy attributes
          rebuild from current DB.
        - pk is not currently cached → nothing to do, no-op.
        - pk no longer exists in the DB → still nothing to do here;
          the row's absence is the next caller's problem to handle.

        The handler is idempotent: re-sending the same flush message
        is harmless.

        Primary current consumer: ``cross_shard_move`` sends the
        destination room's pk so the destination shard re-reads the
        room's contents on next access (otherwise the contents-cache
        on a previously-loaded room misses the just-arrived object).
        The primitive is deliberately generic — any cross-shard
        mutation that other shards need to notice can publish here
        without needing a new message kind.
        """
        from evennia.objects.models import ObjectDB

        pks = message.payload.get("pks") or []
        cache = ObjectDB.__dbclass__.__instance_cache__
        for pk in pks:
            instance = cache.get(pk)
            if instance is not None:
                instance.flush_from_cache(force=True)
        return True

    def _handle_room_msg(self, message) -> bool:
        """Deliver a multicast message to a room's contents.

        Resolves the room by pk and calls
        ``room.msg_contents(text, exclude=..., from_obj=...)`` —
        Evennia's own ``Object.msg_contents`` handles fanout to every
        obj currently in the room's contents.

        Payload shape:

        - ``room_pk`` (int, required) — the target room.
        - ``text`` (str, required) — the rendered text to broadcast.
          Sender composes attribution and formatting; the bus is dumb.
        - ``exclude_pks`` (list[int], optional) — pks to skip when
          fanning out. Pks that don't resolve (deleted, on a yet-
          another shard) are skipped silently — they're hints, not
          the target.
        - ``from_obj_pk`` (int, optional) — pk of the sender, looked
          up locally and passed as ``from_obj`` for Evennia's
          attribution machinery. Skipped silently if it doesn't
          resolve.

        Target-gone semantics: room missing → log warning, return
        ``True`` (consumed). Matches ``_handle_obj_msg``.

        Misroute safety: ``ObjectDB.objects.get(pk=room_pk)`` trips
        the ``from_db`` chokepoint if the room is on another shard —
        caught by ``process_inbox`` and treated as defer, eventually
        triggering ``undeliverable_reply``. Loud failure on misroute
        of the primary target. The optional pks are caught locally so
        a stale hint can't time-bomb the whole message.

        Primary current consumer: the ``ShardAwareCmdTeleport``
        cross-shard branch publishes ``room_msg`` for arrival
        announces on the destination room after ``cross_shard_move``
        completes. The primitive is generic — any consumer needing to
        broadcast to a foreign room (world events, system messages,
        future cross-shard chat) can publish here without inventing a
        new kind.
        """
        from evennia.objects.models import ObjectDB

        pk = message.payload.get("room_pk")
        text = message.payload.get("text")
        try:
            room = ObjectDB.objects.get(pk=pk)
        except ObjectDB.DoesNotExist:
            log.warning(
                "room_msg: target ObjectDB pk=%r not found; dropping "
                "(message pk=%s from %r)",
                pk, message.pk, message.from_shard,
            )
            return True

        exclude = []
        for ex_pk in message.payload.get("exclude_pks") or []:
            try:
                exclude.append(ObjectDB.objects.get(pk=ex_pk))
            except ObjectDB.DoesNotExist:
                # Stale or misrouted hint — drop silently and proceed.
                continue

        from_obj = None
        from_obj_pk = message.payload.get("from_obj_pk")
        if from_obj_pk is not None:
            try:
                from_obj = ObjectDB.objects.get(pk=from_obj_pk)
            except ObjectDB.DoesNotExist:
                # Attribution lost but the message still goes.
                from_obj = None

        room.msg_contents(text, exclude=exclude, from_obj=from_obj)
        return True


def process_inbox(handler: MessageHandler | None = None) -> int:
    """Run one polling cycle: poll, dispatch, delete on success or timeout.

    Lifecycle per message:
    - handler returns truthy -> success, delete (counted as processed)
    - handler returns falsy AND age <= lifespan -> defer for next cycle
    - handler returns falsy AND age > lifespan -> insert
      `undeliverable_reply` to original `from_shard` (if there is one
      and it isn't us), then delete the original

    A handler that raises is logged and the message is treated as falsy
    for the rest of the cycle — it can still be deferred or timed out
    on this pass.

    Returns the count of messages that were successfully processed by
    the handler (timed-out messages are counted separately in logs but
    not in the return value).

    Pure function — testable without the reactor; the polling loop wraps
    this in a Twisted `LoopingCall`.
    """
    from django.utils import timezone

    from .config import get_message_timeout, get_shard_id

    if handler is None:
        handler = MessageHandler()

    processed = 0
    now = timezone.now()
    for msg in poll_messages():
        try:
            handled = bool(handler.handle(msg))
        except Exception:
            log.exception(
                "MessageHandler raised on pk=%s kind=%r; treating as defer",
                msg.pk,
                msg.kind,
            )
            handled = False

        if handled:
            delete_message(msg)
            processed += 1
            continue

        lifespan = get_message_timeout(msg.kind)
        age = (now - msg.created_at).total_seconds()
        if age <= lifespan:
            continue

        # Aged out. Reply with undeliverable to the original sender, if
        # there is a valid one, then drop the original.
        current = get_shard_id()
        if msg.from_shard and msg.from_shard != current:
            send_message(
                kind="undeliverable_reply",
                payload={
                    "original_kind": msg.kind,
                    "original_payload": msg.payload,
                    "reason": "timeout",
                },
                to_shard=msg.from_shard,
                from_shard=current,
            )
        else:
            log.warning(
                "Message pk=%s kind=%r timed out with no valid from_shard "
                "(%r); dropping without reply",
                msg.pk,
                msg.kind,
                msg.from_shard,
            )
        delete_message(msg)
    return processed


def start_message_bus(
    handler: MessageHandler | None = None,
    interval: float = 0.5,
):
    """Start the cross-shard message bus polling loop.

    Registers a Twisted `LoopingCall` that runs `process_inbox(handler)`
    every `interval` seconds. Call once from the consumer's
    `at_server_start()` hook. Returns the `LoopingCall` so the consumer
    can stop it if needed.

    If `handler` is omitted, the library's base `MessageHandler` is used
    (which currently does nothing — every message defers and eventually
    times out). Consumers wanting message dispatch must pass a subclass.
    """
    from twisted.internet.task import LoopingCall

    loop = LoopingCall(process_inbox, handler)
    loop.start(interval, now=False)
    return loop
