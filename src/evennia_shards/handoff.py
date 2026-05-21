# SPDX-License-Identifier: BSD-3-Clause
"""Cross-shard handoff primitives.

The library's cross-shard handoff mechanism — operations that move a
row's identity (``shard_id``, location) from one shard process to
another, evict it from the source's idmapper, and redirect any
puppeting sessions to the destination shard.

Houses two public primitives:

- :func:`cross_shard_move` — composes the full handoff for an
  ``ObjectDB``-derived row and its full recursive inventory. Mutates
  ``shard_id`` via ``QuerySet.update`` (bypassing ``save()``, which the
  ``shard_id``-immutability check on ``__setattr__`` would otherwise
  refuse).
- :func:`_redirect_to_character_shard` — the per-session redirect
  used by the ``ic`` command and the ``at_post_login`` override.
"""

from collections import namedtuple

from django.db import transaction
from evennia.utils import logger

from .config import get_shard_id, get_shard_url
from .tenancy import shard_context
from .tickets import create_ticket


def _collect_all_contents(root_pk):
    """Return pks of every descendant of *root_pk* (breadth-first, exclusive).

    Uses ``values_list`` queries to avoid ``from_db`` / instance
    construction — safe to call for objects on any shard.

    Evennia prevents location loops, so cycle detection is unnecessary.
    """
    from evennia.objects.models import ObjectDB

    all_pks = []
    parents = [root_pk]
    while parents:
        children = list(
            ObjectDB.objects.filter(db_location_id__in=parents)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        all_pks.extend(children)
        parents = children
    return all_pks


def _evict_pks_from_idmapper(pks):
    """Evict a list of pks from the ObjectDB idmapper cache.

    Uses ``flush_from_cache(force=True)`` on instances already in the
    cache (Evennia's API), with a direct dict pop as fallback for pks
    not currently cached.  Both are equivalent today — ``force=True``
    skips ``at_idmapper_flush`` and is just a dict pop — but using the
    method is more future-proof.
    """
    from evennia.objects.models import ObjectDB

    cache = ObjectDB.__dbclass__.__instance_cache__
    for pk in pks:
        cached = cache.get(pk)
        if cached is not None:
            cached.flush_from_cache(force=True)
        else:
            cache.pop(pk, None)


MoveResult = namedtuple(
    "MoveResult",
    ["objects_moved", "sessions_redirected", "failures"],
)
"""Outcome of a :func:`cross_shard_move` call.

- ``objects_moved`` (int): number of rows whose ``shard_id`` was
  updated to the new shard (the moved obj + its recursive inventory).
- ``sessions_redirected`` (int): number of puppeting sessions for
  which a ticket was created and a ``shard_redirect`` OOB was sent.
- ``failures`` (list[tuple]): per-session failure entries —
  ``(session, exception)`` pairs for redirects that raised. The move
  itself committed; these are post-commit recoverable failures
  (network OOB couldn't be sent, etc.). Player can reconnect.
"""


def cross_shard_move(obj, target_shard, target_location_pk):
    """Move ``obj`` and its inventory to ``target_shard``, into the row at ``target_location_pk``.

    Operates on any ``ObjectDB``-derived row — character, item, mob,
    container, NPC, exit, etc. Moves the row itself plus all recursive
    contents (inventory items, bags-within-bags, characters carried by
    other characters, etc.).  Contents' ``db_location_id`` is unchanged
    — parent pk doesn't change across shards.  Contents with
    ``shard_id == "*"`` (globals) are left alone.

    Steps:

    1. Validate ``target_shard`` is in ``SHARD_URLS``.
    2. Validate ``target_location_pk`` exists and is on
       ``target_shard`` (or is the global ``"*"`` sentinel).
    3. Snapshot the sessions currently puppeting ``obj`` (with their
       accounts) before anything changes.
    3b. Collect all descendant pks (recursive inventory).
    4. Inside ``transaction.atomic()``: ``qs.update`` ``obj``'s row
       to set ``shard_id`` and ``db_location_id`` in a single SQL
       UPDATE, then evict from this process's idmapper. Using
       ``qs.update`` (not ``obj.save()``) is what makes the
       ``shard_id`` mutation possible — the multitenant
       ``__setattr__`` would flag the assignment as an attempt to
       mutate the tenant column and the next ``save()`` would raise
       ``NotSupportedError``. ``qs.update`` goes directly to SQL,
       bypassing the instance-level immutability check.
    5. Bulk-update contents' ``shard_id`` to ``target_shard`` and
       evict them from the idmapper.
    6. Pre-emptive session detach. Clears ``session.puppet`` and
       ``session.puid`` for each snapshotted session, and removes
       the ``"puppeted"`` tag from ``obj``. Avoids calling Evennia's
       full ``unpuppet_object()`` because its ``at_post_unpuppet``
       hook dereferences ``obj.location`` — a FK to the row on
       ``target_shard``, which the multitenant auto-filter now
       excludes (returns ``None``, which the hook chain doesn't
       expect).
    7. For each snapshotted session: create a ticket and send
       ``shard_redirect`` OOB. Per-session failures are captured in
       the returned :class:`MoveResult` and do not roll back the
       move.
    8. Send a ``flush_from_cache`` bus message to ``target_shard``
       with the destination row's pk. ``qs.update`` does not fire
       Evennia's post-save signal, so the destination shard's
       in-process ``contents_cache`` for the target room doesn't
       see the arriving obj. The bus message asks the destination
       to evict, so its next access rebuilds contents fresh from
       the DB. Skipped when target equals current shard. Send
       failure is logged but does not roll back the move.

    Args:
        obj: any ``ObjectDB``-derived instance on this shard, to be
            moved to ``target_shard``. Characters are the most common
            case (their puppeting sessions get redirected), but
            unpuppeted objects (items, mobs, etc.) work identically —
            sessions_redirected is simply zero.
        target_shard: the destination shard's ``SHARD_ID``. Must be a
            key in ``SHARD_URLS``.
        target_location_pk: pk of the destination row on the target
            shard (typically a room, but the primitive does not enforce
            that — see "Target typeclass not validated" below). Must
            exist; must have ``shard_id == target_shard`` or
            ``shard_id == "*"``.

    Target typeclass not validated. The primitive checks only that the
    target row exists and is on the target shard. It does not check
    that the target is a Room (vs. a Character, Item, Exit, etc.).
    Considered and rejected: encoding "valid move targets" as a
    library-level rule would put a game concept into the primitive,
    against load-bearing principle 3 (the library does not own game
    concepts). Some games legitimately move characters into vehicles,
    mounts, or containers; the choice of valid destinations is the
    consumer's. Consumer-side typeclass code (e.g. a ``CrossShardExit``
    or a teleport command) should validate the target's typeclass
    before calling this primitive.

    Returns:
        :class:`MoveResult` with counts and per-session failures.

    Raises:
        ValueError: if ``target_shard`` isn't configured, if
            ``target_location_pk`` doesn't exist, or if it's on a
            shard other than ``target_shard`` (and not global).
    """
    from evennia.objects.models import ObjectDB

    # 1. Validate target shard.
    try:
        get_shard_url(target_shard)
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"cross_shard_move: target_shard {target_shard!r} is not "
            f"configured (not present in SHARD_URLS)"
        ) from exc

    # 2. Validate target location exists and is on the target shard.
    # The target row lives on ``target_shard``, so the default
    # auto-filter (scope: ``[current_shard, "*"]``) would exclude it
    # from the queryset entirely — ``values_list`` bypasses instance
    # materialisation but not the SQL ``WHERE`` clause. Drop into the
    # unscoped context briefly to read the foreign row's ``shard_id``.
    with shard_context(None):
        target_rows = list(
            ObjectDB.objects.filter(pk=target_location_pk)
            .values_list("shard_id", flat=True)[:1]
        )
    if not target_rows:
        raise ValueError(
            f"cross_shard_move: target_location_pk {target_location_pk!r} "
            f"does not exist"
        )
    location_shard = target_rows[0]
    if location_shard != target_shard and location_shard != "*":
        raise ValueError(
            f"cross_shard_move: target_location_pk {target_location_pk!r} "
            f"is on shard {location_shard!r}, not {target_shard!r}"
        )

    # 3. Snapshot sessions before anything changes. After the session
    # detach (step 6) the puppet references will be cleared, and we
    # need the (session, account) pairs for redirect in step 7.
    sessions_to_redirect = [
        (session, session.account) for session in obj.sessions.all()
    ]

    # 3b. Collect all descendant pks (recursive inventory).
    content_pks = _collect_all_contents(obj.pk)

    # 4 + 5. Atomic DB writes + idmapper eviction.
    #
    # ``qs.update`` bypasses ``save()`` entirely — no ``__setattr__``,
    # no ``pre_save`` signal, no ``_do_update`` immutability check.
    # That's the whole reason the rewrite uses it: the multitenant
    # tenant-column-immutability rule would refuse a ``save()`` that
    # changes ``shard_id``.
    #
    # The auto-filter on the update queryset is ``shard_id IN (current,
    # '*')`` — our source rows match (they're owned by current shard),
    # so the update affects them. The explicit ``shard_id=current``
    # filter narrows further to skip ``"*"`` globals.
    #
    # ``flush_from_cache`` lives inside ``transaction.atomic`` so an
    # eviction failure rolls back the DB write. On exception the
    # except branch evicts defensively in case the partial state left
    # stale entries in the idmapper.
    current_shard = get_shard_id()
    contents_updated = 0
    try:
        with transaction.atomic():
            objs_updated = ObjectDB.objects.filter(
                pk=obj.pk, shard_id=current_shard,
            ).update(
                shard_id=target_shard,
                db_location_id=target_location_pk,
            )
            if objs_updated != 1:
                # Either the row doesn't exist on this shard, or
                # another process moved it first. Either way the
                # caller's premise is invalid.
                raise ValueError(
                    f"cross_shard_move: obj pk={obj.pk!r} not found "
                    f"on current shard {current_shard!r}"
                )
            # Sync the in-memory state to match the DB. ``qs.update``
            # only touches the row; the Python instance still holds
            # the pre-move values. The redirect loop below reads
            # ``obj.shard_id`` for ticket destinations — without this
            # sync it would stamp the old shard. ``object.__setattr__``
            # bypasses our ``__setattr__`` wrapper so the in-memory
            # write doesn't flag ``_try_update_tenant`` (we don't want
            # any later ``save()`` to refuse on this instance).
            object.__setattr__(obj, "shard_id", target_shard)
            object.__setattr__(obj, "db_location_id", target_location_pk)
            obj.flush_from_cache(force=True)

            if content_pks:
                contents_updated = ObjectDB.objects.filter(
                    pk__in=content_pks, shard_id=current_shard,
                ).update(shard_id=target_shard)
                _evict_pks_from_idmapper(content_pks)

        # 6. Pre-emptive session detach.
        #
        # ``session.puppet = None`` so the disconnect handler's
        # ``obj = session.puppet; if obj:`` guard finds ``None`` and
        # returns immediately (no save, no zombie). ``session.puid =
        # None`` mirrors Evennia's own ``unpuppet_object`` cleanup.
        # The ``puppeted`` tag removal prevents ``server_maintenance``
        # from later tag-scanning and stumbling over a now-foreign row.
        #
        # We deliberately do not call Evennia's full
        # ``unpuppet_object()`` — its ``at_post_unpuppet`` hook
        # dereferences ``obj.location``, which is now a FK to a row
        # on the target shard and excluded by the multitenant
        # auto-filter (returns ``None``). The hook chain doesn't
        # expect ``None`` there.
        for session, _account in sessions_to_redirect:
            session.puppet = None
            session.puid = None
        try:
            obj.tags.remove("puppeted", category="account")
        except Exception as exc:
            logger.log_warn(
                f"cross_shard_move: puppeted tag removal failed for "
                f"obj pk={obj.pk}: {exc}"
            )
    except Exception:
        # Defensive eviction — idmapper pops aren't rolled back by
        # transaction.atomic, so purge stale entries on failure.
        try:
            obj.flush_from_cache(force=True)
        except Exception:
            pass
        try:
            _evict_pks_from_idmapper(content_pks)
        except Exception:
            pass
        raise

    # 7. Per-session redirect. Per-session failures are captured but
    # don't roll back the move; the player can ticket-auth on next
    # reconnect regardless.
    redirected = 0
    failures = []
    for session, account in sessions_to_redirect:
        try:
            _redirect_to_character_shard(account, session, obj)
            redirected += 1
        except Exception as exc:
            logger.log_warn(
                f"cross_shard_move: redirect failed for session "
                f"{session!r} on obj pk={obj.pk}: {exc}"
            )
            failures.append((session, exc))

    # 8. Tell the destination shard to drop its cached view of the
    # destination room. ``qs.update`` doesn't fire ``post_save``, so
    # the destination's in-process ``contents_cache`` for the target
    # room doesn't see the arriving obj. The bus message asks the
    # destination to evict, so its next ``room.contents`` access
    # rebuilds fresh from the DB.
    #
    # Skip when target == current shard (the bus refuses same-shard
    # sends). Send failure is logged but does not roll back the move.
    if target_shard != current_shard:
        try:
            from .messagebus import send_message
            send_message(
                kind="flush_from_cache",
                payload={"pks": [target_location_pk]},
                to_shard=target_shard,
            )
        except Exception as exc:
            logger.log_warn(
                f"cross_shard_move: post-move flush_from_cache send "
                f"failed for room pk={target_location_pk} on shard "
                f"{target_shard!r}: {exc}"
            )

    return MoveResult(
        objects_moved=1 + contents_updated,
        sessions_redirected=redirected,
        failures=failures,
    )


