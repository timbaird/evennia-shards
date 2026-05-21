# SPDX-License-Identifier: BSD-3-Clause
"""Sender-side helpers for cross-shard player-facing messaging.

Built on top of the ``obj_msg`` and ``account_msg`` bus primitives in
``messagebus.py``. The primitives are the receiver-side handlers and
the bus is the transport; this module is the ergonomic sender API
that consumers call from command code.

Currently exposes one helper:

- :func:`send_cross_shard_message` — universal sender for delivering
  to any ``ObjectDB`` row (typically a character but generic over the
  hierarchy via the ``target_typeclass`` filter). Defaults the filter
  to ``settings.BASE_CHARACTER_TYPECLASS`` resolved at call time —
  the common case (player characters) requires zero arguments beyond
  the pk and kwargs.

An ``AccountDB``-side analogue (for OOC tells / system messages to
brand-new and never-yet-IC players) is deliberately not yet shipped;
it raises a separate routing question (no ``shard_id`` column on
``AccountDB``) that is being tackled as a follow-up.
"""

import logging

log = logging.getLogger(__name__)


def send_cross_shard_message(target_pk, kwargs, target_typeclass=None):
    """Deliver ``kwargs`` to ``target_pk``, locally or via the bus.

    Sender-side helper that wraps the ``obj_msg`` bus primitive with
    the ergonomic shape consumers actually want from command code:

    - **Local-vs-remote dispatch.** If the target row is on this shard
      (or is global, ``shard_id == "*"``), the helper looks up the
      instance and calls ``target.msg(**kwargs)`` directly — no bus
      hop, no polling latency. Otherwise it inserts an ``obj_msg``
      bus row addressed to the target's owning shard.
    - **Typeclass filter.** Validates that the target row's typeclass
      is a subclass of ``target_typeclass``. Defaults to the
      consumer-configured ``BASE_CHARACTER_TYPECLASS`` (resolved at
      call time, not import time, so test ``override_settings`` and
      consumer settings churn are picked up). Pass an explicit class
      to message NPC base classes, animated objects, vehicles, etc.;
      pass ``DefaultObject`` to opt out of filtering.
    - **Single DB read.** One ``.values_list`` query reads both the
      target's typeclass path and its ``shard_id`` (no ``from_db``
      construction, no chokepoint hit on the lookup itself).

    Args:
        target_pk: the recipient's ``ObjectDB`` pk.
        kwargs: a dict to be splatted into ``target.msg(**kwargs)``.
            Must be JSON-serialisable for the remote path (the
            ``Message.payload`` JSONField raises on non-serialisable
            content at send time). The local path doesn't enforce
            this — pass JSON-clean kwargs to keep behaviour symmetric.
            ``from_obj=`` (a common ``Object.msg`` kwarg pointing at
            another ``ObjectDB``) is not serialisable and not
            constructible cross-shard; render text first and drop it
            before calling this helper.
        target_typeclass: optional class to constrain the recipient's
            typeclass. ``None`` (default) resolves to
            ``settings.BASE_CHARACTER_TYPECLASS``.

    Returns:
        ``True`` if the message was delivered locally or queued for
        remote delivery. ``False`` if the helper rejected the call
        for a validation reason — target row doesn't exist, or the
        target's typeclass doesn't satisfy ``target_typeclass``. A
        warning is logged in both rejection cases. Errors from the
        bus (e.g. ``MessageBusError``) propagate.
    """
    from django.conf import settings
    from evennia.objects.models import ObjectDB
    from evennia.utils.utils import class_from_module

    from .config import get_shard_id
    from .messagebus import send_message
    from .tenancy import shard_context

    if target_typeclass is None:
        target_typeclass = class_from_module(settings.BASE_CHARACTER_TYPECLASS)

    # Escape the multitenant auto-filter for the visibility lookup —
    # if the target lives on a foreign shard, the auto-filter would
    # exclude its row from values_list and the helper would falsely
    # report "does not exist". Building the queryset inside the
    # context block is the gating step (get_queryset reads the tenant
    # at construction, not at execute). Same pattern as search.py.
    with shard_context(None):
        rows = list(
            ObjectDB.objects.filter(pk=target_pk)
            .values_list("db_typeclass_path", "shard_id")[:1]
        )
    if not rows:
        log.warning(
            "send_cross_shard_message: target ObjectDB pk=%r does not "
            "exist; rejecting",
            target_pk,
        )
        return False

    typeclass_path, target_shard = rows[0]
    target_cls = class_from_module(typeclass_path)
    if not issubclass(target_cls, target_typeclass):
        log.warning(
            "send_cross_shard_message: target ObjectDB pk=%r has typeclass "
            "%s which is not a subclass of %s; rejecting",
            target_pk,
            typeclass_path,
            f"{target_typeclass.__module__}.{target_typeclass.__name__}",
        )
        return False

    # Local delivery: target is on this shard or is a global row owned
    # by every shard. Skip the bus and call .msg directly so consumers
    # get one code path and zero polling latency for in-shard targets.
    current = get_shard_id()
    if target_shard == current or target_shard == "*":
        target = ObjectDB.objects.get(pk=target_pk)
        target.msg(**kwargs)
        return True

    # Remote delivery: queue an obj_msg row for the target's shard.
    send_message(
        kind="obj_msg",
        payload={"pk": target_pk, "kwargs": kwargs},
        to_shard=target_shard,
    )
    return True


