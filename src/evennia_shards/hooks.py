# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware Evennia hook overrides.

These replace Evennia hook methods when the library is active in a role
that needs them. Injected by ``AppConfig.ready()`` via monkey-patch on
the relevant Evennia class.

Two overrides:

- ``shard_aware_at_post_login`` — replaces ``DefaultAccount.at_post_login``
  on routers, intercepting Evennia's auto-puppet step and converting it
  to a ticket+redirect to the character's owning shard. See
  DESIGN/library-integration-risks.md for what to diff on Evennia upgrade.

- ``make_shard_at_post_login`` — factory that wraps Evennia's original
  ``at_post_login`` on shards, flushing stale idmapper/Attribute-cache
  entries for ``_last_puppet`` so that ``puppet_object`` works with the
  live DB state after a cross-shard move.
"""

from evennia.utils import logger

from .handoff import _redirect_to_character_shard
from .config import get_shard_url


def _is_redirectable_character(character) -> bool:
    """Return True iff ``character`` is usable as a redirect target.

    A character is redirectable when it's set, has a real ``shard_id``
    (not ``None`` and not the global ``"*"`` sentinel), and that shard
    has a configured URL in ``SHARD_URLS``.
    """
    if character is None:
        return False
    # Router process may hold a stale row in the idmapper if another
    # process moved this character (e.g. cross_shard_move updates
    # shard_id and db_location_id together). Flush from the idmapper
    # cache first — Evennia's SharedMemoryModelBase.__call__ returns
    # the cached instance from from_db(), so refresh_from_db() is a
    # no-op unless the cache entry is evicted beforehand.
    character.flush_from_cache(force=True)
    character.refresh_from_db()
    shard_id = getattr(character, "shard_id", None)
    if not shard_id or shard_id == "*":
        return False
    try:
        get_shard_url(shard_id)
    except (KeyError, ValueError):
        return False
    return True


def shard_aware_at_post_login(self, session=None, **kwargs):
    """Library override of ``DefaultAccount.at_post_login`` on routers.

    Reproduced from Evennia 6.0.0 ``DefaultAccount.at_post_login``. The
    prelude (protocol-flag load, ``logged_in`` OOB, connect-channel msg)
    runs verbatim. The original ``if AUTO_PUPPET_ON_LOGIN`` branch is
    replaced with redirect-or-fallback logic; the ``else`` branch (OOC
    character-select menu) is reproduced for the fallback path.

    Three outcomes:

    - ``_last_puppet`` is set with a usable ``shard_id`` → create a
      ticket and send ``shard_redirect``; the player's browser navigates
      to that shard.
    - ``_last_puppet`` is set but its ``shard_id`` is ``None``, ``"*"``,
      or not in ``SHARD_URLS`` → log a warning and render the OOC menu.
      Login does not fail.
    - ``_last_puppet`` is ``None`` → render the OOC menu silently
      (normal first login).

    See DESIGN/library-integration-risks.md for what to diff on Evennia
    upgrade.
    """
    # ── Reproduced Evennia DefaultAccount.at_post_login prelude ───────
    # Based on Evennia 6.0.0. Diff against upstream on upgrade.
    protocol_flags = self.attributes.get("_saved_protocol_flags", {})
    if session and protocol_flags:
        session.update_flags(**protocol_flags)

    if session:
        session.msg(logged_in={})

    self._send_to_connect_channel(f"|G{self.key} connected|n")
    # ── End reproduced prelude ────────────────────────────────────────

    # Honor the consumer's AUTO_PUPPET_ON_LOGIN setting. When the
    # consumer has disabled auto-puppet, vanilla Evennia's else-branch
    # always renders the OOC menu — none of the library's redirect
    # logic should apply. Short-circuit here so the override stays
    # vanilla-aligned for that case.
    from django.conf import settings as _django_settings
    if not getattr(_django_settings, "AUTO_PUPPET_ON_LOGIN", True):
        self.msg(self.at_look(target=self.characters, session=session), session=session)
        return

    # OOC/IC state machine — Server-only owner of account.db._shards_at_ooc_menu.
    #
    # Two and only two write-points on the Server:
    #   1. Here, when the session arrived via ticket auth on the router.
    #      The Portal sets protocol_flags["SHARDS_TICKET_AUTHED"]=True in
    #      onOpen priority #2; Evennia AMP-syncs the flag onto the
    #      Server's session; we read it here, and persist the OOC intent
    #      to the account-level Attribute. Same Server process writes
    #      the Attribute → reads from the same idmapper on subsequent
    #      logins → coherent.
    #   2. ShardAwareCmdIC.func, which clears the flag back to False
    #      before redirecting the player to a character's shard.
    #
    # No other code path touches the account flag.
    ticket_authed = bool(
        session and session.protocol_flags.get("SHARDS_TICKET_AUTHED")
    )

    if ticket_authed:
        # Fresh @ooc arrival on the router. Persist the OOC intent
        # onto the account so subsequent reconnects (refresh, fresh
        # login next day) honour the same intent without needing the
        # ticket to still be present in the URL.
        self.db._shards_at_ooc_menu = True
        self.msg(self.at_look(target=self.characters, session=session), session=session)
        return

    if self.db._shards_at_ooc_menu:
        # No fresh ticket auth on this connection (refresh / reconnect /
        # next-day login), but the persisted intent says OOC.
        self.msg(self.at_look(target=self.characters, session=session), session=session)
        return

    last_puppet = self.db._last_puppet

    if _is_redirectable_character(last_puppet):
        _redirect_to_character_shard(self, session, last_puppet)
        return

    if last_puppet is not None:
        logger.log_warn(
            f"at_post_login on router: account {self} has _last_puppet="
            f"{last_puppet} but its shard_id="
            f"{getattr(last_puppet, 'shard_id', None)!r} is unusable — "
            "falling back to OOC menu"
        )

    # OOC menu (reproduced from Evennia at_post_login else-branch).
    self.msg(self.at_look(target=self.characters, session=session), session=session)


def warn_if_at_post_login_overridden(account_cls, role) -> bool:
    """Log a warning if ``account_cls`` overrides ``at_post_login``.

    The library patches ``DefaultAccount.at_post_login`` directly (full
    replacement on routers, thin wrapper on shards). A consumer that
    subclasses ``DefaultAccount`` and overrides ``at_post_login``
    anywhere in the chain *between* their configured class and
    ``DefaultAccount`` shadows our patch via Python MRO unless the
    override calls ``super().at_post_login(...)``.

    Detection walks ``account_cls.__mro__`` from the leaf class up,
    stopping at ``DefaultAccount`` (which is the library's patch
    target, not a consumer override). If any class along the way has
    ``at_post_login`` in its ``__dict__``, that's an override — fire
    the warning.

    The warning fires even when the override is well-behaved (does
    call ``super()``); the false-positive cost is one log line at
    startup. The true-positive case (no ``super()``) catches a silent
    failure mode that's otherwise only discoverable by the library's
    behaviour mysteriously not running.

    Returns True if a warning was emitted, False otherwise. Used by
    tests to assert detection without coupling to log capture.
    """
    from evennia.accounts.accounts import DefaultAccount

    overriding_cls = None
    for cls in account_cls.__mro__:
        if cls is DefaultAccount:
            break
        if "at_post_login" in cls.__dict__:
            overriding_cls = cls
            break
    if overriding_cls is None:
        return False
    role_specific = (
        "auto-puppet redirect logic"
        if role == "router"
        else "idmapper / Attribute cache-bust before auto-puppet"
    )
    logger.log_warn(
        f"evennia-shards: {overriding_cls.__module__}."
        f"{overriding_cls.__qualname__}.at_post_login is overridden. "
        f"The library patches DefaultAccount.at_post_login; this "
        f"override shadows the library's patch via Python MRO unless "
        f"it calls super().at_post_login(session=session, **kwargs). "
        f"Without the super() call, the {role} role's "
        f"{role_specific} will not run for this account class. See "
        f"DESIGN/library-integration-risks.md."
    )
    return True


def make_shard_at_post_login(original_at_post_login):
    """Return a shard-side ``at_post_login`` that busts stale caches.

    The Account's Attribute-handler cache on this process can hold a
    stale Python object for ``_last_puppet`` after another process has
    moved the character's row (via ``cross_shard_move``, which writes
    via ``qs.update`` and so doesn't go through this process's
    idmapper). The wrapper flushes the cached Python object and
    refreshes its fields from the DB before vanilla ``at_post_login``
    reads ``_last_puppet`` and calls ``puppet_object`` on it.

    If the character's row is no longer owned by this shard
    (cross-shard move while the account was offline) or has been
    deleted, we null out ``_last_puppet`` so vanilla ``at_post_login``
    falls through to the OOC menu instead of crashing on
    ``puppet_object``. The visibility check uses ``ObjectDB.objects``
    deliberately — that manager carries the tenant auto-filter, so
    foreign rows return False from ``exists()``. Django's
    ``refresh_from_db`` would bypass it (it routes through
    ``_base_manager``, which is unfiltered), so we can't rely on it
    to surface the foreign-row case on its own.
    """

    def shard_at_post_login(self, session=None, **kwargs):
        from evennia.objects.models import ObjectDB

        character = self.db._last_puppet
        if character is not None and hasattr(character, "flush_from_cache"):
            character.flush_from_cache(force=True)
            if ObjectDB.objects.filter(pk=character.pk).exists():
                character.refresh_from_db()
            else:
                self.db._last_puppet = None
        original_at_post_login(self, session=session, **kwargs)

    return shard_at_post_login
