"""Django AppConfig for evennia-shards.

Only loaded when the consumer adds `evennia_shards` to `INSTALLED_APPS`,
which by convention they only do in non-monolith roles.
"""

from django.apps import AppConfig


class EvenniaShardsConfig(AppConfig):
    name = "evennia_shards"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .config import ROLE_MONOLITH, ROLE_ROUTER, ROLE_SHARD, get_role

        if get_role() != ROLE_MONOLITH:
            # Bootstrap the django-multitenant tenant context for this
            # process. Shard processes scope all queries on tenant-tagged
            # models to their own rows + ``"*"`` global rows; the router
            # runs unscoped so it can coordinate across shards.
            # See evennia_shards/tenancy.py and DESIGN/shard-isolation.md.
            from .tenancy import bootstrap_tenant_context, install_tenancy_on_objectdb

            bootstrap_tenant_context()
            install_tenancy_on_objectdb()

            from django.conf import settings

            # Override the WebSocket protocol class so the library can
            # intercept incoming connections for ticket-based auth.
            # Stashes the consumer's current value so protocols.py can
            # subclass it (preserving any consumer customisations).
            settings._SHARDS_ORIGINAL_WS_PROTOCOL = getattr(
                settings,
                "WEBSOCKET_PROTOCOL_CLASS",
                "evennia.server.portal.webclient.WebSocketClient",
            )
            settings.WEBSOCKET_PROTOCOL_CLASS = (
                "evennia_shards.protocols.ShardWebSocketClient"
            )

            # Inject the shard redirect JS middleware so the webclient
            # gets the redirect plugin without any template edits.
            _middleware_path = (
                "evennia_shards.middleware.ShardRedirectScriptMiddleware"
            )
            if _middleware_path not in settings.MIDDLEWARE:
                settings.MIDDLEWARE = list(settings.MIDDLEWARE) + [
                    _middleware_path
                ]

            # Register the Portal-side services plugin so we can
            # register the webclient WebSocket independently when
            # WEBSERVER_ENABLED=False. See evennia_shards/portal_services.py
            # for why this exists (Evennia bundles WS registration
            # inside register_webserver, forcing webserver+WS as a
            # combined unit; we untangle them).
            _portal_plugin_path = "evennia_shards.portal_services"
            _existing_plugins = list(
                getattr(settings, "PORTAL_SERVICES_PLUGIN_MODULES", []) or []
            )
            if _portal_plugin_path not in _existing_plugins:
                settings.PORTAL_SERVICES_PLUGIN_MODULES = (
                    _existing_plugins + [_portal_plugin_path]
                )

            # Replace DefaultAccount.at_post_login with role-specific
            # overrides.
            #
            # Router: full replacement that intercepts auto-puppet and
            #   converts it to a ticket+redirect to the character's
            #   owning shard. See DESIGN/library-integration-risks.md.
            #
            # Shard: thin wrapper around Evennia's original that flushes
            #   stale idmapper/Attribute-cache entries for _last_puppet
            #   before auto-puppet, and nulls out _last_puppet if the
            #   character's row has been moved off this shard.
            from evennia.accounts.accounts import DefaultAccount
            from evennia.utils.utils import class_from_module

            # Resolve the consumer-configured account class once. Used
            # for the chargen wrapper install (which patches it
            # directly so consumer overrides compose) and for
            # detecting at_post_login overrides that would shadow the
            # library's DefaultAccount patch (the at_post_login patch
            # itself stays on DefaultAccount — see
            # library-integration-risks.md for the asymmetry rationale).
            from .hooks import warn_if_at_post_login_overridden

            AccountCls = class_from_module(settings.BASE_ACCOUNT_TYPECLASS)
            warn_if_at_post_login_overridden(AccountCls, get_role())

            if get_role() == ROLE_ROUTER:
                from .hooks import shard_aware_at_post_login

                DefaultAccount.at_post_login = shard_aware_at_post_login

            elif get_role() == ROLE_SHARD:
                from .hooks import make_shard_at_post_login

                DefaultAccount.at_post_login = make_shard_at_post_login(
                    DefaultAccount.at_post_login
                )

            # Wrap create_character on the consumer's configured
            # account class so newly created characters get their
            # shard_id stamped from the start-location row's
            # shard_id. Under multitenant the router runs unscoped,
            # so the auto-stamp on insert is skipped — without this
            # wrapper, the new row lands with ``shard_id=NULL``,
            # which is not redirectable. Wrapping AccountCls (rather
            # than DefaultAccount) lets a consumer override of
            # create_character compose with the library's stamp via
            # the wrap pattern. Same pattern as protocols.py uses
            # for WEBSOCKET_PROTOCOL_CLASS.
            if get_role() == ROLE_ROUTER:
                from .chargen import make_shard_aware_create_character

                AccountCls.create_character = (
                    make_shard_aware_create_character(
                        AccountCls.create_character
                    )
                )

            # Replace CmdIC with shard-aware version that redirects on
            # routers and blocks on shards. The AccountCmdSet references
            # account.CmdIC via module attribute, so patching the module
            # makes the cmdset pick up our version on rebuild.
            from evennia.commands.default import account as _account_module

            from .commands import ShardAwareCmdIC

            _account_module.CmdIC = ShardAwareCmdIC

            if get_role() == ROLE_SHARD:
                from .commands import ShardAwareCmdOOC

                _account_module.CmdOOC = ShardAwareCmdOOC

            # Auto-install permanent library admin commands into
            # CharacterCmdSet. We can't import cmdset_character here:
            # its transitive imports (building → prototypes/menus →
            # evmenu) reference ``evennia.Command``, which is still
            # None until ``evennia._init()`` runs. Real-runtime
            # entry points (server.py, portal.py) call ``_init()``
            # AFTER ``django.setup()``, so our ``ready()`` is too
            # early. Wrap ``evennia._init`` instead — when it runs,
            # the lazy exports are populated and the cmdset_character
            # import chain works. See DESIGN/library-integration-risks.md.
            import evennia

            if not getattr(evennia, "_evennia_shards_init_wrapped", False):
                _original_init = evennia._init

                def _shards_wrapped_init(*args, **kwargs):
                    _original_init(*args, **kwargs)

                    # Module-attribute swap for CmdTeleport. Deferred to
                    # here because importing teleport.py pulls in
                    # evennia.commands.default.building, which transitively
                    # imports evmenu — and evmenu's module body subclasses
                    # evennia.Command at load time. Before _original_init
                    # runs, evennia.Command is None and that subclassing
                    # raises TypeError. After _original_init, the lazy
                    # exports are populated and the chain is safe.
                    from evennia.commands.default import building as _building_module

                    from .teleport import ShardAwareCmdTeleport

                    _building_module.CmdTeleport = ShardAwareCmdTeleport

                    from evennia.commands.default.cmdset_character import (
                        CharacterCmdSet,
                    )

                    if getattr(
                        CharacterCmdSet,
                        "_evennia_shards_cmdset_patched",
                        False,
                    ):
                        return
                    _original_at_cmdset_creation = (
                        CharacterCmdSet.at_cmdset_creation
                    )

                    def _shard_aware_at_cmdset_creation(self):
                        _original_at_cmdset_creation(self)
                        from .commands import (
                            CmdCrossShardDig,
                            CmdShardCheck,
                        )

                        self.add(CmdShardCheck())
                        self.add(CmdCrossShardDig())

                    CharacterCmdSet.at_cmdset_creation = (
                        _shard_aware_at_cmdset_creation
                    )
                    CharacterCmdSet._evennia_shards_cmdset_patched = True

                evennia._init = _shards_wrapped_init
                evennia._evennia_shards_init_wrapped = True