def send_cross_shard_room_message(
    room_pk, text, exclude_pks=None, from_obj_pk=None
):
    """Deliver ``text`` to every obj in a room's contents, locally or via bus.

    Sender-side helper that wraps the ``room_msg`` bus primitive with
    the same ergonomic shape as :func:`send_cross_shard_message`:

    - **Local-vs-remote dispatch.** If the room row is on this shard
      (or is global, ``shard_id == "*"``), the helper looks up the
      instance and calls ``room.msg_contents(text, exclude=...,
      from_obj=...)`` directly — no bus hop, no polling latency.
      Otherwise it inserts a ``room_msg`` bus row addressed to the
      room's owning shard.
    - **Single DB read for the primary target.** One
      ``values_list`` query reads the room's ``shard_id`` — no
      ``from_db`` instantiation on the lookup itself.
    - **Optional pks are hints, not targets.** ``exclude_pks`` and
      ``from_obj_pk`` are dropped silently if they don't resolve
      locally (or trip the chokepoint) — same behaviour as the
      receiver-side ``_handle_room_msg``. Losing an exclude hint is
      strictly better than failing the whole broadcast.

    Args:
        room_pk: the target room's ``ObjectDB`` pk.
        text: the rendered, attribution-included string to broadcast.
            Sender composes; the helper and the bus are dumb. Must be
            JSON-serialisable on the remote path (the
            ``Message.payload`` JSONField raises on non-serialisable
            content at send time).
        exclude_pks: optional iterable of pks to skip during fanout.
            Pks resolved locally for the local path; serialised for
            the remote path and resolved on the receiver. Unresolved
            pks (deleted, on yet another shard) are silently dropped.
        from_obj_pk: optional pk of the sender, looked up locally
            (or on the receiver) and passed as ``from_obj`` to
            ``msg_contents``. Skipped silently if it doesn't resolve.

    Returns:
        ``True`` if the broadcast was dispatched locally or queued
        for remote delivery. ``False`` if the helper rejected the
        call because the room row doesn't exist (warning logged).
    """
    from evennia.objects.models import ObjectDB

    from .config import get_shard_id
    from .messagebus import send_message
    from .tenancy import shard_context

    # Escape the multitenant auto-filter for the visibility lookup —
    # see send_cross_shard_message above for the rationale.
    with shard_context(None):
        rows = list(
            ObjectDB.objects.filter(pk=room_pk)
            .values_list("shard_id", flat=True)[:1]
        )
    if not rows:
        log.warning(
            "send_cross_shard_room_message: target room pk=%r does "
            "not exist; rejecting",
            room_pk,
        )
        return False
    target_shard = rows[0]

    current = get_shard_id()
    if target_shard == current or target_shard == "*":
        # Local fast-path: room is here, msg_contents is a synchronous
        # call. Resolve hint pks locally; silently drop any that can't
        # be loaded (matches receiver-side behaviour).
        room = ObjectDB.objects.get(pk=room_pk)
        exclude = []
        for ex_pk in exclude_pks or ():
            try:
                exclude.append(ObjectDB.objects.get(pk=ex_pk))
            except ObjectDB.DoesNotExist:
                continue
        from_obj = None
        if from_obj_pk is not None:
            try:
                from_obj = ObjectDB.objects.get(pk=from_obj_pk)
            except ObjectDB.DoesNotExist:
                from_obj = None
        room.msg_contents(text, exclude=exclude, from_obj=from_obj)
        return True

    # Remote delivery: queue a room_msg row for the room's shard.
    payload = {"room_pk": room_pk, "text": text}
    if exclude_pks:
        payload["exclude_pks"] = list(exclude_pks)
    if from_obj_pk is not None:
        payload["from_obj_pk"] = from_obj_pk
    send_message(
        kind="room_msg",
        payload=payload,
        to_shard=target_shard,
    )
    return True