def _redirect_to_character_shard(account, session, character) -> str:
    """Set ``_last_puppet``, create a ticket, send ``shard_redirect`` OOB.

    Pure mechanism shared between every router-side entry point that
    needs to redirect a session to a character's owning shard:

    - ``ShardAwareCmdIC`` (manual ``ic <char>``)
    - ``shard_aware_at_post_login`` (login-time auto-puppet)
    - ``cross_shard_move`` (programmatic handoff)

    The caller is responsible for validating ``character.shard_id``
    before calling — this helper assumes a usable shard id (not
    ``None``, not the ``"*"`` sentinel, and resolvable via
    ``get_shard_url``).

    The OOB payload is a WebSocket URL with the ticket as a query
    parameter. The client closes its current WebSocket and opens a
    new one to this URL; the destination shard's ``onOpen`` validates
    the ticket and auto-logs-in the session.

    Returns the redirect URL (the WebSocket URL with ticket).
    """
    shard_id = character.shard_id

    # Set _last_puppet so the destination shard's auto-puppet picks up
    # the correct character after ticket auth.
    account.db._last_puppet = character

    # Note: this helper deliberately does NOT touch
    # ``account.db._shards_at_ooc_menu``. That flag is owned by the
    # router's Server process and is written there in two and only
    # two places: the router-Server's ``shard_aware_at_post_login``
    # (sets True when a fresh ticket auth lands at the router) and
    # ``ShardAwareCmdIC.func`` (sets False on @ic). Touching it from
    # this helper would create a cross-process write whenever this
    # function runs from a shard's Server (cross_shard_move)
    # — the router would not see it.

    token = create_ticket(
        account.id, character.id, shard_id, client_ip=session.address,
    )
    url = f"{get_shard_url(shard_id)}?ticket={token}"
    session.msg(shard_redirect=[[url], {}])

    logger.log_sec(
        f"Shard redirect: (Caller: {account}, Target: {character}, "
        f"Shard: {shard_id}, IP: {session.address})."
    )
    return url
