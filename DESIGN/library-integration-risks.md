# Library Integration Risks

Where `evennia-shards` couples to Evennia internals, and the risks each coupling carries. Two audiences read this doc:

- **Library maintainer upgrading Evennia** — diff each coupling against the new Evennia version.
- **Consumer game subclassing or customising Evennia** — know which Evennia surfaces the library has already taken a position on, and the recommended composition pattern.

Each coupling section follows the same template:

- **What we patch / extend** — the Evennia symbol the library touches, and where the library code lives.
- **Why** — the requirement that drives the coupling.
- **Risk on Evennia upgrade** — what to diff in the new Evennia version.
- **Risk in consumer override** — what consumer-side customisation would collide, and the recommended pattern.

This doc is filled in lazily — a coupling is added when it first lands or is next touched. Currently covered: `WebSocketClient.onOpen`, `DefaultAccount.at_post_login`, `Account.create_character`, `CmdIC` / `CmdOOC` (hard overrides — library territory), `evennia._init()` wrap + `CharacterCmdSet.at_cmdset_creation`, webclient HTML injection (middleware regex match against the rendered template), `PORTAL_SERVICES_PLUGIN_MODULES` injection (Portal-side WebSocket registration when `WEBSERVER_ENABLED = False`). Other library couplings (`ObjectDB.from_db`, `pre_save`/`pre_delete` signals, `QuerySet.update`, `WEBSOCKET_PROTOCOL_CLASS` rewiring) will be backfilled as we revisit them.

## WebSocketClient.onOpen() override

**What we patch / extend:** `evennia/server/portal/webclient.py` → `WebSocketClient.onOpen()`. Library code: `evennia_shards/protocols.py` → `ShardWebSocketClient.onOpen()`. Based on Evennia 6.0.0.

**Why:**

Ticket-based auth needs `self.uid` and `self.logged_in` set on the session *after* `init_session()` (which resets both to `None`/`False`) but *before* `sessionhandler.connect()` (which snapshots session state via `get_sync_data()` and sends it to the Server over AMP). The Server's `portal_connect()` auto-logins sessions that arrive with `logged_in=True` and a valid `uid`.

There is no method-level seam between `init_session()` and `sessionhandler.connect()` — both are called inline in `onOpen()`. Three alternatives were ruled out:

- **Swapping call order** (ticket validation before `super().onOpen()`): `init_session()` wipes `uid`/`logged_in`, and `self.address` isn't set yet for IP validation.
- **Post-connect re-sync** (`sessionhandler.sync()`): the Portal's `sync()` deliberately excludes `uid` and `logged_in` from the data it sends to the Server. There is no Portal→Server "please login this session" AMP operation.
- **Overriding `get_sync_data()`**: works mechanically but splits the auth logic across two methods with non-obvious interaction, and makes a data-serialisation method responsible for auth decisions.

Overriding `onOpen()` is the only clean approach. Our override reproduces the parent method and adds a ticket-auth check in the same position as the existing browser-session auth check (between `init_session()` and `sessionhandler.connect()`).

**Risk on Evennia upgrade:**

- New logic added between `init_session()` and `sessionhandler.connect()` — our override would miss it.
- Changes to the `init_session()` / `get_client_session()` / `sessionhandler.connect()` call sequence.
- New protocol flags or connection setup steps added to `onOpen()`.
- Changes to `SESSION_SYNC_ATTRS` that affect what `get_sync_data()` sends.

How to check: diff the upstream `onOpen()` against the snapshot in our override. The override carries a comment citing the Evennia version it was based on.

**Risk in consumer override:**

A consumer that sets a custom `WEBSOCKET_PROTOCOL_CLASS` is **safe by construction**: `AppConfig.ready()` stashes the consumer's class as `_SHARDS_ORIGINAL_WS_PROTOCOL` and `ShardWebSocketClient` subclasses *that* dynamically. Consumer customisations are preserved underneath the library's onOpen logic.

The hazard is a consumer overriding `onOpen()` on their custom class without calling `super().onOpen()` — that bypasses our ticket-auth injection entirely. Recommended pattern: any consumer override of `onOpen()` must call `super().onOpen()` (or accept that ticket-auth will not run on their connections).

## DefaultAccount.at_post_login() override

**What we patch / extend:** `evennia/accounts/accounts.py` → `DefaultAccount.at_post_login`. Two role-specific patches, both installed via monkey-patch in `AppConfig.ready()`. Based on Evennia 6.0.0.

- **Router** (`get_role() == ROLE_ROUTER`): full replacement → `evennia_shards/hooks.py` → `shard_aware_at_post_login`.
- **Shard** (`get_role() == ROLE_SHARD`): thin wrapper around Evennia's original → `evennia_shards/hooks.py` → `make_shard_at_post_login(original)`. Flushes the `_last_puppet` character from the idmapper and gates `refresh_from_db` behind an explicit `ObjectDB.objects.filter(pk).exists()` check (the auto-filter; `refresh_from_db` itself routes through `_base_manager` and would bypass it). If the row is no longer visible from this shard (moved or deleted), `_last_puppet` is cleared so vanilla falls through to the OOC menu instead of crashing on `puppet_object`.

**Why:**

