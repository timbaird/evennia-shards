# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware override of Evennia's ``@teleport`` / ``@tel``.

This module is imported only *after* ``evennia._init()`` has run — see
``apps.py``'s ``_shards_wrapped_init``. The reason is the import chain:
``evennia.commands.default.building`` pulls in ``prototypes/menus``
which pulls in ``evmenu``, and ``evmenu`` defines a class that
subclasses ``evennia.Command`` at module load time. ``evennia.Command``
is a lazy export populated by ``evennia._init()``; before that runs,
the name resolves to ``None``, and the class definition raises
``TypeError: NoneType takes no arguments``. The library's ``apps.py``
defers any code that depends on this chain into a wrapper around
``evennia._init`` so the imports happen at the right moment.

Keeping the class in its own module lets ``commands.py`` (which is
imported at ``AppConfig.ready()`` time, before ``evennia._init``)
stay free of the bomb-triggering ``building`` import.
"""

from evennia.commands.cmdhandler import InterruptCommand
from evennia.commands.default.building import CmdTeleport

from .search import shard_aware_global_search


def _format_multiple(name: str, candidates: list) -> str:
    """Render a disambiguation prompt from a multi-match search result.

    ``candidates`` is the list of ``(pk, shard_id, db_key)`` triples that
    :class:`~evennia_shards.search.ShardSearchResult` carries when more
    than one row matched. Output shape:
    ``"Multiple matches for 'tavern': #5 (Tavern, shard0), #12 (Tavern,
    shard1) — specify by dbref."`` The caller has no way to pick between
    rows without a dbref (foreign-shard rows aren't loadable here), so
    the prompt steers them straight at ``#<pk>``.
    """
    rendered = ", ".join(
        f"#{pk} ({key}, {shard})" for pk, shard, key in candidates
    )
    return (
        f"Multiple matches for {name!r}: {rendered} — specify by dbref."
    )


class ShardAwareCmdTeleport(CmdTeleport):
    """Shard-aware override of Evennia's ``@teleport`` / ``@tel``.

    Installed via module-attribute swap on
    ``evennia.commands.default.building.CmdTeleport`` (see
    ``apps.py``'s ``_shards_wrapped_init``). The standard
    ``CharacterCmdSet`` references ``building.CmdTeleport`` via module
    attribute at cmdset-rebuild time, so the swap is picked up
    transparently.

    Design shape:

    - ``parse()`` mirrors vanilla 1:1, just substituting the three
      ``caller.search(global_search=True)`` calls with
      :func:`shard_aware_global_search` from the helper module. The
      result is stashed onto ``self`` in the same slots vanilla uses
      (``self.obj_to_teleport`` / ``self.destination``) — with the
      instance present when the match is local, ``None`` when foreign.
      Extra fields (``self.obj_pk`` / ``self.obj_shard`` /
      ``self.dest_pk`` / ``self.dest_shard``) carry the cross-shard
      routing data ``func`` needs when delegation isn't safe.

    - ``func()`` dispatches into three branches:
        1. ``/tonone`` — if obj is local, delegate to vanilla; if
           foreign, refuse with the cross_shard_move pointer.
        2. Both obj and destination are local instances — delegate to
           vanilla. All vanilla complexity (lock checks, equality
           checks, ``/loc`` / ``/intoexit`` / ``/quiet`` modifiers,
           the move itself) stays in vanilla; we don't reimplement it.
        3. At least one of obj / destination is cross-shard — for now
           refuse with a "not yet implemented" message. Subsequent
           commits implement these branches one at a time.

    Per-branch implementation strategy lives in DESIGN/ once the
    branches are filled in; the scaffold here just proves the dispatch
    structure is sound.
    """

    def parse(self):
        # Arg split via CmdTeleport's parent (MuxCommand, in the
        # default Evennia stack). Skips vanilla CmdTeleport.parse's
        # body, which would do ``caller.search(global_search=True)``
        # calls — those route through the auto-filtered manager and
        # would silently miss cross-shard matches. The lhs/rhs
        # splitting (via ``rhs_split = ("=", " to ")``) lives in
        # MuxCommand.parse, not in Command.parse — so we have to reach
        # the immediate parent, not the bare base.
        super(CmdTeleport, self).parse()

        # Vanilla's default state (slots that func() reads).
        self.obj_to_teleport = self.caller
        self.destination = None

        # Extra routing fields (None when not applicable / not found).
        # _key fields are populated regardless of locality so the
        # cross-shard branch can produce a readable success message
        # without needing a foreign-row instance.
        self.obj_pk = None
        self.obj_shard = None
        self.obj_key = None
        self.dest_pk = None
        self.dest_shard = None
        self.dest_key = None

        # Mirror vanilla's three-call structure with shard-aware lookups.
        if self.rhs:
            obj_result = shard_aware_global_search(self.caller, self.lhs)
            if obj_result.state == "not_found":
                self.msg("Did not find object to teleport.")
                raise InterruptCommand
            if obj_result.state == "multiple":
                self.msg(_format_multiple(self.lhs, obj_result.candidates))
                raise InterruptCommand
            # state == "found"
            self.obj_to_teleport = obj_result.obj  # None if cross-shard
            self.obj_pk = obj_result.pk
            self.obj_shard = obj_result.shard_id
            self.obj_key = obj_result.db_key

            dest_result = shard_aware_global_search(self.caller, self.rhs)
            if dest_result.state == "multiple":
                self.msg(_format_multiple(self.rhs, dest_result.candidates))
                raise InterruptCommand
            if dest_result.state == "not_found":
                # Vanilla's "Destination not found." message. The
                # original comment here said vanilla's func() would
                # emit it, but our func() dispatch routes around
                # vanilla when ``destination is None`` (branch 2
                # requires both targets present), so we have to
                # surface it ourselves.
                self.msg("Destination not found.")
                raise InterruptCommand
            # state == "found"
            self.destination = dest_result.obj  # None if cross-shard
            self.dest_pk = dest_result.pk
            self.dest_shard = dest_result.shard_id
            self.dest_key = dest_result.db_key

        elif self.lhs:
            dest_result = shard_aware_global_search(self.caller, self.lhs)
            if dest_result.state == "multiple":
                self.msg(_format_multiple(self.lhs, dest_result.candidates))
                raise InterruptCommand
            if dest_result.state == "not_found":
                self.msg("Destination not found.")
                raise InterruptCommand
            # state == "found"
            self.destination = dest_result.obj  # None if cross-shard
            self.dest_pk = dest_result.pk
            self.dest_shard = dest_result.shard_id
            self.dest_key = dest_result.db_key

    def func(self):
        # Branch 1 — /tonone. Vanilla's body handles obj at /tonone time;
        # we only need to guard against a foreign obj (which we can't
        # None-ify from this process).
        if "tonone" in self.switches:
            if self.obj_to_teleport is None:
                # Foreign obj — vanilla would crash on the next
                # attribute access. Refuse with a pointer to the
                # workflow that brings the obj into reach.
                self.caller.msg(
                    f"|rCross-shard /tonone not yet implemented.|n "
                    f"Object {self.lhs!r} is on shard "
                    f"{self.obj_shard!r}; teleport yourself to that "
                    f"shard first (|w@tel <room_on_{self.obj_shard}>|n) "
                    f"and run /tonone there."
                )
                return
            super().func()
            return

        # Branch 2 — both targets local. Vanilla handles everything:
        # lock checks, equality checks, /loc, /intoexit, /quiet, the
        # actual move, the announce messages, the failure paths.
        if self.obj_to_teleport is not None and self.destination is not None:
            super().func()
            return

        # Branch 3 — at least one target is cross-shard. Each sub-case
        # gets its own implementation in a subsequent commit; for now,
        # surface a clear "not yet implemented" so the dispatch
        # structure can be smoke-tested.

        if self.obj_to_teleport is None:
            # Foreign obj. Under our current scope this is refused —
            # the admin should switch shards before trying to move
            # something local to the destination shard.
            self.caller.msg(
                f"|rCross-shard teleport of a foreign object not "
                f"yet implemented.|n Object {self.lhs!r} is on shard "
                f"{self.obj_shard!r}; teleport yourself to that shard "
                f"first (|w@tel <room_on_{self.obj_shard}>|n) and "
                f"teleport the object locally from there."
            )
            return

        # obj is local, destination is cross-shard. The "I want to
        # teleport (myself or a local object) to a room on another
        # shard" path — wraps the library's cross_shard_move
        # primitive.
        #
        # /loc and /intoexit modifiers on a cross-shard destination
        # are refused for now (separate future work):
        #
        # - /loc means "go to destination.location" instead of to
        #   destination itself. Resolving that requires reading the
        #   destination row's db_location_id without loading the
        #   instance (another values_list query) and then re-routing
        #   the move against that pk. Workable but not in this first
        #   cut.
        # - /intoexit means "land inside the exit object". The exit
        #   row lives on the foreign shard and the move target would
        #   be the exit's own pk rather than its destination. Rare
        #   admin scenario, not in this first cut.
        if "loc" in self.switches:
            self.caller.msg(
                f"|rCross-shard /loc not yet supported.|n The "
                f"destination is on shard {self.dest_shard!r}; "
                f"resolve its location pk first and teleport directly "
                f"to that pk (|w@tel #<location_pk>|n)."
            )
            return
        if "intoexit" in self.switches:
            self.caller.msg(
                f"|rCross-shard /intoexit not yet supported.|n The "
                f"exit is on shard {self.dest_shard!r}; teleport to "
                f"its dbref directly (|w@tel #<exit_pk>|n) — without "
                f"/intoexit, @tel into an exit object lands inside "
                f"the exit on the destination shard."
            )
            return

        # Mirror vanilla's "already at <destination>" short-circuit
        # (CmdTeleport.func line 3917). Vanilla compares the obj's
        # location instance against the destination instance; we don't
        # have the destination instance (it's foreign), but pks are
        # globally unique under the single-Postgres model, so a pk
        # match on db_location_id is the same statement. Saves a no-op
        # bus round-trip and matches the user-visible message.
        if self.obj_to_teleport.db_location_id == self.dest_pk:
            dest_display = self.dest_key or f"#{self.dest_pk}"
            self.caller.msg(
                f"{self.obj_to_teleport} is already at {dest_display}."
            )
            return

        # Mirror vanilla's obj-side teleport-lock check
        # (CmdTeleport.func line 3922). Obj is local in this branch,
        # so we have the instance — Admin bypass or explicit
        # `teleport` lock against the caller. Skipping this would
        # leave a security inconsistency: local @tel honours the
        # lock (via super().func()), cross-shard @tel would not.
        # Destination-side `teleport_here` lock is NOT checked here —
        # the destination row lives on another shard and there's no
        # local way to evaluate its lock against the obj; that's a
        # bus round-trip's worth of work, deferred.
        if not (
            self.caller.permissions.check("Admin")
            or self.obj_to_teleport.access(self.caller, "teleport")
        ):
            self.caller.msg(
                f"{self.obj_to_teleport} 'teleport'-lock blocks you "
                f"from teleporting it anywhere."
            )
            return

        # Behaviour intentionally skipped in this first cut (worth
        # documenting so future readers don't think it's an oversight):
        #
        # - Vanilla's "teleport_here" lock check on the destination
        #   (CmdTeleport.func lines 3928-3935). The destination row
        #   lives on the foreign shard, so we can't evaluate its lock
        #   against obj here without a bus round-trip. Deferred until
        #   that primitive lands. The obj-side "teleport" lock is
        #   checked above; only the destination-side check is missing.
        from .handoff import cross_shard_move
        from .messaging import send_cross_shard_room_message

        # Source-side announce — vanilla's announce_move_from. Source
        # room is local (obj is local in this branch); fire a regular
        # msg_contents before the move, while the obj is still in the
        # room's contents. /quiet suppresses this — mirroring vanilla
        # at move_to(quiet=...). The obj itself is excluded so it
        # doesn't see the leave message about itself.
        dest_display = self.dest_key or f"#{self.dest_pk}"
        source = self.obj_to_teleport.location
        if "quiet" not in self.switches and source is not None:
            source.msg_contents(
                f"{self.obj_to_teleport.key} is leaving {source.key}, "
                f"heading for {dest_display}.",
                exclude=[self.obj_to_teleport],
            )

        try:
            result = cross_shard_move(
                self.obj_to_teleport, self.dest_shard, self.dest_pk
            )
        except Exception as exc:
            self.caller.msg(
                f"|rCross-shard teleport failed:|n {exc}"
            )
            return

        # Destination-side announce — vanilla's announce_move_to. The
        # destination room is on the foreign shard, so we route via
        # the room_msg bus kind. Sender composes the text (source name
        # included as attribution), receiver looks up the room and
        # fans out via msg_contents. /quiet suppresses both this and
        # the source-side announce, matching vanilla.
        if "quiet" not in self.switches:
            source_name = source.key if source is not None else "elsewhere"
            send_cross_shard_room_message(
                self.dest_pk,
                f"{self.obj_to_teleport.key} arrives from {source_name}.",
                exclude_pks=[self.obj_to_teleport.pk],
            )

        # Caller always sees a confirmation — vanilla CmdTeleport.func
        # at building.py:3949 emits "Teleported X -> Y." unconditionally;
        # the /quiet switch only suppresses the source/destination room
        # announces (the two msg_contents calls above), not this one.
        who = (
            "you"
            if self.obj_to_teleport == self.caller
            else self.obj_to_teleport.key
        )
        self.caller.msg(
            f"Teleported {who} -> {dest_display} on shard "
            f"{self.dest_shard!r}. (objects_moved={result.objects_moved}, "
            f"sessions_redirected={result.sessions_redirected}, "
            f"failures={len(result.failures)})"
        )
