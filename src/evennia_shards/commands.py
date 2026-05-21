# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware command overrides and library-shipped admin commands.

Two flavours of commands live here:

- **Replacements** — ``ShardAwareCmdIC`` / ``ShardAwareCmdOOC`` swap
  in for Evennia's built-in commands of the same name. Injected via
  module-attribute monkey-patch in ``AppConfig.ready()``; the
  ``AccountCmdSet`` picks them up on cmdset rebuild.

- **Permanent admin commands** — ``CmdShardCheck`` and
  ``CmdCrossShardDig`` are admin commands shipped by the library for
  sharded deployments (Shard Management category, Developer lock).
  Injected by patching ``CharacterCmdSet.at_cmdset_creation`` in
  ``AppConfig.ready()``. Cross-shard movement is exposed through the
  shard-aware ``@teleport`` override (see ``teleport.py``) rather than
  a dedicated admin command — same primitive (``cross_shard_move``),
  one in-game verb (``@tel``) that transparently dispatches local vs
  cross-shard.
"""

from evennia.commands.command import Command as BaseCommand
from evennia.commands.default.account import CmdIC, CmdOOC
from evennia.utils import logger, search, utils

from .config import ROLE_SHARD, get_role, get_router_shard_id, get_router_url
from .handoff import _redirect_to_character_shard
from .tickets import create_ticket


class ShardAwareCmdIC(CmdIC):
    """Shard-aware override of Evennia's ``ic`` command.

    - **Router**: resolves the character, sets ``_last_puppet``, creates
      a ticket, and redirects the client to the character's shard.
    - **Shard**: tells the player to return to the router.
    - **Monolith**: never injected (original ``CmdIC`` stays).
    """

    def func(self):
        role = get_role()

        if role == ROLE_SHARD:
            self.msg("Leave this character before trying to enter another one.")
            return

        # --- Router path: resolve character, then ticket + redirect ---

        new_character = self._resolve_character()
        if new_character is None:
            return  # error messages already sent

        # Router process may hold a stale row in the idmapper if another
        # process moved this character (e.g. cross_shard_move updates
        # shard_id and db_location_id together). Flush from the idmapper
        # cache first — Evennia's SharedMemoryModelBase.__call__ returns
        # the cached instance from from_db(), so refresh_from_db() is a
        # no-op unless the cache entry is evicted beforehand.
        new_character.flush_from_cache(force=True)
        new_character.refresh_from_db()

        shard_id = new_character.shard_id
        if not shard_id or shard_id == "*":
            self.msg("That character has no shard assignment.")
            return

        # Clear the OOC-menu marker — the player is going IC. This is
        # the one IC entry point where the flag is plausibly True at
        # the moment of redirect (player at OOC menu, typing @ic). The
        # write happens on the router's Server process, the same
        # process that reads the flag in shard_aware_at_post_login on
        # subsequent connections. No cross-process write.
        self.account.db._shards_at_ooc_menu = False

        _redirect_to_character_shard(self.account, self.session, new_character)
        self.msg(f"Redirecting to {shard_id}...")

    def _resolve_character(self):
        """Resolve the target character from command args.

        Replicates the resolution logic from ``CmdIC.func()`` without
        calling ``puppet_object()``.  Returns the character or ``None``
        (with error messages already sent to the caller).
        """
        account = self.account
        session = self.session

        character_candidates = []

        if not self.args:
            character_candidates = (
                [account.db._last_puppet] if account.db._last_puppet else []
            )
            if not character_candidates:
                self.msg("Usage: ic <character>")
                return None
        else:
            if playables := account.characters:
                character_candidates.extend(
                    utils.make_iter(
                        account.search(
                            self.args,
                            candidates=playables,
                            search_object=True,
                            quiet=True,
                        )
                    )
                )

            if account.locks.check_lockstring(account, "perm(Builder)"):
                if session.puppet:
                    character_candidates = [
                        char
                        for char in session.puppet.search(self.args, quiet=True)
                        if char.access(account, "puppet")
                    ]
                if not character_candidates:
                    character_candidates.extend(
                        [
                            char
                            for char in search.object_search(self.args)
                            if char.access(account, "puppet")
                        ]
                    )

        if not character_candidates:
            self.msg("That is not a valid character choice.")
            return None
        if len(character_candidates) > 1:
            self.msg(
                "Multiple targets with the same name:\n %s"
                % ", ".join(
                    "%s(#%s)" % (obj.key, obj.id) for obj in character_candidates
                )
            )
            return None

        return character_candidates[0]


class ShardAwareCmdOOC(CmdOOC):
    """Shard-aware override of Evennia's ``ooc`` command.

    - **Shard**: creates a ticket and redirects the client to the router.
      Always redirects — even if no puppet (error state), because a
      player should never be OOC on a shard.
    - **Router**: never injected (original ``CmdOOC`` stays).
    - **Monolith**: never injected (original ``CmdOOC`` stays).

    No explicit ``unpuppet_object()`` call is needed here. The redirect
    triggers a full page navigation (``window.location.href``), which
    closes the WebSocket connection. Evennia's disconnect handler
    (``sessionhandler.disconnect()`` → ``account.unpuppet_object()``)
    automatically releases the character on the shard when the
    connection drops.
    """

    def func(self):
        account = self.account
        session = self.session

        # Resolve the best character_id for the ticket.
        old_char = account.get_puppet(session)
        if old_char:
            character_id = old_char.id
        elif account.db._last_puppet:
            character_id = account.db._last_puppet.id
        else:
            # Truly broken state — no puppet and no _last_puppet.
            # Log a warning and use 0 as a sentinel; the router
            # won't use character_id for OOC tickets anyway.
            character_id = 0
            logger.log_warn(
                f"OOC redirect with no puppet and no _last_puppet "
                f"(Account: {account}, IP: {session.address})."
            )

        token = create_ticket(
            account.id, character_id, get_router_shard_id(),
            client_ip=session.address,
        )
        url = f"{get_router_url()}?ticket={token}"
        session.msg(shard_redirect=[[url], {}])
        self.msg("Redirecting to router...")

        logger.log_sec(
            f"OOC redirect: (Caller: {account}, Character: {character_id}, "
            f"IP: {session.address})."
        )


# =============================================================================
# Permanent admin commands — shipped with the library, auto-installed into
# CharacterCmdSet by AppConfig.ready() when get_role() != ROLE_MONOLITH.
# Available as superuser commands on every sharded deployment.
# =============================================================================


class CmdShardCheck(BaseCommand):
    """
    Inspect an object's underlying ObjectDB row for the shard_id column.

    Usage:
      @shard_check <target>

    Reports whether the column exists on the model, and its value if so.
    Tries the ORM first; falls back to a raw SQL probe so we still get a
    result if the column is in the database but the Python model isn't
    aware of it.
    """

    key = "@shard_check"
    locks = "cmd:perm(Developer)"
    help_category = "Shard Management"

    def func(self):
        if not self.args.strip():
            self.caller.msg("Usage: @shard_check <target>")
            return

        target = self.caller.search(self.args.strip())
        if not target:
            return

        from evennia.objects.models import ObjectDB

        # ORM-level check: is shard_id a known field on ObjectDB?
        field_names = {f.name for f in ObjectDB._meta.get_fields()}
        if "shard_id" in field_names:
            row = ObjectDB.objects.get(id=target.id)
            self.caller.msg(
                f"ORM: ObjectDB row #{target.id} has shard_id field; "
                f"value = {row.shard_id!r}"
            )
        else:
            self.caller.msg(
                f"ORM: ObjectDB has no shard_id field "
                f"(EvenniaShardsConfig.ready() may not have run)."
            )

        # Database-level check: does the column exist in the table?
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute(
                "SELECT shard_id FROM objects_objectdb WHERE id = %s",
                [target.id],
            )
            try:
                row = cur.fetchone()
                self.caller.msg(
                    f"DB:  raw SELECT shard_id WHERE id={target.id} returned {row!r}"
                )
            except Exception as e:  # noqa: BLE001
                self.caller.msg(f"DB:  raw SELECT failed: {e!r}")


class CmdCrossShardDig(BaseCommand):
    """
    Dig a room on a target shard.

    Usage:
      cross_shard_dig <shard_id> <room_name>

    Creates a new DefaultRoom and stamps it with the target shard's
    shard_id, so the row is owned by that shard from creation. Used
    to bootstrap a starting room on a freshly-added shard (you can't
    log in to the new shard without one) and as a general utility for
    cross-shard world-building from an existing shard.

    The room has no location (it's a root room — same as Limbo).
    Reports the new room's dbref so it can be used as a target for
    cross_shard_move or referenced from settings.
    """

    key = "cross_shard_dig"
    locks = "cmd:perm(Developer)"
    help_category = "Shard Management"

    def func(self):
        from evennia.utils.create import create_object

        from .config import get_shard_url
        from .tenancy import shard_context

        args = self.args.strip().split(None, 1)
        if len(args) < 2:
            self.caller.msg("Usage: cross_shard_dig <shard_id> <room_name>")
            return
        target_shard, room_name = args[0], args[1]

        # Validate target_shard is configured.
        try:
            get_shard_url(target_shard)
        except (KeyError, ValueError):
            self.caller.msg(
                f"|rTarget shard {target_shard!r} is not configured "
                f"(not in SHARD_URLS).|n"
            )
            return

        # Create the room *inside* the target shard's context so the
        # multitenant auto-stamp lands ``shard_id=target_shard`` on
        # insert. No re-stamp needed afterward — the bypass primitive
        # from the chokepoint era is gone, and under the tenant-column
        # immutability check we can't re-stamp via assignment anyway.
        with shard_context(target_shard):
            room = create_object(
                "evennia.objects.objects.DefaultRoom",
                key=room_name,
            )

        self.caller.msg(
            f"|wDug |c{room_name}|n on shard {target_shard!r}: "
            f"|w#{room.pk}|n"
        )