When `AUTO_PUPPET_ON_LOGIN = True` (Evennia's default), `at_post_login` calls `puppet_object(session, self.db._last_puppet)` directly, with no exposed seam between the function's prelude (protocol-flag load, `logged_in` OOB, connect-channel msg) and the if/else that decides whether to puppet. On a router this is doubly broken: `_last_puppet=None` raises and the player sees `"The Character does not exist."`; `_last_puppet=<character on shardN>` puppets the character on the *router* — which is structurally wrong (characters live on shards) and the subsequent save would land the character row on the router's identity.

The library needs to make a routing decision (redirect-to-shard vs render OOC menu) at exactly the position where Evennia's if/else lives. Three alternatives were considered and rejected:

- **Mutate `_AUTO_PUPPET_ON_LOGIN`** to force the else-branch: it's a module-level cached constant. Mutating it is process-global, races on concurrent logins, and the redirect would still need to happen *after* the else-branch rendered the OOC menu (visible flash before the WS-level redirect kicks in).
- **Override `puppet_object` instead**: wrong granularity — by the time `puppet_object` is called the prelude has run and the if-branch has been taken. Doesn't handle the `_last_puppet=None` case at all (because `puppet_object` is never called when it raises early). Also broadens blast radius (`puppet_object` is also called from `CmdIC` etc.).
- **Use `SIGNAL_ACCOUNT_POST_LOGIN`**: fires *after* `at_post_login` completes — by which point the puppet has already happened or the OOC menu has rendered. Three state transitions instead of one.

Reproducing the body and swapping the if-branch is the only clean approach. The else-branch (OOC character-select menu via `at_look(target=characters)`) is reproduced for the fallback path so a broken `_last_puppet` lands the player in the OOC menu rather than failing login.

**Risk on Evennia upgrade:**

- New steps added to the prelude (protocol-flag load, `logged_in` OOB, connect-channel msg). Our reproduction would miss them.
- Changes to the if-condition (e.g. new conditions added beyond `AUTO_PUPPET_ON_LOGIN`).
- Changes to the else-branch's OOC-menu rendering — currently `self.msg(self.at_look(target=self.characters, session=session), session=session)`. We reproduce that line.
- Signature or call-order changes around `at_post_login` in `sessionhandler.login()`.

How to check: diff upstream `DefaultAccount.at_post_login` against the snapshot in `shard_aware_at_post_login`. The override carries a comment citing the Evennia version it was based on. The shard wrapper is thin (flush + refresh + delegate), so only the router replacement needs a line-by-line diff.

**Risk in consumer override:**

A consumer that subclasses `DefaultAccount` and overrides `at_post_login` without calling `super().at_post_login(...)` will bypass our patch — the consumer's body runs instead. On the router, auto-puppet redirect stops working. On shards, the cache-busting preamble is skipped and a `_last_puppet` that's been moved off this shard will crash the puppet step instead of falling through to the OOC menu. Recommended pattern: any consumer override of `at_post_login` must call `super().at_post_login(session=session, **kwargs)` (or accept that these behaviours will not run on their accounts).

A consumer that *doesn't* override `at_post_login` is safe by Python MRO — `account.at_post_login(...)` resolves to our patched `DefaultAccount.at_post_login` automatically.

**Asymmetry with `Account.create_character` (deliberate).** The chargen wrapper (below) patches the consumer's configured `BASE_ACCOUNT_TYPECLASS`, so consumer overrides compose; this wrapper patches `DefaultAccount` directly, so consumer overrides shadow ours via MRO unless they call `super()`. The asymmetry is intentional. Chargen is naturally wrap-shaped (call vanilla, read the resulting row, stamp); `at_post_login` is replacement-shaped (the auto-puppet branch is exactly what we replace, vanilla's body would do the wrong thing if called as the inner step of a wrap). Switching to a `BASE_ACCOUNT_TYPECLASS` patch here would silently *clobber* a consumer's override instead of being silently shadowed by it — a different failure mode, not an obviously better one. The current pattern leaves consumer overrides reachable and documents the recovery (call `super()`).

**Possible future improvement: partial wrap.** Delegate to `original.at_post_login` on the `AUTO_PUPPET_ON_LOGIN=False` path (which is just OOC-menu rendering — vanilla / consumer-override does the right thing) and keep the inline router redirect logic only on the `AUTO_PUPPET_ON_LOGIN=True` path. Result: consumer overrides compose cleanly for the no-auto-puppet case (the more common case for many games), and only get bypassed for the auto-puppet path the library specifically exists to handle. Trade-off: in the `AUTO_PUPPET=True + consumer-override` case, behaviour shifts from "library silently shadowed by consumer" to "consumer silently overwritten by library" — a different failure mode, not strictly better. Worth picking up if a real consumer game lands an `at_post_login` override and wants composition semantics; not load-bearing for current MVP scope.

