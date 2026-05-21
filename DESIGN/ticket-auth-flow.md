# Ticket-Based Authentication Flow

## Overview

When a player goes IC on the router, the router creates a single-use ticket and redirects the client to the target shard. The shard validates the ticket on connection, authenticates the session, puppets the character, and deletes the ticket.

## Flow

```
Router                          Client                          Shard
  |                               |                               |
  |  player types: IC bob         |                               |
  |<------------------------------|                               |
  |                               |                               |
  |  token = create_ticket(       |                               |
  |    account_id, character_id,  |                               |
  |    "shard0")                  |                               |
  |                               |                               |
  |  ws_url = get_shard_url(      |                               |
  |    "shard0")                  |                               |
  |  (a ws:// or wss:// URL)      |                               |
  |                               |                               |
  |  OOB: shard_redirect          |                               |
  |  [ws_url?ticket=T]            |                               |
  |------------------------------>|                               |
  |                               |  shard_redirect.js:           |
  |                               |  1. close existing WebSocket   |
  |                               |     (page stays loaded;       |
  |                               |     scrollback / UI persist)  |
  |                               |  2. open new WebSocket to      |
  |                               |     ws_url?ticket=T           |
  |                               |------------------------------>|
  |                               |                               |
  |                               |  onOpen() auth cascade:       |
  |                               |  1. browser session? (no)     |
  |                               |  2. ticket? validate + login  |
  |                               |  3. puppet character          |
  |                               |  4. delete ticket             |
  |                               |                               |
  |                               |  player is IC, playing        |
  |                               |  (same browser tab, same      |
  |                               |   page — only the WebSocket   |
  |                               |   has changed)                |
  |                               |<----------------------------->|
```

## Bidirectional: not just router→shard

Token auth works in **both directions**:

- **Router → Shard** (going IC): router creates ticket, redirects client to shard with token. Shard auto-logins and puppets the character.
- **Shard → Router** (going OOC): shard creates ticket, redirects client back to router with token. Router auto-logins the account (back to OOC state).

The rule: **if there's a token in the URL, use ticket auth — regardless of role** (except monolith). The token path is universal for any non-monolith instance.

The difference between roles is what *else* is available alongside token auth:

- **Router**: token auth (for returning players) + normal login (for fresh connections)
- **Shard**: token auth only (no login screen, no normal login)
- **Monolith**: normal login only (no token auth, library dormant)

## Key properties

- **Token as primary key**: Single indexed DB lookup on the hot path — no JSON scanning.
- **Single-use**: Ticket is deleted after validation. A second connection with the same token is refused.
- **IP-pinned**: The ticket records the client's IP at creation time. The receiving instance compares it against the connecting client's IP and rejects mismatches. Prevents token theft — an intercepted token is useless from a different IP. The field is nullable, so IP pinning is opt-in (e.g. omitted in test harnesses or when the IP is unavailable).
- **No session transfer**: The receiving instance creates a new session. The token is the only bridge between connections.
- **Same codebase**: The router and shard run identical code. Behaviour differences are gated on `SHARDS_ROLE`.

## Protocol override mechanism

Token extraction happens in a custom `WebSocketClient` subclass wired in via Evennia's `WEBSOCKET_PROTOCOL_CLASS` setting. Two design constraints:

1. **Monolith gating**: The override is only installed when `get_role() != "monolith"`. Monolith mode uses normal login exclusively; the library is dormant.

2. **Dynamic base class**: The library does *not* hardcode Evennia's `WebSocketClient` as the base class. Instead, `AppConfig.ready()` stashes the consumer's *current* `WEBSOCKET_PROTOCOL_CLASS` value before overwriting it. When `protocols.py` is later imported by `service.py`, it resolves the stashed path via `class_from_module` and subclasses *that*. This preserves any consumer customisations to the WebSocket protocol — the library layers on top rather than replacing.

## Auth priority in onOpen()

The `onOpen()` override uses a three-way auth cascade:

1. **Browser session** (csessid): if the browser already has an authenticated Django session, use it. This handles page refreshes — a stale `?ticket=` in the URL is ignored, avoiding re-consumption of an already-deleted ticket.
2. **Ticket token**: if no browser session but `?ticket=` is present in the WebSocket URL, validate and consume the ticket. Sets `uid` + `logged_in` for `portal_connect()` auto-login.
3. **No session, no token**: role-dependent gating. **Shards** emit a `shard_redirect` OOB pointing at the router and close — orphan connections (typically stale localStorage routing after session expiry) are routed to the router's login flow rather than greeting the player with a connection error. **Routers** fall through to the normal login screen.

This ordering is load-bearing: tickets are single-use, so checking them first would break page refresh (the token is already consumed on first use, but `at_login()` saved the uid to the browser session).

## Client-side redirect mechanism

The redirect operates at the **WebSocket connection layer**, not the page-navigation layer. The page stays loaded; only the underlying WebSocket changes target. UI state, scrollback, command history, plugin state — all persist across cross-shard transitions.

- **OOB message**: server sends `["shard_redirect", ["ws://host:port/?ticket=TOKEN"], {}]`. The URL is a WebSocket URL (`ws://` or `wss://`) with the single-use ticket in the query string.
- **JS plugin** (`shard_redirect.js`): catches the OOB via a direct emitter listener and:
  1. Sets a `deliberate_transfer` flag.
  2. Calls `Evennia.connection.close()` on the current WebSocket-backed connection.
  3. Constructs a new `WebSocket(target_url)` and replaces `Evennia.connection` with a wrapper exposing the standard `{connect, msg, close, isOpen}` contract — mirroring Evennia's own `WebsocketConnection` event-emit shape so other plugins continue working unchanged.
  4. Wraps `Evennia.emitter.emit` once at module load to swallow the `connection_close` event triggered by step 2 (otherwise webclient_gui's `onConnectionClose` would print a misleading "connection was closed or lost" message). Real disconnects continue to render normally — the flag is consumed on the first `connection_close` after each deliberate transfer, with a 5s safety timeout.
- **Middleware** (`ShardRedirectScriptMiddleware`): injects two script tags into webclient HTML responses:
  - An **early inline `<script>`** placed immediately before `evennia.js`'s `<script>` tag, by regex match on the rendered HTML. This runs synchronously after the template's inline `var wsurl = ...` block but before evennia.js loads, and provides the refresh-routing override of `window.wsurl` (see [Refresh routing via localStorage](#refresh-routing-via-localstorage) below). Critical that it runs early — by the time evennia.js's `WebsocketConnection` reads `window.wsurl` (~500ms later inside `Evennia.init`), the override has already taken effect.
  - The **late `shard_redirect.js` script tag** appended just before `</body>` for the OOB `shard_redirect` handler (server-emitted `@ic` / `@ooc` / cross-shard redirects). This is allowed to load late because it just registers a listener for server messages.
  - The middleware also has a legacy ticket-injection path (`?ticket=` in the page URL → inline `<script>` appending `&ticket=TOKEN` to `window.csessid`); that path is now only relevant if someone loads a webclient page directly with `?ticket=` in the URL — an edge case (manual paste, bookmark) that the WS-level redirect doesn't otherwise traverse.

The middleware and JS plugin are auto-injected by `AppConfig.ready()` — zero consumer configuration beyond `INSTALLED_APPS`.

### Refresh routing via localStorage

Browser refresh while connected to a shard would reload the page from the router's URL bar (since the URL never changes during a WS-level redirect) — naively, the refresh always reconnects to the router. The router has no session for a player whose active session is on a shard, so it'd treat the refresh as a fresh login and `at_post_login` would fire, potentially bouncing the player based on AUTO_PUPPET.

To fix:

1. **Save**: `shard_redirect.js` writes the current target's WS endpoint URL (without the single-use ticket) to `localStorage["evennia_shards_last_target"]` on every successful `shard_redirect` it processes.
2. **Override**: an inline `<script>` injected by `ShardRedirectScriptMiddleware` immediately before evennia.js's `<script>` tag reads `localStorage` and `PerformanceNavigationTiming.type` synchronously and overrides `window.wsurl` directly:

   - `type === "reload"` (browser refresh) **AND** localStorage has a saved target → set `window.wsurl = saved_target`. evennia.js's `WebsocketConnection` constructor reads `window.wsurl` ~500ms later inside `Evennia.init`, by which time our override has taken effect, and opens the default WebSocket directly to the saved target (no router round-trip).
   - `type === "navigate"` (typed URL, link click) → no override. Normal router-first flow with login form etc.
   - localStorage absent / unavailable (private browsing, disabled) → no override. Default behaviour.

The destination's csessid auth (priority #1 in `onOpen`) re-attaches the player to their existing session. csessid is the Django session key, shared across all sharded processes via the shared-DB session backend, so the same value works for csessid auth on the router or any shard.

The override has to run *early* — before evennia.js's `WebsocketConnection` reads `window.wsurl`. Injecting before the `evennia.js` script tag is the latest safe seam: synchronously after the template's `var wsurl = ...` block but before evennia.js loads. Anything later (e.g. `$(document).ready` in `shard_redirect.js`, which fires at end-of-body) misses the read.

**Failure mode:** if the saved target rejects the new WebSocket (csessid expired, target restarted, etc.), the destination's `onOpen` falls through to priority #3 (orphan-redirect) which sends the player to the router. There the Django session either re-auths and `at_post_login`'s AUTO_PUPPET path redirects them back, or the login form is shown if the Django session also expired. Acceptable degradation.

### Why connection-level instead of page-navigation

This design was chosen over an earlier full-page-navigation approach (`window.location.href`) for several reasons that compound:

- **UX continuity.** The page never reloads; scrollback, plugin state, custom UI all persist across cross-shard transitions. Players experience the transition as a brief connection swap, not as "the world disappeared and reappeared."
- **Protocol-agnostic shape.** "Close the existing connection, open a new one to a target with a single-use auth token" is exactly the same pattern that telnet/SSH/MUD-client redirects would use (via GMCP, server-side reconnect notice, etc.). The library's WebSocket implementation is one expression of a universal shape, not a web-specific mechanism.
- **No public-shard-URL UX problem.** A page-navigation redirect has to navigate to a URL on the destination shard, which means the destination shard must serve an HTTP webclient — and any direct hit to that URL becomes an orphan-landing problem. With connection-level redirect, the player's browser only ever loads the router's webclient; shards are reached only via WebSocket. This positions a future architecture where shards run WebSocket-only (no Django HTTP at all) — reducing attack surface, eliminating orphan URLs, simplifying deployment.

## Auto-puppet and `_last_puppet`

Evennia's `AUTO_PUPPET_ON_LOGIN` (default `True`) calls `self.puppet_object(session, self.db._last_puppet)` in `at_post_login`. `_last_puppet` is a serialized ObjectDB reference (stored as model class + PK in the Attribute table). Deserializing it triggers `from_db`.

The library must not force `AUTO_PUPPET_ON_LOGIN = False` — both modes must work.

**Router**: runs unscoped under the tenancy install (`clear_shard_context()` at startup), so it sees rows on every shard. It can freely deserialize `_last_puppet`, load characters from any shard, and perform chargen/chardelete. On login with `AUTO_PUPPET_ON_LOGIN = True`, the router reads `_last_puppet`, determines the character's shard, creates a ticket, and redirects. When the player selects a different character (via IC command or character selection), the router overwrites `_last_puppet` with the chosen character before redirecting — so `_last_puppet` is not always strictly the "last puppeted" character; it's the character the router has chosen for the next shard session. The router never actually puppets — it delegates that to the shard.

**Shard**: receives the player via ticket auth. `at_post_login` fires and auto-puppet reads `_last_puppet` — the character is local to this shard, so the tenant auto-filter admits it. The ticket's `character_id` and `_last_puppet` agree because the router set `_last_puppet` before redirecting. Shards explicitly set `AUTO_PUPPET_ON_LOGIN = True` in their per-instance settings to ensure ticket auth always triggers puppeting.

A thin wrapper (`make_shard_at_post_login` in `hooks.py`) is installed around Evennia's original `at_post_login` on shards. The wrapper flushes the `_last_puppet` character from the idmapper cache, then gates `refresh_from_db` behind an explicit `ObjectDB.objects.filter(pk).exists()` check (the auto-filter; Django's `refresh_from_db` itself routes through `_base_manager` and would bypass it — see [tenancy.md](tenancy.md)). If the row is no longer visible (moved off this shard while the account was offline, or hard-deleted), `_last_puppet` is cleared so vanilla `at_post_login` falls through to the OOC menu instead of crashing on `puppet_object`.

**Accounts are AccountDB**, which is not tagged by the tenancy install — shards load accounts freely during ticket auth.

## IC command override

`ShardAwareCmdIC` (in `evennia_shards/commands.py`) replaces Evennia's `CmdIC` via monkey-patch in `AppConfig.ready()`. Injected when `get_role() != "monolith"`.

- **Router** (`AUTO_PUPPET_ON_LOGIN = False` path): resolves the character using the same logic as Evennia's `CmdIC` (playable characters search, Builder+ global search, `_last_puppet` fallback). Instead of calling `puppet_object()`, it: (a) clears `account.db._shards_at_ooc_menu = False` (the only IC entry point where the flag is plausibly True at the moment of redirect — player at OOC menu typing `@ic`), and (b) calls `_redirect_to_character_shard()` which sets `_last_puppet`, creates a ticket via `create_ticket()`, and sends a `shard_redirect` OOB. The shard then ticket-auths, auto-puppets via `_last_puppet`, and the player is IC.
- **Shard**: tells the player "Leave this character before trying to enter another one." IC always goes through the router — no same-shard shortcut.
- **Monolith**: original `CmdIC` stays; the override is never injected.

The injection uses the same pattern as the WebSocket protocol and middleware overrides: patch the module attribute (`evennia.commands.default.account.CmdIC`) so the `AccountCmdSet` picks up the replacement on cmdset rebuild.

## OOC command override

`ShardAwareCmdOOC` (in `evennia_shards/commands.py`) replaces Evennia's `CmdOOC` via monkey-patch in `AppConfig.ready()`. Injected **only** when `get_role() == "shard"` — the router and monolith keep vanilla `CmdOOC`.

- **Shard**: creates a ticket targeting the router (`to_shard = get_router_shard_id()`, always `"router"`) and sends a `shard_redirect` OOB to redirect the client back to the router's webclient. Always redirects — even if no puppet (error state), because a player should never be OOC on a shard.
- **Character ID in ticket**: `old_char.id` (current puppet) → `account.db._last_puppet.id` (fallback) → `0` (sentinel for truly broken state, with `logger.log_warn`).
- **`_last_puppet` is left alone.** The library does not mutate Evennia's `_last_puppet` attribute — vanilla semantics preserved. The OOC redirect loop on routers running `AUTO_PUPPET_ON_LOGIN = True` is broken instead by the account-level `account.db._shards_at_ooc_menu` flag — written on the **router's Server process** when the redirect's ticket auth lands, read by the router's `at_post_login` (see "Auto-puppet on login" below).
- **Does NOT touch `account.db._shards_at_ooc_menu`.** The shard's `@ooc` command does not write the OOC-menu flag. The flag is owned by the router's Server process: the Portal forwards a per-session protocol flag to signal "this session arrived via ticket auth," and the Server's `at_post_login` is the sole writer of the persistent attribute. Keeping the write on the Server avoids cross-process AttributeHandler cache coherency issues — every other process (including the router's Portal) would hold its own stale idmapper.
- **No explicit unpuppet**: the redirect closes the existing WebSocket connection (the JS plugin's first step before opening the new one). Evennia's disconnect handler (`sessionhandler.disconnect()` → `account.unpuppet_object()`) automatically releases the character on the shard when the connection drops.
- **Router**: vanilla `CmdOOC` stays — normal unpuppet, player stays on the router OOC.
- **Monolith**: vanilla `CmdOOC` stays; the override is never injected.

The asymmetry with IC (which patches both router and shard) is intentional: IC on a shard without the override would attempt a local puppet, which is structurally wrong on a shard process. OOC on a router is harmless — the vanilla command does exactly what's needed (unpuppet, show OOC menu).

## Auto-puppet on login (`AUTO_PUPPET_ON_LOGIN = True`)

Evennia's default `AUTO_PUPPET_ON_LOGIN = True` calls `account.puppet_object(session, self.db._last_puppet)` from inside `at_post_login`. On a router that's broken — see [library-integration-risks.md](library-integration-risks.md#defaultaccountat_post_login-override) for why.

The library replaces `DefaultAccount.at_post_login` on routers with `shard_aware_at_post_login` (in `evennia_shards/hooks.py`). The replacement reproduces Evennia's prelude verbatim (protocol flags, `logged_in` OOB, connect-channel msg), then dispatches:

| `AUTO_PUPPET_ON_LOGIN` | Session / `_last_puppet` state | Outcome |
|---|---|---|
| `False` | any | **OOC character-select menu rendered** (vanilla else-branch behaviour — short-circuits before the library's redirect logic). |
| `True` | `session.protocol_flags["SHARDS_TICKET_AUTHED"]` is `True` (Portal flagged this session as a fresh ticket-auth arrival on the router — implicitly an `@ooc` redirect) | OOC menu. The Server also writes `account.db._shards_at_ooc_menu = True` to persist the OOC intent across reconnects. |
| `True` | `account.db._shards_at_ooc_menu` is `True` (no fresh ticket auth on this connection, but persisted OOC intent from a prior `@ooc`) | OOC menu. **No auto-redirect** — honours the player's expressed intent across refresh / reconnect / next-day login. |
| `True` | `_last_puppet` set with usable `shard_id` (in `SHARD_URLS`, not `"*"`) | `_redirect_to_character_shard(...)` — ticket created, OOB `shard_redirect` sent, the JS plugin closes the current WebSocket and opens a new one to the destination shard's WS URL with the ticket. |
| `True` | `_last_puppet` set but `shard_id` is `None` / `"*"` / not in `SHARD_URLS` | Warning logged, OOC menu rendered. Login does not fail. |
| `True` | `_last_puppet` is `None` (fresh first login, no last char) | OOC menu rendered silently. |

The override honours the consumer's `AUTO_PUPPET_ON_LOGIN` setting as the first decision: if the consumer has disabled auto-puppet, the library applies *none* of its redirect logic and renders the OOC menu — same observable outcome as vanilla. The library's redirect machinery only activates when `AUTO_PUPPET_ON_LOGIN = True`.

`_is_redirectable_character()` is the predicate that distinguishes the redirect-eligible row from the broken-state row when AUTO_PUPPET is True. The redirect itself reuses the same `_redirect_to_character_shard()` helper (in `evennia_shards/handoff.py`) that `ShardAwareCmdIC` uses, so both router-side entry points (manual `ic <char>` and login-time auto-puppet) share one code path. The helper is the library's single mechanism for "send a player session to a character's owning shard"; the `cross_shard_move` primitive (also in `handoff.py`) uses it for per-session redirect after a programmatic handoff.

### The OOC-intent signals

The `@ooc` → router → bounce-back-to-shard loop is broken by a **two-flag mechanism**: a transient per-session protocol flag carries the "this session just arrived via ticket auth" signal across the Portal→Server AMP boundary, and a persistent account attribute carries "the player's last expressed intent was OOC" across reconnects.

#### `protocol_flags["SHARDS_TICKET_AUTHED"]` — Portal→Server bridge

- **Set** by `ShardWebSocketClient._mark_ooc_arrival_if_router` (`protocols.py`), called from `onOpen` whenever a `?ticket=` is in the URL on the router — from priority #2 (ticket auth) and also from priority #1 (csessid auth) when the browser carries both a valid Django session and a ticket. The signal is "ticket-in-URL on the router," not "ticket-auth path won." Gated on `get_role() == ROLE_ROUTER` — IC tickets target shards and are validated there, never on the router, so on the router any inbound ticket is implicitly an `@ooc` arrival regardless of which auth branch authenticates the session.
- **Read** by `shard_aware_at_post_login` (`hooks.py`) on the Server. Evennia AMP-syncs `protocol_flags` from Portal to Server as part of the standard session handover.
- **Lifetime** is the WebSocket connection. Refresh discards it. Reconnect creates a new session that does not carry the old flag.

The Portal stores nothing about OOC state. It is a pure pass-through: ticket-authed → forward the bit. All OOC/IC state lives in the Server's account attribute.

#### `account.db._shards_at_ooc_menu` — persistent OOC intent

- **Set** by `shard_aware_at_post_login` (`hooks.py`) on the Server, when it reads `protocol_flags["SHARDS_TICKET_AUTHED"]=True` on a fresh session. Same Server process owns the persistent write and the eventual read on subsequent connections — coherent.
- **Cleared** by `ShardAwareCmdIC.func` on the router (`commands.py`), the only IC entry point where the flag is plausibly True at the moment of redirect (player at OOC menu, types `@ic`). Same router-Server process writes False → reads False on the next connection's `at_post_login`. Coherent.
- **Read** by `shard_aware_at_post_login` on the Server, on every connection where AUTO_PUPPET would otherwise apply. If True (no fresh ticket auth, but persisted OOC intent) → render OOC menu, suppress AUTO_PUPPET. If False → fall through to the standard auto-puppet path.

The flag's semantics is "the player has explicitly chosen to be at the OOC menu, persist that across reconnect." It's set once when an `@ooc` redirect's ticket auth lands at the router, cleared once when the player's `@ic` command runs on the router.

#### Why two flags

A single account-level flag, written by the Portal during ticket auth, was tried and didn't work: the Portal and Server are separate processes with independent `AccountDB` / `Attribute` idmappers. The Portal's write was invisible to the Server's read. The protocol flag bridges that gap — it's the one piece of state Evennia already AMP-syncs Portal→Server, so the Server can make the persistent write itself, and the persistent flag's reads and writes both happen in the same process.

The protocol flag is the bridge, the account flag is the persistence. Together they cover both fresh-arrival (`@ooc` redirect lands) and reconnect (refresh, csessid re-attach, next-day login) cases.

#### Why no shard-side or Portal-side writes to the account flag

Two and only two places write `account.db._shards_at_ooc_menu`: `shard_aware_at_post_login` (sets True on protocol-flag detection) and `ShardAwareCmdIC.func` (sets False on `@ic`). Both are router-Server-side. Any other write would either be cross-process (Portal write, shard-Server write) — invisible to the router-Server's read — or out of scope (vanilla CmdOOC on a monolith doesn't redirect, no flag needed). The shared `_redirect_to_character_shard` helper deliberately does NOT touch the flag, because it can run from a shard's Server process during `cross_shard_move`.

**Trade-off: cross-shard forced moves don't clear the flag.** If an admin moves a player who was at the OOC menu (flag=True), the player ends up IC on the destination shard via the move's ticket auth. The router-side flag remains True. On the player's next connection to the router (refresh, etc.), they land at the OOC menu and have to type `@ic` to return to the moved character. Acceptable degradation; not worth a cross-process invalidation primitive for this edge case.

**Behaviour shift vs. vanilla AUTO_PUPPET.** A vanilla Evennia game with `AUTO_PUPPET_ON_LOGIN = True` auto-puppets `_last_puppet` on every login. A sharded game treats `@ooc` as a sticky preference that survives until `@ic`. Players wanting vanilla "always auto-puppet" don't use `@ooc` — but in a sharded game they realistically would, so honouring that intent is the right default. The escape valve is `@ic` (or character selection) — both clear the flag.

### Scope

The full override is router-only. Monolith uses vanilla Evennia. Shards wrap Evennia's original `at_post_login` with a thin cache-busting preamble (`make_shard_at_post_login` in `hooks.py`) — see the "Auto-puppet and `_last_puppet`" section above — but delegate entirely to vanilla Evennia for the actual auto-puppet logic.
