# Progress

A running log of high-level milestones as the project moves from design into build. Each entry is a brief note pointing to whatever artefact (test result, design doc, code change) is the evidence for that milestone. New entries go at the top.

This is not a changelog (use `git log` for that) and not a roadmap (the phasing lives in [archive/evennia-shards-HANDOVER.md](archive/evennia-shards-HANDOVER.md#phased-poc-plan)). It is a thin index of "what has actually happened so far."

> **Note on older entries.** Entries before 2026-05-21 describe milestones in the order they happened. Some describe the earlier four-chokepoint isolation design that has since been replaced by django-multitenant ([tenancy.md](tenancy.md)); they're kept as the historical record of how the library got here, not as a description of how the code looks today.

## Milestones

### 2026-05-21 — Shard partition: django-multitenant adopted

The library's shard partition is enforced via [django-multitenant](https://github.com/citusdata/django-multitenant) 4.1.1. Every query through `ObjectDB.objects` carries `WHERE shard_id IN (current, '*')` automatically; the boundary is in the database, not in Python. See [tenancy.md](tenancy.md) for the live design.

Library entry points: `src/evennia_shards/tenancy.py` — `bootstrap_tenant_context()` sets the process-wide scope based on `SHARDS_ROLE`; `install_tenancy_on_objectdb()` late-binds `TenantMeta`, the three identity properties, wrapped `save` / `_do_update` / `__setattr__`, the global Django query decorators, and the patched `get_queryset` / `bulk_create` on the manager class. The two-element tenant list (`[Shard(current), Shard("*")]`) gives every shard visibility of its own rows plus `"*"`-stamped globals natively via multitenant's IN-filter mode, without subclassing any QuerySet.

`cross_shard_move` uses `qs.update(shard_id=..., db_location_id=...)` for the cross-shard write — bypasses the tenant-column immutability check at the SQL level. Two narrow rule-breaks documented inline at the call site: `with shard_context(None):` for the target-shard validation read, and `object.__setattr__` to sync the in-memory instance after `qs.update`.

`shard_aware_global_search` wraps its `values_list` queryset construction inside `shard_context(None)` to escape the auto-filter and find rows on every shard; the post-resolution instance load stays on the auto-filtered manager (only local matches are loaded).

`make_shard_at_post_login` (shard-side) gates `refresh_from_db` behind an explicit `ObjectDB.objects.filter(pk).exists()` check, because Django's `refresh_from_db` routes through `_base_manager` (unfiltered) and would otherwise silently load a foreign row.

Smoke-tested end-to-end against the three demo gamedirs: login flow → router auto-redirect to character's shard → IC on shard → cross-shard `@tel` between shard0 and shard1 → `cross_shard_dig` on a remote shard → ticket auth + session reattach. Tests: 290 passing in `src/evennia_shards/tests.py`.

The library previously enforced the partition via a four-chokepoint design (`from_db` override, `pre_save` / `pre_delete` signals, `QuerySet.update` override, `shard_writes_allowed_for` bypass). That design is archived at [archive/shard-isolation.md](archive/shard-isolation.md) for historical context.

### 2026-05-20 — `@tel`: cross-shard leave/arrive announces via new `room_msg` bus kind

Closes the announce gap on cross-shard movement. Vanilla's `move_to` fires `announce_move_from` on the source room and `announce_move_to` on the destination room; `cross_shard_move` bypasses `move_to` entirely (atomic DB update + session redirect, no per-room hook firing), so both announces silently dropped — a stationary observer in the source room saw no departure, a stationary observer in the destination saw no arrival.

The earlier framing of "destination-side announce is architecturally not doable" turned out to be wrong. Re-using the same fire-and-forget bus pattern as `flush_from_cache` makes it straightforward: source process tells destination process to run a local `msg_contents` against a foreign room.

Fix has three layers:

- **New bus kind: `room_msg`**. Multicast variant in the `*_msg` family: payload `{"room_pk": <int>, "text": <str>, "exclude_pks": [<int>, ...]?, "from_obj_pk": <int>?}`. Receiver looks up the room and calls `room.msg_contents(text, exclude=..., from_obj=...)`. Sender composes; receiver is dumb. Optional pks (`exclude_pks` / `from_obj_pk`) are hints — pks that don't resolve are dropped silently rather than failing the whole message (losing a hint is strictly better than losing the broadcast). Misroute of the primary `room_pk` trips the chokepoint and follows the standard defer → undeliverable-reply path, same as `obj_msg`.
- **New sender helper: `send_cross_shard_room_message`**. Mirrors `send_cross_shard_message`'s shape: one `values_list` to read the room's `shard_id`; local-fast-path if the room is on this shard (calls `room.msg_contents` directly, no bus hop); otherwise queues a `room_msg` bus row. Optional pks resolved locally on the fast-path, serialised into the payload on the remote path.
- **Wired into `ShardAwareCmdTeleport.func`**. Source-side announce fires synchronously before `cross_shard_move` via the local source room's `msg_contents` (source room is local in this branch, since obj is local). Destination-side announce fires after `cross_shard_move` via `send_cross_shard_room_message` (destination is foreign, so the bus path is what gets exercised). Both gated on `/quiet`. Caller-facing confirmation is **not** gated — fixing a separate inversion bug where our previous `/quiet` implementation silenced the caller, opposite to vanilla. Vanilla's `/quiet` only suppresses room announces; the caller always sees confirmation. Test pinned the corrected behaviour.

A new bus kind was preferred over baking an `announce_arrival` specific kind in for one consumer. The library shouldn't own the announce concept (a game-layer notion) — `room_msg` is infrastructure ("broadcast to a room across shards") and the announce composition is game-layer code in the consumer or in `ShardAwareCmdTeleport.func`. Other future use cases — cross-shard world events, system messages targeting specific foreign rooms, eventual cross-shard chat — share the primitive.

Smoke-tested in the demo gamedirs by leaving an observer character in Limbo on shard0 and teleporting another character (root) cross-shard twice:

| Step | Observer in Limbo sees |
|---|---|
| root teleports Limbo → newroom (shard1) | `root is leaving Limbo, heading for newroom.` |
| root teleports newroom (shard1) → Limbo | `root arrives from newroom.` |

(The "has entered the game" system message that follows is the known cross-shard re-login artifact, unrelated.)

`/quiet` verified separately to suppress both announces while still emitting the caller-facing confirmation.

**Files:** `src/evennia_shards/messagebus.py` (new `_handle_room_msg`), `src/evennia_shards/messaging.py` (new `send_cross_shard_room_message`), `src/evennia_shards/__init__.py` (export), `src/evennia_shards/teleport.py` (announce wiring + `/quiet` fix), `src/evennia_shards/tests.py` (+17 tests: 6 receiver-handler, 6 sender helper, 5 announce wiring; existing `/quiet` test rewritten to assert vanilla-aligned behaviour). `DESIGN/cross-shard-message-bus.md` and `DESIGN/library-integration-risks.md` updated. Suite at 280. Branch: `shard-aware-teleport`.

### 2026-05-20 — `@tel`: obj-side `teleport` lock check added to cross-shard branch

Closes the security-consistency gap that emerged from the dispatch design: the both-targets-local branch delegated to vanilla `super().func()` and therefore honoured vanilla's [`caller.permissions.check("Admin") or obj.access(caller, "teleport")`](https://github.com/evennia/evennia/blob/main/evennia/commands/default/building.py) check at building.py:3922, but the cross-shard branch bypassed `super().func()` entirely and silently skipped the check. A `teleport`-locked obj that was unteleportable locally became teleportable cross-shard — wrong direction.

Fix: copy vanilla's check verbatim into the cross-shard branch, after `/loc` and `/intoexit` refusals and after the same-position short-circuit, before the call to `cross_shard_move`. Three lines, one new comment block. The "intentionally skipped" comment block below was trimmed to mention only `teleport_here` (destination-side), which remains deferred — the destination row lives on the foreign shard, so its lock can't be evaluated locally without a bus round-trip.

Smoke-tested in the demo gamedirs with a non-Admin Builder account (`tim`):

| Setup | Path | Result |
|---|---|---|
| root (superuser), `teleport:false()` on ball | cross-shard | succeeds (Admin bypass) |
| tim (Builder, no Admin), `teleport:false()` on ball | cross-shard | refused with lock message |
| tim (Builder, no Admin), `teleport:false()` on ball | local | refused with **same** lock message (vanilla parity) |

The smoke test surfaced a separate Evennia subtlety worth noting for future test setup: `cmd:perm(Builder)` on `@tel` checks the **account's** permissions via the lockfunc (see [`evennia/locks/lockfuncs.py:163`](file:///c:/Users/micro/Documents/FCM/libraries/evennia-shards/venv/Lib/site-packages/evennia/locks/lockfuncs.py#L163)), whereas our in-func `caller.permissions.check("Admin")` checks the character's. Granting Builder to a character alone leaves the command invisible; the account also needs the permission (`@perm/account tim = Builder`). Two separate permission surfaces, both come into play.

**Files:** `src/evennia_shards/teleport.py` (new check + trimmed skipped-behaviour comment), `src/evennia_shards/tests.py` (3 new tests: lock-blocks-non-admin, Admin-bypass, non-Admin-allowed-when-obj-grants-access; `_FakeCaller` extended with `permissions.check` / `access` surfaces). Suite at 263. `DESIGN/library-integration-risks.md` § `CmdTeleport` updated. Branch: `shard-aware-teleport`.

### 2026-05-20 — `@tel`: trivial vanilla-parity gaps closed

Four small alignments with vanilla `CmdTeleport` / `caller.search`, each independently smoke-tested in the demo gamedirs (root teleporting + ball cross-shard with `me` / `here` / aliases / ambiguous names).

- **Multi-match disambiguation prompt** — both the obj and destination paths in `ShardAwareCmdTeleport.parse` now render the full candidate list (`Multiple matches for 'tavern': #5 (Tavern, shard0), #12 (Tavern, shard1) - specify by dbref.`) instead of a static placeholder. The destination path previously silently fell through to vanilla's "Destination not found." message when multi-match fired; now it raises `InterruptCommand` with the prompt.
- **Same-position short-circuit** — `ShardAwareCmdTeleport.func` checks `obj.db_location_id == dest_pk` before calling `cross_shard_move`, mirroring vanilla's `obj.location == destination` check ([building.py:3917](https://github.com/evennia/evennia/blob/main/evennia/commands/default/building.py)). Pk uniqueness under the single-Postgres bound makes the instance comparison and the pk comparison equivalent. Saves a no-op bus round-trip and emits vanilla's user-visible "is already at" message.
- **Caller-relative specials** — `shard_aware_global_search` now resolves `"me"` / `"self"` (→ caller) and `"here"` (→ `caller.location`) before the SQL path, matching vanilla `caller.search` semantics. Always local by construction — no cross-shard routing needed.
- **Alias matching** — `shard_aware_global_search` now also matches Tag rows with `db_tagtype="alias"`, using the same `Q(...) | (Q(...) & Q(...))` + `.distinct()` pattern vanilla `ObjectDBManager.object_search` uses. Composes correctly with the existing `tag=` / `tag_category=` zone filter (separate `filter()` calls = separate joins). Important convention note: aliases live in `db_tagtype="alias"`, not `db_category="alias"` — confirmed against `AliasHandler._tagtype = "alias"` in `evennia/typeclasses/tags.py`.

After these, name-resolution parity with vanilla `caller.search(global_search=True)` is complete except for fuzzy / partial name matching (the regex fallback when exact fails). Teleport-side gaps that remain are documented but not yet closed: cross-shard `/loc`, cross-shard `/intoexit`, foreign-obj move, `announce_move_from` / `announce_move_to`, obj `teleport` lock check, destination `teleport_here` lock check.

**Files:** `src/evennia_shards/search.py` (specials short-circuit + alias predicate + scope-note update), `src/evennia_shards/teleport.py` (`_format_multiple` helper + multi-match dispatch + already-at short-circuit), `src/evennia_shards/tests.py` (+14 tests; 260 total). `DESIGN/shard-aware-search.md` and `DESIGN/library-integration-risks.md` § `CmdTeleport` updated. Branch: `shard-aware-teleport`.

### 2026-05-20 — `cross_shard_move` invalidates destination's idmapper via `flush_from_cache` bus message

Closes the contents-cache-stale issue that surfaced when an unpuppeted
object (an item, in this case) was teleported cross-shard: the
destination room's in-process `contents_cache` on the destination
shard would keep its pre-move view, and `look` / `room.contents`
on the destination would omit the just-arrived obj even though the
DB row was correctly stamped.

Root cause: `cross_shard_move`'s `obj.save()` sets
`_safe_contents_update = True` to suppress Evennia's post-save
signal (which would otherwise dereference the now-foreign
`db_location` and trip the chokepoint). The suppression is
mandatory, but it also means neither the source room's nor the
destination room's contents_cache gets updated by the signal. The
source side stays consistent for free via the source process's
`flush_from_cache` on the moved obj — but the destination process
never sees that eviction.

Fix: a new `flush_from_cache` message kind on the cross-shard
message bus, with payload `{"pks": [<int>, ...]}`. Receiver iterates
the pks and, for each that's currently in this process's `ObjectDB`
idmapper, calls `instance.flush_from_cache(force=True)` — same
mechanism Evennia uses for its own idmapper management. Pks not
cached are no-ops; the handler is idempotent.

`cross_shard_move` step 8 (new): after the per-session redirect
block, send `flush_from_cache` to `target_shard` with the
destination row's pk. Gated on `target_shard != current_shard`
(the bus refuses same-shard sends). Send failure is logged but
doesn't roll back the move — worst case the destination shows
stale contents until the room is next evicted for some other reason.

Naming intent: the message is named after the **mechanic** (what the
handler does) rather than the **trigger** (cross-shard arrival). Any
future cross-shard mutation that other shards should drop their
cached view of — `delete`, mass attribute changes, world rebuilds —
can publish the same kind without needing a new message type.

Verified live in the demo gamedirs: ball moved cross-shard to a
previously-loaded destination room is visible in `look` and in
`room.contents` immediately on arrival.

**Files:** `src/evennia_shards/messagebus.py` (handler), `src/evennia_shards/handoff.py` (sender + docstring), `src/evennia_shards/teleport.py` (debug-message detection hook removed; no longer load-bearing), `DESIGN/cross-shard-message-bus.md` (new kind documented). Branch: `shard-aware-teleport`.

### 2026-05-20 — `cross_shard_character_move` renamed `cross_shard_move`

The primitive has always worked on any `ObjectDB`-derived row, not just characters — the `_character_` segment in the name was load-bearing as documentation but actively misleading as the API surface. With `CmdCrossShardMove` gone, the shorter `cross_shard_move` no longer competes with anything for the namespace. Pure rename: docstrings polished to drop "character"-specific language; the `CrossShardCharacterMoveTests` test class renamed to `CrossShardMoveTests`. 204 tests passing.

### 2026-05-20 — `CmdCrossShardMove` removed; `@tel` is the in-game entrypoint

With [`ShardAwareCmdTeleport`](library-integration-risks.md#cmdteleport--narrow-override-delegate-to-vanilla-when-local) landing — vanilla `@tel` that transparently dispatches local or cross-shard via the same `cross_shard_move` primitive — the dedicated `cross_shard_move` admin command became redundant. Two ways to do the same thing forces admins to remember which one to use; one way is cleaner. `CmdCrossShardMove` removed from the library; the matching `CmdCrossShardMoveTests` removed; `AdminCommandAutoInstallTests` no longer asserts the key; the demo-gamedir stub-comment updated.

The `cross_shard_move` primitive itself is unchanged — every consumer that was previously calling it directly (FCM, the library's own `@tel` override) continues to. What's gone is only the in-game command surface that wrapped it.

Migration for consumers: anywhere `cross_shard_move <shard> <room_pk>` was typed in-game, type `@tel #<room_pk>` instead. The override resolves the target's shard from the dbref and routes the move via the same primitive.

**Files:** `src/evennia_shards/commands.py` (CmdCrossShardMove deleted), `src/evennia_shards/apps.py` (no longer auto-installed), `src/evennia_shards/tests.py` (CmdCrossShardMoveTests deleted), `examples/demo_shard0/commands/command.py` (comment updated). Branch: `shard-aware-teleport`.

### 2026-05-18 — `CmdCrossShardMove` promoted from demo into library

The cross-shard movement admin command had been living in
`examples/demo_shard0/commands/command.py` since the
`cross_shard_move` spike, marked `TEMPORARY`. Promoted into
[`evennia_shards/commands.py`](../src/evennia_shards/commands.py)
alongside `CmdShardCheck` and `CmdCrossShardDig`: same Developer lock,
same "Shard Management" help category, auto-installed into
`CharacterCmdSet` via the existing `at_cmdset_creation` patch in
`AppConfig.ready()`.

The command is a thin wrapper around the
`cross_shard_move` primitive — validates `target_shard` is
in `SHARD_URLS`, parses `room_pk` as int, calls the primitive, prints
the `MoveResult`. The primitive itself is what's load-bearing; the
command is just an admin-facing entrypoint that doesn't require the
consumer to ship one of their own for bootstrap-time use.

**Why now.** FCM (first real consumer) hit the chicken-and-egg of
"shard1 has no rooms, can't log in, can't `cross_shard_dig` from
inside it." `CmdCrossShardDig` solves the room-creation half from
shard0; this command solves the "now actually walk over there" half.
The pair give an admin everything needed to bootstrap content on a
fresh shard without consumer-side scaffolding.

**Consumer notes.** This is an *admin tool*, not a player-facing
movement mechanism. Consumer games that want IC cross-shard
movement still need to write their own `CrossShardExit` typeclass
(or equivalent) that gates the primitive with their safe-state
predicate — see [consumer-constraints.md § Cross-shard movement
requires a safe character state](consumer-constraints.md#cross-shard-movement-requires-a-safe-character-state).

**210 tests passing** (204 prior + 6 new in
`CmdCrossShardMoveTests`): no-args usage, one-arg usage,
non-integer room_pk, unknown shard validation, happy-path primitive
delegation (mocked), primitive exception surfaces as error message.
The `AdminCommandAutoInstallTests` assertion was extended to
include the new command key.

**Files:** `src/evennia_shards/commands.py` (new command),
`src/evennia_shards/apps.py` (cmdset patch adds it), demo's
`commands/command.py` and `commands/default_cmdsets.py` (spike copy
removed). Branch: `cross-shard-move-cmd`.

### 2026-05-18 — Local multi-process testing on Windows: no view dirs needed

Empirical finding while preparing FCM as the library's first consumer.
On Windows, three Evennia processes (router + shard0 + shard1) can be
started from the same `demo_shard0/` gamedir in three different
PowerShell terminals, each with `--settings settings_<role>`, with no
view directories, no symlinks, no junctions, and no patching of the
launcher. All three came up cleanly and listened on their per-role ports.

**Why it works.** Evennia's launcher gates the `--pidfile` argument
passed to `twistd` on `os.name != "nt"`
([`evennia_launcher.py:529-532`](../../venv/Lib/site-packages/evennia/server/evennia_launcher.py#L529)).
On Windows that branch is skipped: `twistd` is never told to write
`<gamedir>/server/server.pid` or `<gamedir>/server/portal.pid`, so two
`evennia start` invocations from the same folder don't fight over those
files. `evennia stop` on Windows uses Win32 console-group signals
(`GenerateConsoleCtrlEvent`, line 1618-1631) instead of PID-file lookups
— so each terminal stops its own processes by virtue of being its own
console.

**What this prevented building.** Earlier in the session we sketched a
cross-OS `evennia_shards.local_views.create_view()` helper that would
build view gamedirs via directory junctions on Windows and symlinks on
POSIX, plus a wrapper CLI variant that would patch `init_game_directory`
to read PID paths from settings, plus an `AppConfig.ready()`-time
wrapper around `_get_twistd_cmdline` to inject role-suffixed PID paths.
All three are dropped — the underlying problem (PID-file collision in a
shared gamedir) only exists on Unix, and on Unix the demo's existing
symlinked-view recipe already covers it.

**Decision: documentation, not code.** The OS-specific recipes are
captured as the canonical reference in
[deployment-topology.md § Local development](deployment-topology.md#local-development),
with [shard-settings.md](shard-settings.md#localhost-multi-instance-game-directories)
and [`examples/README.md`](../examples/README.md) pointing back there
for the rationale. The previously-conflicting claims in those two docs
(deployment-topology said "same folder works", shard-settings said
"symlinked view dirs needed") are now resolved as the Windows and
Unix cases of one underlying split.

No library code changed. No new tests. The discovery does, however,
trim a substantial would-have-been workstream from the roadmap.

### 2026-05-04 — Shards run WebSocket-only: HTTP webserver router-only by default

Shards no longer run an HTTP webserver. Default deployment shape is now:

- Router: `WEBSERVER_ENABLED = True` — serves the webclient page, the website, the static-asset pipeline, the Django admin.
- Shards: `WEBSERVER_ENABLED = False` — only the AMP and WebSocket ports listen.

Reason this was non-trivial: Evennia 6.0.0 registers the webclient WebSocket *inside* `PortalServerFactory.register_webserver` (nested in the loop that builds the HTTP reverse-proxy). Setting `WEBSERVER_ENABLED = False` cleanly disables the HTTP stack but takes the WebSocket down with it. The WebSocket has no architectural reason to be coupled to the HTTP webserver — it's a separate Twisted service on a separate port speaking a different protocol — so the library extracts the WS-registration block and runs it independently via the `PORTAL_SERVICES_PLUGIN_MODULES` hook.

**Library piece:** `evennia_shards/portal_services.py` exposes `start_plugin_services(portal)` which registers the WebSocket factory standalone when `WEBSERVER_ENABLED = False`. No-op when the webserver is enabled (Evennia's normal flow handles it). `AppConfig.ready()` auto-appends the plugin module to the setting; consumer needs no wiring.

**Generalises symmetrically.** Same plugin works on the router too: a consumer running their website on a separate service entirely (Next.js, static site host, separate Django) can flip `WEBSERVER_ENABLED = False` on the router as well. Library treats router and shard symmetrically; the only invariant is "exactly one place serves the webclient page somewhere."

**Settings shape:**
- Router (`settings_router.py`): `WEBSERVER_ENABLED = True` (explicit, matches Evennia default; documents intent).
- Shards (`settings_shard0.py`, `settings_shard1.py`): `WEBSERVER_ENABLED = False`.

**204 tests passing** (199 prior + 5 new in `StartPluginServicesTests`): no-op when webserver enabled, no-op when WS deliberately disabled, no-op when port missing, registers WS standalone in the happy case, honours `LOCKDOWN_MODE` for interface forcing.

**Files:** new `evennia_shards/portal_services.py`; `evennia_shards/apps.py` appends to `PORTAL_SERVICES_PLUGIN_MODULES`; demo `settings_router.py` / `settings_shard0.py` / `settings_shard1.py` set `WEBSERVER_ENABLED` explicitly. Doc updates in `DESIGN/deployment-topology.md` (new "HTTP webserver topology" section), `DESIGN/library-integration-risks.md` (new coupling section for the plugin), `DESIGN/shard-settings.md` (recommended `WEBSERVER_ENABLED` per role).

### 2026-05-04 — Refresh handling completed (refresh-while-IC, refresh-while-OOC, post-restart)

Follow-on to the WebSocket-level redirect milestone below. The PoC ship covered IC/OOC transitions but left refresh handling broken in several modes. This milestone closes those out and lands a clean architecture for OOC-intent persistence.

**Two-flag OOC-intent mechanism.** The naive single-flag design (Portal writes `account.db._shards_at_ooc_menu` on ticket auth, Server reads it in `at_post_login`) failed silently because Portal and Server are separate processes with independent `AccountDB` / `Attribute` idmappers — Portal's write was invisible to Server's read. Fixed by splitting:

- `protocol_flags["SHARDS_TICKET_AUTHED"]` — transient Portal→Server bridge. Portal sets it on ticket-bearing connections to the router; Evennia AMP-syncs it onto the Server's session.
- `account.db._shards_at_ooc_menu` — persistent intent. Server-only writer in two places: `shard_aware_at_post_login` (sets True when it sees the protocol flag) and `ShardAwareCmdIC.func` (sets False on `@ic`). Same Server process owns both write-points and the read in subsequent `at_post_login`s, so coherent.

`_redirect_to_character_shard` is now flag-neutral — it can run from a shard's Server during `cross_shard_move` without creating a cross-process write the router would never see.

**Refresh-while-IC routes directly to the shard.** Browser refresh re-fetches the webclient page from the router, but the JS opens its WebSocket directly to the shard via localStorage routing. `ShardRedirectScriptMiddleware` injects an inline `<script>` immediately before `evennia.js`'s `<script>` tag (regex match on the rendered HTML); the inline script reads `localStorage["evennia_shards_last_target"]` and `PerformanceNavigationTiming.type === "reload"` synchronously, then overrides `window.wsurl` directly. `Evennia.init` reads `window.wsurl` ~500ms later inside `WebsocketConnection`, by which time our override has taken effect. No router round-trip on refresh, no flash through the router OOC menu.

**Earlier failed approaches captured for the next session bumping into them:** an `_auth_user_id`-based restoration of `webclient_authenticated_uid` in our `onOpen` was tried and shipped, then deleted once the early `window.wsurl` override made it redundant. The Django HTTP middleware's `make_shared_login` already populates `webclient_authenticated_uid` on every page-load HTTP request to the webclient; with refresh routing direct to the shard, the shard reads that just-written value. WS-side restoration was working around a problem the early-override eliminates upstream.

**JS-side URL augmentation.** The OOB `shard_redirect` URLs the server emits are `?ticket=XXX` only, but Evennia parses the first `&`-separated chunk as csessid — for `?ticket=XXX` URLs, that gives the literal string `'ticket=XXX'` as csessid, which fails the destination's csession lookup. Added an `ensure_csessid_in_url` helper in `shard_redirect.js` that prefixes `<csessid>&<cuid>&<browserstr>` before the existing query, so the destination's csessid auth has a real Django session key to look up. Idempotent: refresh-routing URLs (already built with csessid first) skip the augmentation cleanly.

**Outcome.** Full IC ↔ OOC ↔ refresh matrix verified live: `@ic`, `@ooc`, refresh-while-IC, refresh-while-OOC, refresh-after-server-restart, both `AUTO_PUPPET_ON_LOGIN=True` and `False`. 199 tests passing.

**Files:** `evennia_shards/protocols.py`, `evennia_shards/hooks.py`, `evennia_shards/commands.py`, `evennia_shards/handoff.py`, `evennia_shards/middleware.py` (early inline `window.wsurl` override injection), `evennia_shards/static/evennia_shards/js/shard_redirect.js` (URL augmentation helper, removed obsolete late refresh-routing block), `evennia_shards/tests.py` (199 tests). Doc updates in `DESIGN/ticket-auth-flow.md`. Branch: `websocket-level-redirect-poc`.

### 2026-05-04 — WebSocket-level cross-shard redirect (replaces page navigation)

Cross-shard transitions (`@ic`, `@ooc`, `cross_shard_move`) now operate at the WebSocket connection layer instead of via full-page navigation. When the server emits a `shard_redirect` OOB, the JS plugin closes the current WebSocket and opens a new one to the destination's WS URL with the ticket as a query parameter; the browser page itself does not reload. UI state, scrollback, plugins, command history all persist across the transition.

Architectural rationale beyond the immediate UX win:

- **Protocol-agnostic shape.** "Close the existing connection, open a new one to a target host:port with a single-use auth token" is the same pattern that telnet (via GMCP), SSH (via reconnect notice), and MUD-client redirect support would use. The WebSocket implementation here is one expression of a universal connection-level redirect; the deferred non-web-protocol question (long-standing in [open-questions.md](open-questions.md)) now has a concrete shape to build on rather than a hypothetical.
- **Removes the public-shard-URL UX problem.** A page-navigation redirect required shards to serve an HTTP webclient, which created an orphan-URL UX gap if anyone hit a shard's URL directly without a ticket. With connection-level redirect the player's browser only ever loads the router's webclient; shards are reached only via WebSocket. This positions a future architecture where shards run WebSocket-only (no Django HTTP at all) — smaller attack surface, no orphan URLs, simpler deployment.
- **Cleaner mental model.** The library's URL settings (`SHARD_URLS`, `ROUTER_URL`) became WebSocket URLs, not HTTP URLs — they were only ever used in two places (the redirect URL constructions), so the semantic shift was contained. Setting names unchanged; values changed from `http://...` to `ws://.../`.

**Connection-lost flash polish.** A naive WS swap fires the `connection_close` event, which webclient_gui handles with a misleading "The connection was closed or lost." message. Suppressed by wrapping `Evennia.emitter.emit` once at module load: a `deliberate_transfer` flag is set just before the deliberate close, and the wrapper swallows the next `connection_close` event when the flag is set. Real disconnects continue to render normally — the flag is consumed on first use, with a 5s safety timeout.

**Behaviour change at PoC ship time:** with the URL bar no longer carrying the ticket post-redirect, the original `SHARDS_TICKET_AUTHED` per-WebSocket flag did not persist across page refresh. The follow-on milestone above resolves this with the two-flag OOC-intent mechanism (transient `protocol_flags["SHARDS_TICKET_AUTHED"]` Portal→Server bridge + persistent `account.db._shards_at_ooc_menu` Server-only attribute) — `@ooc` remains a sticky preference across refresh / reconnect / next-day login until cleared by `@ic`.

196 tests passing on the PoC branch at this checkpoint (URL strings updated from `http://` to `ws://` everywhere). Live smoke verified: IC, OOC, and `cross_shard_move` all transition cleanly with page persistence; the connection-lost flash is gone; real disconnects still render the standard error message. Refresh handling completed in the follow-on milestone above.

**Files:** `evennia_shards/handoff.py` (`_redirect_to_character_shard` builds WS URL), `evennia_shards/commands.py` (`ShardAwareCmdOOC` builds WS URL), `evennia_shards/static/evennia_shards/js/shard_redirect.js` (rewritten for WS swap + emit-wrap suppression), demo gamedirs settings updated to `ws://` URLs. Docs: `DESIGN/shard-settings.md`, `DESIGN/ticket-auth-flow.md`, `DESIGN/library-integration-risks.md` updated for the new mechanism. Branch: `websocket-level-redirect-poc`.

### 2026-05-03 — Cross-shard messaging primitives: `obj_msg` and `account_msg`

First piece of cross-shard player-facing messaging. Two new library-shipped bus kinds, both at the same architectural level as the existing `ping` / `ping_received` / `undeliverable_reply` handlers in `messagebus.py`:

- **`obj_msg`** — payload `{"pk": <ObjectDB pk>, "kwargs": <dict>}`. Receiver does `ObjectDB.objects.get(pk=...).msg(**kwargs)`. Evennia's own `Object.msg` handles local session fanout (covers `MULTISESSION_MODE 2/3`). Generic over puppetable `ObjectDB` rows — characters, vehicles, possessed objects, NPCs.
- **`account_msg`** — same shape, targeting `AccountDB`. Required for OOC delivery and for brand-new / never-yet-IC accounts that have no character.

Sessions don't survive cross-process (no `SessionDB`, `sessid` is local to one Portal/Server pair), so the lowest *cross-shard* primitive is one level up from Evennia's own `ServerSession.data_out`: it operates on a row pk and lets the receiving shard's local Evennia handle session fanout via `target.msg(**kwargs)`.

**Target-gone semantics:** if the row no longer exists at receive time, log a warning and consume silently. The bus is real-time only — deferring won't bring a deleted target back, and routine "logged off / chardelete between send and receive" doesn't justify an `undeliverable_reply` round-trip.

**Misroute safety:** if an `obj_msg` arrives at the wrong shard, `ObjectDB.objects.get` trips the `from_db` chokepoint with `ShardIsolationError`, which `process_inbox`'s except branch treats as defer — eventually triggering `undeliverable_reply` to the sender. Loud failure on misroute, by construction.

**Deliberate non-scope:** this milestone lands the primitive layer only. No sender-side helper (`send_cross_shard_message`), no typeclass filter, no local-vs-remote dispatch, no specialised wrappers (cross-shard tells, channels, room broadcast). All deferred to a separate "helper layer" plan once the primitives are proven. JSON-payload constraint means `from_obj=` (a common `Object.msg` kwarg pointing at an `ObjectDB`) needs sender-side rendering before the helper layer ships.

**Follow-on (same day):** `send_cross_shard_message(target_pk, kwargs, target_typeclass=None)` shipped later 2026-05-03 (commit `59ae37f`), covering the sender helper, typeclass filter, local-vs-remote dispatch, and single `.values_list` lookup. Specialised wrappers (cross-shard tells, channels, room broadcast) and the AccountDB-side routing question remain deferred — the latter as an explicit open question (see [open-questions.md](open-questions.md)).

183 tests passing (177 prior + 6 new in `MessageHandlerTests`): obj_msg/account_msg happy paths, kwargs pass-through (text + OOB + options), target-gone behaviour, `super().handle()` subclass composition.

**Live smoke verified end-to-end:** superuser on shard0 sent `obj_msg` to a character on shard1 (text appeared on B's wire within the polling tick); same primitive shape proven for `account_msg` with an OOC player at the router's character-select menu. Both bus rows correctly deleted by `process_inbox` on truthy handler return.

**Files:** `evennia_shards/messagebus.py` (two new `_handle_*` methods, module-level `log` promotion), `evennia_shards/tests.py` (six new test cases). Doc: `DESIGN/cross-shard-message-bus.md` kinds list extended with semantics for both new kinds.

### 2026-05-03 — Chargen wrapper: stamp `shard_id` from the start-location row

Closes a quiet break in chargen under split deployment. Without intervention, a new character created on the router lands with `shard_id="router"` (auto-stamped by `pre_save`), which is not in `SHARD_URLS` — so going IC fails with no redirect target.

The fix is a shallow wrapper around `Account.create_character` (the converging seam for `CmdCharCreate`, `AUTO_CREATE_CHARACTER_WITH_ACCOUNT`, and the guest path). After vanilla creates the character, the wrapper looks up `db_location_id`'s `shard_id` via `.values_list` (same idiom as `handoff.py:166`) and overwrites the router auto-stamp with the start room's shard. The two rows must agree by definition — the character's location determines its shard. No new setting; no policy decision; just lookup-and-stamp.

**Files:** new `evennia_shards/chargen.py` (`make_shard_aware_create_character` factory), `apps.py` (router-side install). The patch resolves the consumer's configured account class via `class_from_module(settings.BASE_ACCOUNT_TYPECLASS)` rather than patching `DefaultAccount` directly — so a consumer override of `create_character` is wrapped rather than shadowed (same pattern `protocols.py` uses for `WEBSOCKET_PROTOCOL_CLASS`).

Unusable start-location cases (`shard_id` is `None`, `"*"`, or `"router"`) log a warning and leave the character router-stamped — chargen does not fail, but the player will hit the existing broken-IC path so the misconfiguration surfaces twice (log + IC attempt).

`DEFAULT_HOME` is intentionally not handled here. Vanilla `account.create_character` does not set `db_home`; any later cross-shard "send to home" is a runtime move, handled by the existing handoff machinery.

177 tests passing (170 prior + 7 new in `ShardAwareCreateCharacterTests`): happy path, vanilla-returned-None pass-through, unstamped/global/router-owned/no-location cases, and kwargs pass-through. Live smoke pending.

### 2026-05-03 — Inventory recursion + rename to `cross_shard_move`

Renamed `cross_shard_move_to` → `cross_shard_move` to reflect that the primitive is character-shaped (sessions, puppet tags, redirects). Added recursive inventory movement: all contents (items, bags-within-bags, arbitrarily nested) have their `shard_id` bulk-updated and are evicted from the idmapper in the same atomic block as the character. Contents' `db_location_id` is unchanged — parent pk doesn't change across shards. Global (`"*"`) items are left alone.

**Technical approach:** `_collect_all_contents(root_pk)` does breadth-first pk traversal via `values_list` (avoids `from_db`). Single `qs.update` for contents — no bypass needed since items have `shard_id == current_shard`. Idmapper eviction uses `flush_from_cache(force=True)` on cached instances with direct dict-pop fallback.

170 tests passing (164 prior + 6 new inventory tests). Live smoke-tested: flat inventory, nested containers (char → bag → gem), round-trips across shards.

### 2026-05-03 — Pre-emptive session detach: zombie session fix for cross-shard round-trips

Live smoke testing of `cross_shard_move` round-trips (shard0 → shard1 → shard0) exposed a zombie session bug that caused a black screen on the return move. Root cause: Evennia's asynchronous disconnect handler (`unpuppet_object`) runs after the WebSocket close triggered by the redirect, but by that point the character's `shard_id` has been mutated to the target shard and the bypass context has exited — so `pre_save` refuses.

**Full causal chain:**

1. Forward move (shard0 → shard1) commits correctly. WebSocket close fires `unpuppet_object` on the in-memory character whose `shard_id` is now `"shard1"` — `pre_save` refuses, creating a zombie session on shard0.
2. Return move (shard1 → shard0) commits correctly. Player arrives on shard0, `disconnect_duplicate_sessions` finds the zombie from step 1, tries to unpuppet it, `del obj.account → save` fails with the stale `shard_id`. AMP exception kills the entire `portal_connect`. `at_post_login` never fires. Black screen.

**First fix attempt (calling `unpuppet_object` inside bypass) failed.** Evennia's `at_post_unpuppet` hook does `self.db.prelogout_location = self.location`, which dereferences the location FK to the room on the target shard. The room is NOT in the bypass set, so `from_db` refuses.

**Working fix: minimal session detach.** Instead of calling Evennia's full `unpuppet_object`, `cross_shard_move` now clears `session.puppet = None` and `session.puid = None` for each puppeting session, and removes the `"puppeted"` tag from the character. This prevents the disconnect handler from entering the puppet cleanup path (its `if obj:` guard finds `None`), and prevents `server_maintenance` from trying to `from_db` a now-foreign row via `get_by_tag("puppeted")`.

**Why minimal detach is safe:** The destination shard's `puppet_object` overwrites `db_sessid` and `db_account` when the player arrives, so stale values in those fields are harmless. The skipped hooks (`at_pre_unpuppet`, `at_post_unpuppet`) are not needed because the character is leaving this process entirely.

**Files changed:** `handoff.py` (step 5 replaced: `unpuppet_object` call → minimal session detach), `tests.py` (`unpuppet_object` on `_FakeAccount`, `remove` on `_FakeSessionHandler`, `flush_from_cache` stub on `_FakeCharacter`).

164 tests passing. Full round-trip verified live: forward move, return move, OOC/IC cycling, cross-shard dig, all clean.

### 2026-05-03 — Idmapper / Attribute-cache staleness fix for cross-shard moves

Live smoke testing of `cross_shard_move` (shard0 → shard1 → shard0 round-trip) exposed two bugs caused by Evennia's in-memory caching defeating cross-process DB updates. Both share the same root cause: when one process updates a row's `shard_id`, other processes' caches still hold the old value.

**Bug 1: Router IC command redirects to wrong shard.** After moving a character from shard0 to shard1, going OOC back to the router, then typing `ic` — the router redirected to shard0 (old `shard_id`) instead of shard1.

- **Root cause:** Evennia's idmapper (`SharedMemoryModelBase.__call__`) returns the cached instance from `from_db()`, ignoring fresh DB values. This makes `refresh_from_db()` a no-op for cached instances.
- **Fix:** Added `flush_from_cache(force=True)` before `refresh_from_db()` in `ShardAwareCmdIC.func()` and `_is_redirectable_character()`.

**Bug 2: Black screen on return move to shard.** After a shard0 → shard1 move, then a shard1 → shard0 return move, the client arrives on shard0 but gets a blank screen. Portal log: `pre_save refused: shard 'shard0' cannot persist Character pk=1 owned by shard 'shard1'`.

- **Root cause:** The Account's Attribute-handler cache on shard0 still held the Python object from the outbound move (whose `shard_id` field was `"shard1"`). Evennia's default `at_post_login` read `_last_puppet` from this stale cache and handed the stale object to `puppet_object`, which tried to save it — tripping the `pre_save` chokepoint.
- **Fix:** Installed a thin `at_post_login` wrapper on shards (`make_shard_at_post_login` in `hooks.py`) that flushes the `_last_puppet` character from the idmapper and refreshes its fields from the DB before delegating to Evennia's original `at_post_login`.

**General pattern documented:** Any code path that reads an `ObjectDB` field which may have been updated by another process must use `flush_from_cache(force=True)` + `refresh_from_db()` before acting on the value. See [shard-isolation.md](archive/shard-isolation.md#cross-process-cache-staleness) for the full write-up.

**Files changed:** `hooks.py` (flush+refresh in `_is_redirectable_character`, new `make_shard_at_post_login` factory), `commands.py` (flush+refresh in `ShardAwareCmdIC.func()`), `apps.py` (shard-side `at_post_login` wrapper installation alongside existing router override), `tests.py` (`flush_from_cache` stub on `_FakeCharacter`).

**Docs updated:** [ticket-auth-flow.md](ticket-auth-flow.md) (shards no longer use vanilla `at_post_login`), [library-integration-risks.md](library-integration-risks.md) (shard wrapper added to `at_post_login` coupling section), [shard-isolation.md](archive/shard-isolation.md) (new "Cross-process cache staleness" section).

164 tests passing.

### 2026-05-02 — `cross_shard_move` spike 1: character move (unit-tested)

The cross-shard handoff primitive landed in [`evennia_shards/handoff.py`](../src/evennia_shards/handoff.py). Initial scope: move a single character row across shards (inventory recursion added 2026-05-03 — see milestone above), with proper composition of the three primitives the handoff needs (atomic DB writes via the chokepoint bypass, idmapper eviction, per-session ticket+redirect).

The primitive composes:

1. Validate `target_shard` is configured and `target_location_pk` exists on it.
2. Atomic DB writes + idmapper eviction inside one `transaction.atomic()` block. Save and `flush_from_cache` are inside the bypass; on any exception, a defensive second eviction runs in the `except` branch so a rolled-back move doesn't leave the in-memory `obj` (whose `shard_id` was already mutated) lingering in the idmapper.
3. Per-session redirect via `_redirect_to_character_shard(session.account, session, obj)`. Uses the session's authenticated account (via `session.account`, not the character's FK) — more accurate semantically and decoupled from the character's `db_account` FK descriptor. Per-session failures captured in the returned `MoveResult`; the move itself doesn't roll back.

Three findings worth recording for future work:

- **`session.account`, not `obj.account`.** The session is the canonical source of "the account doing the move" — independent of the character's FK and consistent across multisession modes.
- **`_safe_contents_update` flag suppresses Evennia's post-save contents-cache update**, which would otherwise dereference `self.db_location` (the target room on the remote shard) and trip the `from_db` chokepoint. Same flag Evennia itself uses for analogous location-change paths.
- **Test setup uses `obj.__dict__["sessions"] = ...`** to shadow the lazy_property descriptor without going through Evennia's protective `__setattr__`. Real `ObjectDB` for the chokepoint-exercising parts; fake session handler for the redirect-counting parts.

156 tests passing (148 prior + 8 new in `CrossShardCharacterMoveTests`): no-sessions, one-session, multi-session, target-shard-not-configured, target-location-doesn't-exist, target-location-on-wrong-shard, atomic-rollback-on-save-failure, session-redirect-failure-captured.

**Deferred:** generalisation to non-character objects (same machinery, no session redirect needed), contrib-layer typeclasses (`CrossShardExit`, `CrossShardCmdTeleport`).

### 2026-05-02 — Shard isolation refactor + `shard_writes_allowed_for` bypass primitive

The shard isolation mechanism was reorganised into a dedicated module and gained the long-anticipated bypass primitive — together they're the foundation Phase 2's `cross_shard_move` will be built on.

**Refactor.** The four chokepoints (`pre_save`, `pre_delete`, `from_db`, `QuerySet.update`) were extracted from `apps.py` into [`evennia_shards/isolation.py`](../src/evennia_shards/isolation.py). `apps.py` now calls a single `install_chokepoints()` entry point. Pure relocation, no behavioural change — the existing chokepoint test suite (~30 cases) passed without modification.

**Bypass primitive.** [`shard_writes_allowed_for(*objs)`](../src/evennia_shards/isolation.py) — a context manager that lifts the chokepoints for specific objects within a `with` block. Tracks identity two ways:

- `id(instance)` — checked by `pre_save` and `pre_delete` (instance-receiving chokepoints). Works for unsaved rows.
- `(concrete_model, pk)` — checked by `from_db` and `QuerySet.update` (which receive class + pk, not the instance). Normalised via `_meta.concrete_model` so a bypass entered with an Evennia typeclass instance (a Django proxy of `ObjectDB`) matches `from_db` calls where `cls` is `ObjectDB` itself.

Scoped to the `with` block, nesting-safe, exception-safe (cleanup runs in `finally`). Public API, exported from `evennia_shards.__init__`.

Documented in [shard-isolation.md](archive/shard-isolation.md#bypass-shard_writes_allowed_for) — the doc gained a new "Bypass" section explaining the semantics, identity tracking, and composition with `transaction.atomic()` and `flush_from_cache()` for handoff scenarios.

148 tests passing (138 prior + 10 new in `ShardWritesAllowedForTests`): allows-remote-save, scoped-cleanup, no-auto-stamp-of-explicit-id, allows-remote-delete, only-listed-objects, nested-bypass, exception-cleanup, allows-from-db, allows-qs-update, partial-qs-update-still-raises.

### 2026-05-02 — `AUTO_PUPPET_ON_LOGIN = True` path: live smoke-test green

The router-side `at_post_login` override (landed 2026-05-01) verified end-to-end with live smoke testing under both auto-puppet modes. Two corrections were applied during the smoke test cycle:

1. **Portal/Server AMP sync.** First-cut implementation stored the OOC-return signal as a direct attribute (`self._ticket_authed`) on the WebSocket protocol instance. Live testing showed the flag was set on the Portal-side object but read as `False` on the Server side — Python object ids differed between set and read. Cause: Evennia's Portal and Server are separate processes; only attributes listed in `settings.SESSION_SYNC_ATTRS` survive the AMP crossing, and arbitrary attributes don't. Fix: store the flag in `protocol_flags["SHARDS_TICKET_AUTHED"]` instead — `protocol_flags` is in the synced set (it carries `OOB`, `XTERM256`, etc.), so the value reaches the Server intact.

2. **Honour `AUTO_PUPPET_ON_LOGIN = False`.** Smoke testing under `AUTO_PUPPET_ON_LOGIN = False` showed the override was *still* auto-redirecting — vanilla Evennia would render the OOC menu unconditionally in that case. The override was forcing True-shaped behaviour regardless of the consumer's setting (a divergence from Evennia and a violation of the two-audiences principle). Fix: short-circuit at the top of the override after the prelude — when `AUTO_PUPPET_ON_LOGIN` is `False`, render the OOC menu and return without applying any of the library's redirect logic.

After both fixes, both modes work as expected:

| `AUTO_PUPPET_ON_LOGIN` | Behaviour |
|---|---|
| `False` | OOC menu always rendered; library redirect machinery dormant. Vanilla parity. |
| `True` | Auto-puppet path produces a redirect to the character's owning shard; OOC return from a shard lands at the router's OOC menu (no loop). |

Debug instrumentation added during diagnosis (`SHARDS-DEBUG-TICKET-FLAG` markers in `protocols.py` and `hooks.py`) has been removed. **138 tests passing** (137 prior + 1 new `test_auto_puppet_disabled_renders_ooc_menu_unconditionally`).

Phase 1 of the original PoC plan (router + 1 shard, both auto-puppet modes) is now functionally complete and verified live. Phase 2 (cross-shard handoff, gateway primitives, character movement between shards) is the next milestone.

### 2026-05-01 — `AUTO_PUPPET_ON_LOGIN = True` path: router `at_post_login` override

Closes the auth/redirect feature: both `AUTO_PUPPET_ON_LOGIN = True` and `False` paths now work on the router. With auto-puppet=True, login itself triggers the redirect; with False, the player goes through the OOC menu and types `ic <char>`.

`shard_aware_at_post_login` (in [evennia_shards/hooks.py](../src/evennia_shards/hooks.py), new module) replaces `DefaultAccount.at_post_login` on routers via monkey-patch in `AppConfig.ready()`. The override reproduces Evennia 6.0.0's prelude verbatim, then dispatches three ways via the `_is_redirectable_character()` predicate:

| `_last_puppet` state | Outcome |
|---|---|
| set with usable `shard_id` | redirect via `_redirect_to_character_shard(...)` (the helper extracted in commit `946bc2e`, now reused by both the IC command and the login hook) |
| set with broken `shard_id` (`None`, `"*"`, not in `SHARD_URLS`) | log warning, render OOC menu — login does not fail |
| `None` | render OOC menu silently |

Install gating: inline `if get_role() == ROLE_ROUTER:` in `AppConfig.ready()`, alongside the existing CmdIC/CmdOOC patches. Shards originally kept vanilla `at_post_login` — that was their auto-puppet path after ticket-auth. *(Later amended: shards now wrap the original with a cache-busting preamble — see 2026-05-03 milestone.)*

New coupling section added to [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) covering Evennia upgrade and consumer override risks.

`ShardAwareCmdOOC` was extended to clear `account.db._last_puppet` before creating the ticket. Without this, a player on a shard typing `ooc` would land on the router, the router's `at_post_login` would see `_last_puppet` still set, and the player would be auto-redirected straight back to the shard — an infinite loop. The clearing diverges from vanilla `CmdOOC` (which sets `_last_puppet = old_char`); rationale documented in [ticket-auth-flow.md](ticket-auth-flow.md#ooc-command-override). A multi-puppet edge case (modes 2/3) is captured in [open-questions.md](open-questions.md) as an Evennia upstream limitation that the library inherits.

**Post-smoke-test correction (same day):** the `_last_puppet = None` clearing approach failed live testing. Cross-process Attribute writes don't propagate fast enough — the router reads the stale value (or its idmapper / AttributeHandler serves a cached one), redirects back to the shard, and the shard's vanilla `at_post_login` then sees `None` (clear has caught up) and dies with `"The Character does not exist."` Replaced with a per-session signal set in `ShardWebSocketClient.onOpen()` based on URL presence of `?ticket=`, stored in `protocol_flags["SHARDS_TICKET_AUTHED"]` (initially attempted as a direct attribute `self._ticket_authed`, but the Portal/Server AMP sync drops attributes not listed in `SESSION_SYNC_ATTRS` — only `protocol_flags` survives the crossing). The router's `at_post_login` checks the flag before consulting `_last_puppet` — any session whose URL carried a ticket is, by construction, an OOC-return target and gets the OOC menu without auto-redirect. `_last_puppet` is left vanilla; the `_last_puppet = None` mutation in `ShardAwareCmdOOC` was reverted, replaced by a canary asserting we *don't* mutate it. Aligns with the two-audiences principle (minimum divergence from Evennia).

137 tests passing (128 prior + 3 in `AtPostLoginRouterTests` + 1 ticket-flag test in `AtPostLoginRouterTests` + 4 in `TicketAuthedFlagTests` + 1 canary on `ShardAwareCmdOOCShardTests`). Live smoke test pending.

### 2026-05-01 — OOC command override: shard→router redirect proven end-to-end

`ShardAwareCmdOOC` completes the bidirectional ticket flow. A player IC on a shard types `ooc`, the shard creates a ticket targeting the router, and redirects the client back. The router ticket-auths the account back into OOC state. Full round-trip proven with live smoke test (router + shard0, localhost multi-instance).

**What was proven**:

| Step | Result |
|------|--------|
| `ooc` on shard (while IC) | Ticket created with `old_char.id`, `to_shard="router"` |
| `shard_redirect` OOB sent | Browser navigates to router webclient with ticket |
| Router ticket auth | Player is OOC on the router |
| Full round-trip: router → IC → shard → OOC → router | Works end-to-end |

**Implementation**:

- `ShardAwareCmdOOC` (`evennia_shards/commands.py`): subclasses Evennia's `CmdOOC`, overrides `func()`. Character ID fallback chain: `old_char.id` → `_last_puppet.id` → `0` (sentinel with `log_warn`). Always redirects — even error states with no puppet.
- `AppConfig.ready()` monkey-patch: replaces `CmdOOC` on `evennia.commands.default.account` module, gated on `get_role() == "shard"` only. Asymmetric with IC (which patches both router and shard) because vanilla OOC on the router is correct behaviour.
- No explicit unpuppet: page navigation closes WebSocket, Evennia's disconnect handler releases the character automatically.

127 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for the OOC command override design and remaining work (`at_post_login` override for auto-puppet = true).

### 2026-05-01 — Router shard ID: library mandate and accessor

Added `get_router_shard_id()` accessor returning the hardcoded constant `"router"`. This is a library mandate — the router's `SHARD_ID` must be `"router"`, not configurable. Needed by shards to populate `to_shard` in OOC redirect tickets (and any future cross-shard ticket targeting the router).

**What landed:**

- `ROUTER_SHARD_ID = "router"` constant and `get_router_shard_id()` accessor in `config.py`
- Exported in `__init__.py` and `__all__`
- `RouterShardIdAccessorTests` in `tests.py`
- `ROUTER_SHARD_ID` documented in `shard-settings.md` settings table and usage example

121 tests passing.

### 2026-05-01 — IC command override: router→shard redirect proven end-to-end

The `AUTO_PUPPET_ON_LOGIN = False` path is complete. A player logs into the router OOC, types `ic <character>`, and the router resolves the character, creates a ticket, and redirects the client to the character's shard — where ticket auth + auto-puppet puts them IC. Proven with live smoke test (router + shard0, localhost multi-instance).

**What was proven**:

| Step | Result |
|------|--------|
| `ic <character>` on router | Character resolved, `_last_puppet` set, ticket created |
| `shard_redirect` OOB sent | Browser navigates to shard webclient with ticket |
| Shard ticket auth + auto-puppet | Player is IC on the shard |
| `ic` on shard | "Leave this character before trying to enter another one." |

**Implementation**:

- `ShardAwareCmdIC` (`evennia_shards/commands.py`): subclasses Evennia's `CmdIC`, overrides `func()` with role-gated behaviour. Character resolution logic extracted into `_resolve_character()` (copied from parent — can't call `super().func()` since it calls `puppet_object()`).
- `AppConfig.ready()` monkey-patch: replaces `CmdIC` on `evennia.commands.default.account` module. Same injection pattern as WebSocket protocol and middleware overrides.
- Router exemption from shard isolation chokepoints (implemented in previous session) is load-bearing — the router must load characters from any shard to resolve them.

120 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (`at_post_login` override for auto-puppet = true, OOC command override).

### 2026-04-30 — Client redirect spike proven end-to-end

The client-side redirect mechanism from [ticket-auth-flow.md](ticket-auth-flow.md) is validated on the `bespoke` branch. An OOB `shard_redirect` message triggers a full page navigation to the target instance's webclient with a ticket token. The target instance's middleware injects the token into the WebSocket connection, and the auth cascade logs the player in.

**What was proven** (live smoke test on router instance redirecting to itself):

| Step | Result |
|------|--------|
| Server sends `shard_redirect` OOB | JS plugin catches it (direct emitter listener bypasses `default_out.js`) |
| `window.location.href` navigates to target | Browser loads target webclient page |
| Middleware injects `&ticket=TOKEN` into `window.csessid` | Token flows into WebSocket URL query string |
| `onOpen()` auth cascade: session first, then ticket | Ticket validated, auto-login succeeds |
| Page refresh with stale `?ticket=` in URL | Browser session takes priority, stale token ignored |

**Key implementation decisions**:

- **Full page redirect** (not WebSocket-level reconnect): avoids fighting Evennia's private `WebsocketConnection` class, URL construction conflicts (`wsurl + '?' + csessid`), and emitter lifecycle. Browser address bar updates, so refresh stays on the target instance.
- **Auth priority reorder**: browser session checked before ticket (was ticket-first). Load-bearing for page refresh — tickets are single-use, so checking them first would fail on refresh after consumption.
- **Middleware dual role**: injects both the `shard_redirect.js` plugin (always) and the ticket injection script (only when `?ticket=` in page URL). Zero consumer config beyond `INSTALLED_APPS`.

**Components**:

- `shard_redirect.js`: OOB listener → `window.location.href = url`
- `middleware.py`: `ShardRedirectScriptMiddleware` — script tag injection + ticket-to-csessid injection
- `protocols.py`: `onOpen()` refactored from two-phase to single-phase with three-way auth cascade (session → ticket → role gate)

96 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-puppet, IC/OOC command overrides).

### 2026-04-30 — Ticket auth: auto-login spike proven end-to-end

The ticket-based auto-login flow from [ticket-auth-flow.md](ticket-auth-flow.md) is validated end-to-end on the `bespoke` branch. A WebSocket connection arriving with `?ticket=<token>` is intercepted, validated, consumed, and the session is auto-logged-in before `sessionhandler.connect()` fires — so the Server sees `logged_in=True` and `uid` already set, triggering Evennia's built-in `portal_connect()` auto-login path.

**What was proven** (live smoke test with `test_ticket_ws.py` and browser against the demo game):

| Case | Mode | Result |
|------|------|--------|
| Valid ticket | Router | Auto-login (`["logged_in", ...]` received) |
| Valid ticket | Shard | Auto-login (`["logged_in", ...]` received) |
| Invalid/bogus token | Router | Rejected with error + close code 4001 |
| Invalid/bogus token | Shard | Rejected with error + close code 4001 |
| No token (browser) | Router | Normal login screen shown |
| No token (browser) | Shard | Rejected ("this shard requires a ticket") |
| Reused token | Both | Rejected (single-use enforced) |

**Key implementation**: two-phase `onOpen()` override in `ShardWebSocketClient`:

- **Phase 1** (pre-session): extract token from URL, validate ticket, abort on failure — no session registered, no login screen. Role gating: shards reject tokenless connections, routers allow them.
- **Phase 2** (reproduced Evennia `onOpen()`): `init_session()` → inject `uid` + `logged_in` from ticket → `sessionhandler.connect()`. The injection point is between these two calls — the only clean seam (see [library-integration-risks.md](library-integration-risks.md) for rationale and rejected alternatives).

**New helpers**: `_get_client_address()` (proxy-aware IP resolution), `_validate_ticket()` (pure validation, returns `(bool, data|error)`), `_extract_ticket_token()` (URL query parsing), `_send_text()` (Evennia JSON protocol wrapper).

**New files**: `DESIGN/library-integration-risks.md` (documents the `onOpen()` override and what to diff on Evennia upgrades), `settings_router.py` / `settings_shard0.py` (role-specific settings for the demo game).

96 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-puppet, client-side redirect). Note: the two-phase `onOpen()` was later refactored into a single-phase auth cascade — see the "Client redirect spike" milestone above.

### 2026-04-30 — Ticket auth: full validation spike proven

The ticket-based auth flow from [ticket-auth-flow.md](ticket-auth-flow.md) is validated end-to-end on the `bespoke` branch. A WebSocket connection arriving with `?ticket=<token>` is intercepted, looked up by token PK, IP-validated, consumed (single-use), and the result reported to the client.

**What was proven** (live smoke test with `test_ticket_ws.py` against the demo game):

| Case | Result |
|------|--------|
| Valid ticket + matching IP | `Ticket validated` with correct account/character IDs |
| Valid ticket + wrong IP | `Ticket rejected: IP mismatch` |
| Valid ticket + no IP pinning | `Ticket validated` (IP check skipped) |
| No ticket in URL | Normal login screen, no ticket messages |
| Bogus/nonexistent token | `Ticket not found or wrong shard` |
| Reused consumed token | `Ticket not found` (single-use enforced) |

**Components**:

- **`Ticket` model** (`models.py`): `token` (PK), `account_id`, `character_id`, `to_shard`, `client_ip` (nullable, for IP pinning), `created_at`. Migrations `0003` + `0004`.
- **Ticket primitives** (`tickets.py`): `create_ticket()`, `get_ticket()`, `delete_ticket()`.
- **`ShardWebSocketClient`** (`protocols.py`): token extraction from URL query string, ticket validation with IP check, single-use consumption. Dynamic base class preserves consumer customisations.
- **`AppConfig.ready()` wiring** (`apps.py`): protocol override gated on `get_role() != "monolith"`.

90 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for remaining work (auto-login, puppet hook, client-side redirect).

### 2026-04-30 — Ticket auth: protocol override PoC proven

The WebSocket protocol override mechanism from [ticket-auth-flow.md](ticket-auth-flow.md) is wired and proven on the `bespoke` branch:

- **`ShardWebSocketClient`** (`evennia_shards/protocols.py`): subclass of the consumer's configured `WEBSOCKET_PROTOCOL_CLASS` (not Evennia core directly — see "dynamic base class" below). Overrides `onOpen()` to intercept WebSocket connections. Currently sends a PoC message; next step is extracting `?ticket=<token>` from the URL.
- **`AppConfig.ready()` wiring** (`evennia_shards/apps.py`): stashes the consumer's current `WEBSOCKET_PROTOCOL_CLASS` value, then overwrites it to point to `ShardWebSocketClient`. Gated on `get_role() != "monolith"` — monolith mode uses normal login exclusively.
- **Dynamic base class**: `protocols.py` resolves the stashed original class via `class_from_module` at import time and subclasses *that*, preserving any consumer customisations to the WebSocket protocol. The library layers on top rather than replacing.
- **Proven live**: demo game running as `shard` role displays `[evennia-shards] Protocol override active.` on WebSocket connect — before Evennia's own connection screen — with zero changes to the demo game's code.

72 tests passing. See [ticket-auth-flow.md](ticket-auth-flow.md) for the full design and remaining work.

### 2026-04-29 — Cross-shard message bus: primitives + lifecycle land

The bus from [cross-shard-message-bus.md](cross-shard-message-bus.md) is in place on the `bespoke` branch, end-to-end:

- **`Message` model + migration `0002`** (commit `463f2b6`): `id`, `created_at`, `to_shard`, `from_shard`, `kind`, `payload` (JSONField), composite index on `(to_shard, created_at)`. Also `get_message_timeout(kind)` accessor + `SHARDS_MESSAGE_TIMEOUT_DEFAULT` / `SHARDS_MESSAGE_TIMEOUTS` settings, same override pattern as `get_role` / `get_shard_id`.
- **Primitives** — `send_message` (`4511139`), `poll_messages` (`88547b7`), `delete_message` (`7ac2dd5`). Module-level functions in `evennia_shards/messagebus.py`.
- **Same-shard send guard** (`e3a992c`): new `MessageBusError` exception; `send_message` refuses if `to_shard == from_shard` (after defaulting), since the bus is for cross-shard communication and same-shard sends are almost always a misconfigured `SHARD_ID`.
- **Polling cycle + handler hook** (`566bf11`): `process_inbox(handler)` runs one cycle (poll → dispatch → delete on success); `start_message_bus(handler, interval)` wraps it in a Twisted `LoopingCall`. `MessageHandler` base class is the consumer-overrideable hook — subclasses call `super().handle(message)` to compose library handling with their own kind dispatch. Library-shipped kinds: `ping` (replies with `ping_received`), `ping_received` and `undeliverable_reply` (silently consumed in the base). `examples/demo_game/server/conf/at_server_startstop.py` calls `start_message_bus()` from `at_server_start()` when role is non-monolith.
- **Timeout / undeliverable_reply lifecycle** (`779f611`): `process_inbox` now does the three-way decision per message — handler truthy → delete, falsy + age ≤ lifespan → defer, falsy + age > lifespan → insert `undeliverable_reply` to original `from_shard` (with `original_kind`, `original_payload`, `reason="timeout"`) and delete the original. If `from_shard` is missing or equals current shard, log a warning and drop without reply.

60 tests passing in ~1.5s. The bus is primitive-complete; deeper kinds (`character_handoff` for the gateway protocol) are deliberately deferred to Phase 2.

### 2026-04-29 — Bespoke spike: all four chokepoints land with isolated tests

The `bespoke` branch now carries all four chokepoints documented in [shard-isolation.md](archive/shard-isolation.md), with full automated test coverage. The four-chokepoint spike is functionally complete.

- **`pre_save` chokepoint** (commit `80226be`): the existing auto-stamp handler grew a second arm — refuse the save if `instance.shard_id` is set and is neither the current shard nor `"*"`. New `ShardIsolationError` exception type. Live smoke test confirmed via in-game `@py`.
- **`pre_delete` chokepoint** (commit `0bcce76`): mirrors `pre_save` minus the auto-stamp arm. Refuses to delete a row whose `shard_id` is neither current nor `"*"` (and not `None`, since legacy/unstamped rows are tolerated). Covers both `instance.delete()` and `qs.delete()` because Django fires `pre_delete` per affected row even on bulk queryset deletes.
- **`from_db` chokepoint** (commit `c7510ed`): patches `ObjectDB.from_db` from `AppConfig.ready()` with a closure-captured replacement that inspects `shard_id` in the row data before delegating to the original. Refuses construction if `row.shard_id` is set and is neither current nor `"*"`. Inherited automatically by typeclass subclasses (Room, Character, ...) via Python MRO. Covers all three Django call sites: `ModelIterable` (queryset iteration), `RawModelIterable` (raw queries), `RelatedPopulator` (select_related). Idempotent via a marker attribute against dev reload.
- **`QuerySet.update()` chokepoint** (this commit): patches the queryset class returned by `ObjectDB.objects.get_queryset()` so that `update()` runs an upfront `values_list("shard_id")` SELECT to detect any non-owned, non-global rows in the queryset's scope. Raises before issuing the UPDATE if any are found, so owned rows in a mixed queryset are not partially modified. Inner `issubclass(self.model, ObjectDB)` guard so the patched class only enforces for ObjectDB-derived models in case the queryset class is shared.

**Test infrastructure decoupled from `examples/demo_game/`:** `tests/test_settings.py` + `runtests.py` run the suite against an in-memory sqlite database with `evennia_shards` in `INSTALLED_APPS`, using `BaseEvenniaTestCase` to force `evennia.game_template.*` fallbacks. No gamedir needed. See [testing-setup.md](testing-setup.md). 23 tests passing (3 config + 1 app setup + 4 pre_save + 5 pre_delete + 5 from_db + 5 qs.update) in ~0.7s.

**What this proves:**

- All four chokepoints function exactly as designed in `shard-isolation.md` — read, save, delete-instance, delete-queryset, and bulk-update operations on remote-shard rows raise loudly with shard ids in the message.
- The chokepoints compose cleanly: `from_db` catches read-side leaks, the signals catch instance-level writes, and the QuerySet override catches bulk updates. No idmapper subclassing, no broad manager replacement.
- `.values()` / `.values_list()` correctly bypass `from_db` (per design — they return row data without constructing instances), and the `QuerySet.update` chokepoint uses this bypass internally to inspect remote rows for the upfront check.
- The library has a deterministic, hermetic test suite that runs in under a second.

**Beyond the four-chokepoint spike** (Phase 2 / out of scope here):

- ~~Cross-shard ownership handoff and the bypass primitive (`shard_writes_allowed_for(...)`).~~ *Both landed 2026-05-02 — bypass primitive and cross_shard_move spike 1 (single-object move) are working with full unit-test coverage. See milestones above.*
- Backfill migration for legacy NULL rows.
- ~~Revisit the comparison with `django-multitenant` on the parallel `django-multitenant` branch.~~ *Decided in favour of bespoke chokepoints — see [shard-isolation.md](archive/shard-isolation.md#decision-bespoke-chokepoints-vs-django-multitenant). The `django-multitenant` branch was discontinued without merging.*

### 2026-04-29 — Auto-stamp on save works (hybrid pre_save signal)

A pre_save signal handler in `EvenniaShardsConfig.ready()` now stamps `shard_id` to the current process's `SHARD_ID` whenever an `ObjectDB` (or subclass) is saved with `shard_id == None`. Explicit values (e.g. those set during a cross-shard handoff) are respected. Verified end-to-end: after a clean DB wipe + `evennia migrate` + `evennia start`, the bootstrap rows (`#1` superuser character, `#2` Limbo) and a runtime-dug `test` room (`#3`) all reported `shard_id = 'shard0'` via both the ORM and a raw SQL probe.

**Key implementation finding** (worth recording): Evennia's typeclass system uses concrete Django subclasses of `ObjectDB` — `Room`, `Character`, `Exit`, and consumer-defined typeclasses — that all share the `ObjectDB` table. Django dispatches `pre_save` with `sender = type(instance)`, which is the subclass, never the `ObjectDB` base. A naïve `pre_save.connect(handler, sender=ObjectDB)` therefore matches *zero* saves of game-world objects. The fix is to connect without a sender filter and do an `isinstance(instance, ObjectDB)` check inside the handler. Performance cost of the universal handler is negligible (microseconds per save).

**What this proves:**

- Auto-population works for both bootstrap-time saves (via `at_initial_setup`) and runtime saves (via `dig` or any `create_object` path).
- The "if shard_id is None" guard is load-bearing: it lets explicit consumer/library code (cross-shard handoff, central seed scripts) set values that the signal will respect.
- Lazy-backfill side effect: legacy NULL rows would auto-populate on their next save, useful for monolith-to-shard adoption but not a substitute for an explicit migration backfill.

**What this does *not* prove** (next spikes):

- Backfill of pre-existing rows that never save again (the explicit `RunPython` migration is still required for that).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper) — the next big architectural unknown.
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-29 — Migration spike confirmed: `shard_id` column on `ObjectDB` is viable

A small spike proved the foundational partitioning mechanism. Library now ships an `apps.py` AppConfig and a `0001_add_shard_id_to_objectdb` migration; in shard mode the demo game adds `evennia_shards` to `INSTALLED_APPS` via a one-line conditional in `settings.py`. After `evennia migrate`, an in-game `@shard_check` command confirmed both ORM-level (`ObjectDB._meta` knows the field) and database-level (raw `SELECT shard_id` returns) presence of the column on existing rows.

**What this proves:**

- A library-shipped Django migration can add a column to Evennia's `ObjectDB` table via `RunSQL`, anchored to Evennia's own migration history.
- `add_to_class` from `AppConfig.ready()` makes the new field visible to the ORM without a model fork.
- The library can be a Django app conditionally (only when `SHARDS_ROLE != "monolith"`), and the cross-app migration sequencing under `evennia migrate` works without bespoke command flow.
- Consumer adoption is three lines in `settings.py` (`SHARDS_ROLE`, `SHARD_ID`, conditional `INSTALLED_APPS`).

**What this does *not* prove** (next spikes):

- Auto-population of `shard_id` on object creation (pre_save signal mechanism untested).
- Backfill of pre-existing rows (`#1` superuser, `#2` Limbo currently `NULL`).
- Auto-filtering manager composition with Evennia's `SharedMemoryManager` (idmapper).
- Cross-shard `UPDATE` semantics during handoff.

### 2026-04-28 — Case 1 gate re-run with first library code (still satisfied)

Re-ran `evennia test evennia` after the `config.py` accessors landed. Result identical to the previous gate run: 1662 / 2 errors / 38 skipped, same two errors (both missing optional Evennia contrib dependencies, unrelated to evennia-shards). The library's first real code is provably non-perturbing of Evennia's test suite. See [test-history/test_results_3_2026-04-28.md](test-history/test_results_3_2026-04-28.md).

### 2026-04-28 — Config accessor wire proven (live, in-game)

First piece of real library code: [evennia_shards/config.py](../src/evennia_shards/config.py) with `get_role()` / `get_shard_id()` accessors. Settings design documented in [shard-settings.md](shard-settings.md) and load-bearing principle 9 added to [CLAUDE.md](../CLAUDE.md). Wire proven end-to-end with a temporary `@shards_debug` superuser command in the demo game (since reverted): both accessors return the documented defaults when the consumer declares nothing, and return the consumer-declared values when overridden. See [test-history/test_results_2_2026-04-28.md](test-history/test_results_2_2026-04-28.md). Case 1 gate re-run with the new library code is still outstanding.

### 2026-04-28 — Case 1 gate satisfied (empty-library state)

Re-ran `evennia test evennia` with `evennia_shards 0.0.1` installed (`pip install -e .` from repo root). Result identical to baseline: 1662 / 2 errors / 38 skipped, same two errors. Zero delta — the library is genuinely dormant in monolith mode at its current (empty) state. Gate must be re-run after each future change that could execute at import or app-ready time. See [test-history/test_results_1_2026-04-28.md](test-history/test_results_1_2026-04-28.md).

### 2026-04-28 — Baseline test run (vanilla Evennia)

Ran `evennia test evennia` against vanilla Evennia 6.0.0 (with `evennia_shards` *not* installed) to establish the reference for the Case 1 verification gate. Result: 1662 tests, 2 errors (both missing optional contrib dependencies — `xyzgrid` needs scipy, `git_integration` needs GitPython), 38 skipped. See [test-history/test_results_0_2026-04-28.md](test-history/test_results_0_2026-04-28.md).