**Inherited Evennia limitation: MULTISESSION_MODE 2/3.** Evennia's `_last_puppet` is a single account-scoped Attribute. Under `MULTISESSION_MODE = 2` or `3`, an account can have multiple simultaneous puppets across sessions, and a single attribute can't represent "which puppet belongs to which session for reconnect." Evennia itself documents this in `settings_default.py`: *"This will only work if the session/puppet combination can be determined (usually `MULTISESSION_MODE 0` or `1`)."* The library inherits the limitation — `AUTO_PUPPET_ON_LOGIN = True` on routers works under modes 0 and 1; modes 2/3 are documented-broken to the same degree as vanilla Evennia. The conceptual fix (per-session puppet memory, e.g. `account.db._last_puppets = {session_key: char_id, ...}` keyed by something durable) is an Evennia-level change — the session-puppet mapping lives in `AccountDB`/`puppet_object`, not in this library. If Evennia upstream ever ships per-session puppet memory, this library's `at_post_login` override should adopt it transparently.

**Install-time detection of consumer overrides.** Because the silent-shadow failure mode is hard to discover at runtime (the library's behaviour just doesn't run, no error fires), `AppConfig.ready()` calls `warn_if_at_post_login_overridden(AccountCls, role)` after resolving `BASE_ACCOUNT_TYPECLASS`. The helper walks the MRO from the consumer's account class up to (but not including) `DefaultAccount`; if any class along the way has `at_post_login` in its `__dict__`, a warning is emitted at startup naming the offending class and reminding the consumer to call `super()`. The detection is structural (`__dict__` membership), so a well-behaved override that *does* call `super()` will also trigger the warning — false-positive cost is one log line at startup, which is much cheaper than a silent shadow.

## `Account.create_character()` wrapper

**What we patch / extend:** `evennia/accounts/accounts.py` → `Account.create_character` (the consumer-configured account class via `settings.BASE_ACCOUNT_TYPECLASS`, not `DefaultAccount` directly). Library code: `evennia_shards/chargen.py` → `make_shard_aware_create_character(original)`. Installed only on the router. Based on Evennia 6.0.0.

**Why:**

`Account.create_character` is the converging seam for all chargen paths — `CmdCharCreate`, `AUTO_CREATE_CHARACTER_WITH_ACCOUNT`, and the guest path all funnel through it. It runs on the router (chargen is an OOC operation; the player's session lives on the router while OOC). The router runs unscoped under the multitenant integration, so the auto-stamp on insert is skipped and the new row lands with `shard_id=NULL` — not in `SHARD_URLS`, so `ShardAwareCmdIC` and the `at_post_login` auto-puppet path cannot redirect the player to any shard.

The wrapper calls vanilla unmodified, then reads the new character's `db_location_id`'s `shard_id` via `.values_list` and stamps the character to match. The character's shard is by definition the shard that owns its location row — there is no separate policy decision. The post-create assignment-then-save lands without a bypass: the row's `shard_id` is `NULL` so the `__setattr__` immutability check passes through, and the router being unscoped means `_do_update` applies no extra tenant filter.

`DEFAULT_HOME` is not touched at chargen time — vanilla `create_character` does not set `db_home`, and any later cross-shard home transfer is a runtime move handled by `cross_shard_move`.

**Risk on Evennia upgrade:**

- Changes to `Account.create_character`'s return contract (currently `(character, errs)`). Wrapper assumes a tuple and a falsy `character` on failure.
- Changes to whether `db_location_id` is set inline by `account.create_character` before returning (currently set from `settings.START_LOCATION` if not provided). If a future Evennia version delays location assignment, the wrapper's lookup would see `None` and skip the stamp.
- A new chargen path that bypasses `Account.create_character` (e.g. a separate `Account.create_npc` or rewritten `DefaultGuest.authenticate`) would not pass through the wrapper.

How to check: diff upstream `Account.create_character` (and `DefaultGuest.authenticate`'s character-creation block) against the calling pattern the wrapper assumes.

**Risk in consumer override:**

A consumer that subclasses `DefaultAccount` and overrides `create_character` is **safe by construction**: the wrapper is installed on the configured `BASE_ACCOUNT_TYPECLASS` and reads `AccountCls.create_character` at install time, so MRO picks up either the consumer's override or the inherited `DefaultAccount` method — whichever is in effect — and wraps that. The consumer's body runs first, then the stamp.

The hazard is a consumer override that doesn't actually call `create.create_object` (or otherwise produces a character whose `db_location_id` is `None`). The wrapper logs a warning and leaves `shard_id` as `NULL`; chargen succeeds but IC won't work. Recommended pattern: any consumer override should set `db_location` via the same `START_LOCATION` (or equivalent) source as vanilla.

A consumer who points `BASE_ACCOUNT_TYPECLASS` at a class that doesn't derive from `DefaultAccount` would not have `create_character` at all; the wrapper install would fail at startup. This is symmetrical to other base-class assumptions in Evennia (locks, `_playable_characters`, etc.).

## `CmdIC` / `CmdOOC` — hard overrides

**What we patch / extend:** `evennia/commands/default/account.py` → `CmdIC` (both router and shard roles) and `CmdOOC` (shard role only). Library code: `evennia_shards/commands.py` → `ShardAwareCmdIC`, `ShardAwareCmdOOC`. Installed via module-attribute swap in `AppConfig.ready()` (`_account_module.CmdIC = ShardAwareCmdIC`). Based on Evennia 6.0.0.

**Why:**

In sharded mode, `CmdIC` and `CmdOOC` *are* the cross-shard redirect mechanism — that's their entire job. `ShardAwareCmdIC.func` does not call `super().func()`; it implements the IC flow from scratch (resolve character → create ticket → emit `shard_redirect` OOB → close session). Vanilla `CmdIC.func` would puppet the character locally on the router, which is structurally wrong (characters live on shards, not the router). There is no compose-with-vanilla story: sharded IC isn't "vanilla IC plus some redirect logic," it's a different operation entirely. The same reasoning applies to `CmdOOC` on shards — going OOC from a shard means redirecting back to the router, not unpuppeting locally.

**Library territory, not a consumer extension point.**

These two commands are owned by the library in non-monolith roles. Subclassing or replacing them is **not** a supported integration pattern:

- `class MyCmdIC(CmdIC): ...` in a sharded deployment is a category error — the consumer is extending vanilla IC semantics, but the runtime IC semantics are the library's. Whether the resulting subclass picks up `ShardAwareCmdIC` or vanilla `CmdIC` as its base depends on Python import order (whether the consumer's module was imported before or after `AppConfig.ready()` ran our patch). Either way the resulting code is wrong: with vanilla as base, the cross-shard redirect never runs; with `ShardAwareCmdIC` as base, the consumer's customisation is layered on top of a flow whose contract they didn't design against.
- Adding `CmdIC()` directly to a custom cmdset (rather than letting `AccountCmdSet` resolve it via the patched module attribute) has the same import-order failure: the local binding may have snapshotted vanilla.

The recommended posture is **don't subclass or replace IC/OOC in sharded deployments.** If a consumer game genuinely needs IC/OOC behaviour the library doesn't provide (audit logging on IC, custom error messaging on OOC, etc.), the right path is to discuss it as a library feature or contrib pattern — not to layer their own semantics on top of an infrastructure command. The library deliberately does not document a "subclass `ShardAwareCmdIC` like this" pattern, because doing so would imply support for use cases that haven't been thought through.

**Risk on Evennia upgrade:**

- Changes to `CmdIC.func` / `CmdOOC.func` body — the router redirect and OOC-return logic in our overrides reproduces the parts of vanilla we explicitly do *not* want (e.g. character resolution from `account.characters`), so a diff against upstream is needed when bumping Evennia.
- Changes to where `AccountCmdSet` looks up `CmdIC` / `CmdOOC` — currently a module-attribute reference (`account_module.CmdIC`), which is what makes the swap work. If Evennia ever changes `AccountCmdSet` to import `CmdIC` directly into its own module at definition time, our module-attribute swap stops being seen and we'd need to switch to a different patch shape.

How to check: diff upstream `CmdIC.func` and `CmdOOC.func` bodies against what `ShardAwareCmdIC._resolve_character` reproduces; verify `AccountCmdSet` still references commands via module attribute (`from evennia.commands.default import account; account.CmdIC` or equivalent).

**Risk in consumer override:** see "Library territory" above. If a consumer does subclass or replace these, behaviour is undefined — not because of a recoverable bug, but because the consumer is replacing infrastructure they don't own. The library does not detect this case at install time (unlike `at_post_login`); the recommendation is documentation only. Consumer subclasses can be detected by the same MRO walk we use for `at_post_login`, but the failure isn't recoverable via a documented `super()` pattern, so the warning would just say "don't do this" — easier to say it once in the docs than at every startup.

## `CmdTeleport` — narrow override (delegate to vanilla when local)

**What we patch / extend:** `evennia/commands/default/building.py` → `CmdTeleport`. Library code: `evennia_shards/teleport.py` → `ShardAwareCmdTeleport`. Installed via module-attribute swap on `building.CmdTeleport` — same shape as `CmdIC` / `CmdOOC` — but the swap is performed inside `_shards_wrapped_init` (the `evennia._init` wrap, see below) rather than at `AppConfig.ready()` time, because the `building.py` import chain pulls in `prototypes/menus` → `evmenu`, and `evmenu` subclasses `evennia.Command` at module load. `evennia.Command` is a lazy export populated by `evennia._init()`; importing `building` before `_init` runs raises `TypeError: NoneType takes no arguments`. The `_shards_wrapped_init` runs `_original_init` first, so the lazy exports are populated by the time we import `teleport.py`. Based on Evennia 6.0.0.

**Why:**

`CmdTeleport.parse` calls `caller.search(name, global_search=True)` up to three times to resolve `obj_to_teleport` and `destination`. On a shard process, that search routes through the auto-filtered `ObjectDB.objects` manager and silently sees only local + global rows — foreign-shard rooms are invisible to vanilla's lookup, so cross-shard `@tel` would fail with "Destination not found" even though the destination exists.

The override has a narrow design: stay close to vanilla. The class subclasses `CmdTeleport`. `parse()` mirrors vanilla's structure 1:1, substituting the three `caller.search` calls with [`shard_aware_global_search`](shard-aware-search.md) (which returns either a loaded instance for a local match or pk + shard_id for a foreign match). `func()` dispatches into three branches:

1. **`/tonone`** — vanilla logic if `obj_to_teleport` is local. If foreign, refuse with a pointer to the "teleport yourself to that shard first, then run /tonone locally" workflow.
2. **Both local** — delegate to vanilla `super().func()` unchanged. All vanilla behaviour (lock checks, equality checks, `/loc` / `/intoexit` / `/quiet`, the move itself, announce messages, failure paths) runs untouched. This is the common case.
3. **Cross-shard** — route via the library's `cross_shard_move` primitive when the destination is foreign and the object is local. (The foreign-object case is refused with the same "teleport yourself across first" pointer; supporting it would require a remote-execute primitive the library does not currently provide.)

The branch where both targets are local — by far the common case in practice — runs vanilla code verbatim. The cross-shard branch wraps an existing library primitive. The structure imitates rather than reimplements; the override surface area is minimal.

Two small vanilla alignments live in the cross-shard branch itself, both aimed at making the user-visible behaviour match vanilla wherever it's cheap:

- **Multi-match disambiguation prompt.** When `shard_aware_global_search` returns `state="multiple"`, the override renders the candidate list (`#5 (Tavern, shard0), #12 (Tavern, shard1) - specify by dbref.`) and raises `InterruptCommand`. The user gets actionable information instead of a generic "ambiguous" refusal.
- **Same-position short-circuit.** Before calling the `cross_shard_move` primitive, the override checks `obj.db_location_id == dest_pk`. If they match, the obj is already in the destination room — pks are globally unique under the [single-Postgres bound](library-scope-and-mandates.md), so the pk match is equivalent to vanilla's `obj.location == destination` instance comparison. Emits vanilla's `"<obj> is already at <dest>."` and returns without bus traffic.
- **Obj-side `teleport` lock check.** Before calling `cross_shard_move`, the override mirrors vanilla's `caller.permissions.check("Admin") or obj.access(caller, "teleport")` short-circuit ([building.py:3922](https://github.com/evennia/evennia/blob/main/evennia/commands/default/building.py)). Without it, a `teleport`-locked obj would be teleportable cross-shard despite vanilla refusing the same op locally — a security inconsistency. The destination-side `teleport_here` lock is **not** checked here: the destination row lives on the foreign shard and there's no local way to evaluate its lock against the obj. That requires a bus round-trip and is deferred.
- **Leave / arrive announces.** Cross-shard movement bypasses Evennia's `move_to`, so the vanilla `announce_move_from` / `announce_move_to` hooks would silently drop. The override fires the announces explicitly: source-side via local `source.msg_contents(...)` synchronously before the move (source room is local in this branch, so this is a plain `msg_contents` call); destination-side via `send_cross_shard_room_message` after the move, routing through the bus's [`room_msg`](cross-shard-message-bus.md) kind. The destination-side announce text is composed on the source side (the only side with both `obj.key` and `source.key` available locally) and shipped in the bus payload; receiver renders verbatim. Both announces are gated on `/quiet`, matching vanilla. Caller-facing confirmation is NOT gated on `/quiet` (vanilla emits it unconditionally — `/quiet` is documented as suppressing room announces only).

Name-resolution coverage in the helper itself ([shard-aware-search.md](shard-aware-search.md)) — dbref, exact key, alias, `me`/`self`/`here` — matches vanilla `caller.search(global_search=True)` for everything except fuzzy / partial name matching.

**Risk on Evennia upgrade:**

- Changes to `CmdTeleport.parse`'s structure — currently three discrete `caller.search` calls organised under `if self.rhs / elif self.lhs`. Our parse mirrors this exact branching. If vanilla refactors parse (e.g. moves to a helper, adds a fourth lookup, changes the rhs vs no-rhs distinction), our parse needs the same refactor.
- Changes to `CmdTeleport`'s parent class. Currently `COMMAND_DEFAULT_CLASS` (typically `MuxCommand`), which is where the `lhs` / `rhs` splitting via `rhs_split` lives. Our override calls `super(CmdTeleport, self).parse()` explicitly to reach this parent, skipping vanilla `CmdTeleport.parse`'s body. If the parent changes — or if the `rhs_split` mechanism moves — that call site needs updating.
- New switches added to `CmdTeleport` — `/quiet`, `/intoexit`, `/tonone`, `/loc` today. Our dispatch enumerates the relevant ones (`/tonone` short-circuits) and delegates the others to vanilla. A new switch that interacts with cross-shard semantics would need explicit handling in our dispatch.
- Changes to where `CharacterCmdSet` looks up `CmdTeleport` — currently `building.CmdTeleport()` via module attribute (see `evennia/commands/default/cmdset_character.py`). The module-attribute swap pattern relies on this. If Evennia changes the cmdset to bind `CmdTeleport` at import time, the swap stops being seen and we'd need a different patch shape.

How to check: diff upstream `CmdTeleport.parse` and `CmdTeleport.func` against our override on Evennia bump. The relevant cmdset references are in `cmdset_character.py`.

**Risk in consumer override:**

A consumer who subclasses `CmdTeleport` to add custom teleport behaviour (audit logging, restricted-target rules, narrative beats) needs to be aware of the swap: by the time `CharacterCmdSet.at_cmdset_creation` runs, `building.CmdTeleport` points at `ShardAwareCmdTeleport`, so a consumer subclass picks that up via MRO automatically — gaining shard-awareness for free.

A consumer who binds `CmdTeleport` at import time (`from evennia.commands.default.building import CmdTeleport` at the top of their module, used as a base class for a custom subclass) may snapshot vanilla `CmdTeleport` before our swap runs. The result: their subclass extends vanilla, the cross-shard safety is bypassed. Recommended pattern: import the module, reference `building.CmdTeleport` at class-definition time (or define the subclass inside a function called at cmdset-creation time). The library does not detect this case at install time; the recommendation is documentation only.

A consumer who replaces `CmdTeleport` entirely without subclassing the library version is replacing a piece of cross-shard infrastructure, and behaviour is undefined. Same posture as the `CmdIC` / `CmdOOC` "library territory" guidance above.

## `evennia._init()` wrap + `CharacterCmdSet.at_cmdset_creation` override

**What we patch / extend:** `evennia._init` (the function in `evennia/__init__.py` that populates Evennia's lazy top-level exports — `Command`, `CmdSet`, etc.) is wrapped from `AppConfig.ready()` to install a follow-on patch on `evennia.commands.default.cmdset_character.CharacterCmdSet.at_cmdset_creation`. The follow-on patch adds the library's permanent admin commands (`CmdShardCheck`, `CmdCrossShardDig`) after the parent populates the default cmdset. Library code: `evennia_shards/apps.py` (the wrap installation) and `evennia_shards/commands.py` (the commands themselves). Based on Evennia 6.0.0.

**Why:**

The library ships permanent superuser commands ("Shard Management" category) that should appear on every sharded deployment with no consumer-side cmdset wiring required. Evennia's standard cmdset-extension point is `CharacterCmdSet.at_cmdset_creation`: subclass and call `self.add(...)` after `super()`. The library does the equivalent via monkey-patch so consumers get the commands without editing `default_cmdsets.py`.

The `evennia._init()` wrap is the indirection that makes this safe. Importing `cmdset_character` at `AppConfig.ready()` time eagerly pulls the chain `cmdset_character → building → prototypes/menus → evmenu`, and `evmenu.py` does `from evennia import Command` at module level. At `ready()` time `evennia.Command` is still `None` (the lazy-init pattern in `evennia/__init__.py`), so `class CmdEvMenuNode(Command):` fails with `TypeError: NoneType takes no arguments`. Real-runtime entry points (`server.py`, `portal.py`, `evennia_launcher`) call `django.setup()` *before* `evennia._init()`, so any `ready()`-time import of `cmdset_character` would trip this in production too — not just in tests. Wrapping `_init` instead defers the import until the lazy exports are populated.

**Risk on Evennia upgrade:**

- Changes to `evennia._init`'s signature or call order — the wrap calls `_original_init(*args, **kwargs)` and assumes the original returns normally before the lazy exports are populated.
- Changes to `CharacterCmdSet.at_cmdset_creation`'s contract — our wrap calls the original first then `self.add()`s our commands.
- If Evennia ever populates top-level exports at module-load time (no more `_init` indirection), the whole wrap is unnecessary; remove it.

How to check: diff `evennia/__init__.py`'s `_init` and the lazy-export block; diff `cmdset_character.CharacterCmdSet.at_cmdset_creation`. The `AdminCommandAutoInstallTests.test_character_cmdset_contains_library_commands` test is the canary on this patch firing as expected.

**Risk in consumer override:**

A consumer that subclasses `CharacterCmdSet` and calls `super().at_cmdset_creation()` inherits the library's command additions transparently — standard Evennia pattern, works as expected. A consumer that subclasses but **doesn't** call `super()` skips both the default cmdset population *and* the library's additions. Recommended pattern: always call `super().at_cmdset_creation()` first.

A consumer that points `CMDSET_CHARACTER` at a class that doesn't derive from `evennia.commands.default.cmdset_character.CharacterCmdSet` won't get the library commands. They'd need to manually add them (`from evennia_shards.commands import CmdShardCheck, CmdCrossShardDig`) — the library exposes them as part of its public command surface for exactly that case.

The test runner depends on this wrap behaving — `runtests.py` calls `evennia._init()` explicitly so the deferred patch installation runs in time for tests. Real-runtime startup paths already call `evennia._init()` after `django.setup()`, so no additional integration is needed there.

## Webclient HTML injection (`ShardRedirectScriptMiddleware`)

**What we patch / extend:** Django response middleware that rewrites the rendered HTML of `/webclient*` responses to inject library-side JS. Library code: `evennia_shards/middleware.py` → `ShardRedirectScriptMiddleware`. Auto-installed into `settings.MIDDLEWARE` by `AppConfig.ready()` when `get_role() != ROLE_MONOLITH`. Based on Evennia 6.0.0's webclient template (`evennia/web/templates/webclient/base.html`).

**Why:**

Two pieces of JS need to load in the webclient:

1. **An early inline `<script>`** that runs synchronously *after* the template's inline `var wsurl = ...` block but *before* `evennia.js` loads. This implements refresh routing — read `localStorage` and `PerformanceNavigationTiming.type`, override `window.wsurl` if the page-load is a refresh and a saved shard target exists. The override has to take effect before `evennia.js`'s `WebsocketConnection` reads `window.wsurl` (~500ms later inside `Evennia.init`); any later seam (e.g. `$(document).ready` in `shard_redirect.js`, which fires at end-of-body) misses the read. See [ticket-auth-flow.md](ticket-auth-flow.md#refresh-routing-via-localstorage).

2. **The `shard_redirect.js` script tag**, appended just before `</body>`, for the OOB `shard_redirect` handler (server-emitted `@ic` / `@ooc` / cross-shard redirects). This is allowed to load late because it just registers a listener for server messages.

A consumer-side template edit (`{% block extrascripts %}` or similar) was rejected as a configuration burden — the library's invariant is "drop into INSTALLED_APPS and it works." Middleware injection achieves that.

The early-injection seam is found by regex match on the `<script src="...evennia.js"...>` tag in the rendered HTML (`re.compile(rb"<script\b[^>]*\bsrc=[^>]*evennia\.js[^>]*>\s*</script>")`). The override script is inserted immediately before that tag.

The middleware also has a legacy ticket-injection path (`?ticket=` in the page URL → inline `<script>` appending `&ticket=TOKEN` to `window.csessid`), kept for the manual-paste / bookmark edge case where a webclient page is loaded directly with a ticket query parameter.

**Risk on Evennia upgrade:**

- **Webclient template restructured.** The injection is keyed on the template's `<script src=...evennia.js...>` tag. If a future Evennia version inlines `evennia.js`, bundles it via webpack / ES modules, renames the file, or removes the explicit `<script>` tag in favour of an import map, the regex won't match and the override won't be injected. Failure mode: refresh routing stops working (player goes to router on every refresh), but core auth and IC/OOC flows continue to work because they use OOB `shard_redirect`. Detection: the inline override script's `console.log` line goes silent in the browser console.
- **`var wsurl = ...` removed from template.** The override mutates `window.wsurl`; if the template stops setting it (e.g. `evennia.js` reads connection URL from a `<meta>` tag or computed at init time), the override becomes a no-op. Same failure mode as above.
- **Middleware ordering changes.** The middleware is appended to `settings.MIDDLEWARE` in `AppConfig.ready()`. If Evennia ever moves `SharedLoginMiddleware` (or another middleware) such that our injection runs before HTML is fully rendered, `process_response` could miss the response. Currently it runs after the view, so this hasn't been an issue.
- **Content-Length header semantics.** We update `Content-Length` after injection — if Evennia switches to chunked encoding by default, this becomes a no-op (which is fine), but a regression where Content-Length is required and we mismatch it would truncate the response. Detection: webclient page fails to fully load.

How to check: render `/webclient` from a router gamedir, view source, confirm the early inline override script appears immediately before the `evennia.js` script tag. Verify `shard_redirect.js` is referenced before `</body>`.

**Risk in consumer override:**

A consumer that supplies their own webclient template (e.g. via `{% extends %}` or a custom Django app providing `webclient/base.html`) is the main hazard. The middleware's regex matches "any `<script>` tag whose `src` attribute mentions `evennia.js`," which is robust against attribute order and whitespace variations but assumes the tag exists. A consumer template that:

- Inlines `evennia.js` directly with `<script>...</script>` (no `src=`) would not match. Override is skipped.
- Renames the file to a custom path (`<script src="my-evennia.js">`) would not match either.
- Bundles `evennia.js` into a single bundle with no separate tag — same.

Recommended pattern: consumer templates should keep the `<script src="...evennia.js"...>` form recognisable to the regex. If a consumer needs a fundamentally different webclient bundling strategy, the library's middleware injection is not the right seam — they'd need to inject the early override themselves at the right point in their bundle.

A consumer that strips the `<script src=...shard_redirect.js...>` injection from their pipeline (e.g. with their own response-rewriting middleware that runs after ours) breaks server-side `shard_redirect` OOBs entirely. No documented detection — the library trusts that custom middleware doesn't actively undo our work.

**Note on file scope.** This middleware is scoped to URLs containing `/webclient` — see `process_response`'s early return. It does not touch the website pages, admin, or other Django-served URLs. Evennia's webclient view path is currently `/webclient` (and variants); a future rename would require updating the path filter.


## Portal services plugin

**What we patch / extend:** `settings.PORTAL_SERVICES_PLUGIN_MODULES` — Evennia's hook for additional Portal-side Twisted services. Library code: `evennia_shards/portal_services.py` → `start_plugin_services(portal)`. Auto-registered into the setting by `AppConfig.ready()` for any non-monolith install.

**Why:**

Evennia 6.0.0 registers the webclient WebSocket inside `PortalServerFactory.register_webserver` ([`evennia/server/portal/service.py:200-237`](../../venv/Lib/site-packages/evennia/server/portal/service.py#L200)) — specifically nested in the `for proxyport, serverport in settings.WEBSERVER_PORTS:` loop that builds the HTTP reverse-proxy. The structural consequence: `WEBSERVER_ENABLED` controls *both* the HTTP stack *and* the WebSocket as a single unit. Setting `WEBSERVER_ENABLED = False` cleanly disables the HTTP reverse-proxy, the AJAX webclient, the `WEB_PLUGINS_MODULE` hook chain — and also the WebSocket. There is no upstream toggle to keep the WebSocket while dropping the HTTP serving.

This is awkward enough that it deserves a paragraph of architectural commentary. The webclient WebSocket is a self-contained `WebSocketServerFactory` running on a dedicated port (`WEBSOCKET_CLIENT_PORT`, distinct from any HTTP port), speaking its own protocol, sharing no state with the HTTP reverse-proxy or the Django views. Functionally it is at the same level as the telnet protocol and the SSH protocol, which Evennia *does* register as top-level Portal services (see `register_telnet` and `register_ssh` immediately preceding `register_webserver` in the same file). The bundling of WebSocket-into-webserver appears to be incidental to the original layout of `register_webserver` — convenient because the WebSocket and the HTTP proxy happen to share an interface list and a lockdown check, not because of any structural reason they belong together. The library would prefer `WebSocketServerFactory` lived alongside telnet and SSH at the top level and was gated on its own `WEBSOCKET_CLIENT_ENABLED` setting; absent that, the library extracts the WS portion and registers it independently via the plugin module hook.

**Risk on Evennia upgrade:**

- **`register_webserver` body restructured.** The plugin reproduces lines 222-237 of `register_webserver` (the WebSocket factory + `TCPServer` + `setServiceParent`). A new version of Evennia that changes how the WebSocket factory is built — different protocol class wiring, different sessionhandler attachment, additional setup steps — would cause our reproduction to drift. Failure mode: WebSocket starts with mis-configured factory, sessions don't auth correctly. How to check: diff lines 222-237 of upstream `register_webserver` against the body of `start_plugin_services`.
- **Evennia decouples WS from `register_webserver`.** The desirable outcome — the library could delete the plugin entirely. Detection: `WEBSOCKET_CLIENT_ENABLED` becomes meaningful as a top-level switch; `register_webserver` no longer mentions WebSocket. Action: remove the plugin and the `PORTAL_SERVICES_PLUGIN_MODULES` injection.
- **`PORTAL_SERVICES_PLUGIN_MODULES` removed or renamed.** Evennia drops the plugin hook in favour of a different extension shape. Failure mode: AppConfig.ready() append targets a non-existent setting (via `getattr(...) or []` defaulting), which silently fails. Detection: shards come up without the WebSocket port listening. How to check: `grep PORTAL_SERVICES_PLUGIN_MODULES` in the new Evennia source; verify `PortalServerFactory.register_plugins` still iterates over it.
- **Plugin call timing changes.** Evennia currently calls `register_plugins` after `register_webserver` and before `super().privilegedStartService()` ([line 95-97](../../venv/Lib/site-packages/evennia/server/portal/service.py#L95)). If a future version moves the call to before `register_webserver`, the early-return check `if settings.WEBSERVER_ENABLED: return` still works correctly (we'd no-op rather than double-register). If it moves to after `super().privilegedStartService()` runs, the reactor may already be processing connections — registration ordering could matter for race-free startup. Detection: smoke test, no obvious diagnostic.
- **`evennia.PORTAL_SESSION_HANDLER` re-architected.** The plugin reads this from the `evennia` top-level module to attach as the factory's sessionhandler. If Evennia changes how the Portal session handler is exposed, the plugin's import breaks or attaches the wrong handler. Detection: `AttributeError` at startup, or sessions never reach the Server.

**Risk in consumer override:**

The plugin is gated on `WEBSERVER_ENABLED = False` and is a no-op otherwise. A consumer that runs `WEBSERVER_ENABLED = True` on every process (vanilla Evennia behaviour) is unaffected by the plugin's existence — it loads, runs, returns immediately, registers nothing. So the only consumer-side hazard is for those who actively configure `WEBSERVER_ENABLED = False`:

- **Consumer adds their own entry to `PORTAL_SERVICES_PLUGIN_MODULES`.** Both plugins run in order; ours appends to the list, the consumer's runs alongside. No collision unless the consumer's plugin registers a WebSocket on the same port — in which case Twisted's `TCPServer` will EADDRINUSE on startup. Detection: clear startup error.
- **Consumer overrides `WEBSOCKET_PROTOCOL_CLASS` in addition to `WEBSERVER_ENABLED = False`.** The plugin reads `WEBSOCKET_PROTOCOL_CLASS` at registration time, so consumer overrides compose correctly — same as the vanilla flow.
- **Consumer disables `WEBSOCKET_CLIENT_ENABLED` but expects the WebSocket to still work.** The plugin honours the same gate Evennia's vanilla flow does — `WEBSOCKET_CLIENT_ENABLED = False` means no WebSocket. Consistent posture; not a regression. If the consumer wants to disable the *AJAX* webclient but keep the *WebSocket*, the existing `WEBCLIENT_ENABLED` / `WEBSOCKET_CLIENT_ENABLED` split already supports that on the vanilla path — but on the `WEBSERVER_ENABLED = False` path, only `WEBSOCKET_CLIENT_ENABLED` matters because there's no HTTP webserver to host the AJAX endpoint on anyway.

**Note on `WEBSERVER_INTERFACES`.** Evennia's vanilla WebSocket registration uses `WEBSERVER_INTERFACES` only for cosmetic log formatting (`len(...) > 1` check on the ifacestr). The plugin uses `WEBSOCKET_CLIENT_INTERFACE` directly and skips the cosmetic check — slightly less verbose log lines, no functional change.
