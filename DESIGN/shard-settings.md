# Shard settings

How the library's configuration items are declared, read, and defaulted.

## Settings

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `SHARDS_ROLE` | `str` | `ROLE_MONOLITH` | One of `ROLE_MONOLITH`, `ROLE_ROUTER`, `ROLE_SHARD` (string constants exported by the library: `"monolith"`, `"router"`, `"shard"`). Selects which role this Evennia process plays. |
| `SHARD_ID` | `str \| None` | `None` | Identifier for this shard. Consumer-chosen — descriptive names like `"overworld"` or `"underdark"` are fine. Required when role is `ROLE_SHARD`. For `ROLE_ROUTER`, must equal `get_router_shard_id()` (library mandate). |
| `ROUTER_URL` | `str \| None` | `None` | **WebSocket** URL for the router (e.g. `"ws://router.example.com/"`, `"wss://..."`). Used by shards for OOC redirect: the player's webclient closes its current WebSocket and opens a new one to this URL with `?ticket=TOKEN` appended. |
| `ROUTER_SHARD_ID` | `str` | `"router"` | The router's shard ID. Library mandate — not consumer-configurable. The router's `SHARD_ID` must be `"router"`. |
| `SHARD_URLS` | `dict \| None` | `None` | Maps shard IDs to **WebSocket** URLs (e.g. `"ws://shard0.example.com/"`). Used by the router for IC redirect: same connection-level swap as `ROUTER_URL`, in the other direction. Shard IDs are flexible — name them to match your game world. |

## How they flow

The library does **not** ship a settings module. It does not write to Django's settings registry, mutate `INSTALLED_APPS`, or modify the consumer's `settings.py`. The flow is one-directional:

1. **Consumer declares** (or doesn't declare) the settings in their `server/conf/settings.py`:
   ```python
   from evennia.settings_default import *
   from evennia_shards import ROLE_ROUTER, ROLE_SHARD, get_router_shard_id
   # ...
   # Router instance:
   SHARDS_ROLE = ROLE_ROUTER
   SHARD_ID = get_router_shard_id()  # mandated to equal the role string

   # Or shard instance:
   SHARDS_ROLE = ROLE_SHARD
   SHARD_ID = "world-east"           # consumer's choice
   ```
   The `ROLE_*` constants are the single source of truth for the role enum — using them rather than bare literals means a future change to the strings is a one-line edit in `config.py`.
2. **Django loads** that file as the canonical settings module (per Evennia's launcher pointing `DJANGO_SETTINGS_MODULE` at `server.conf.settings`).
3. **Library code reads** through accessor functions that apply defaults:
   ```python
   from evennia_shards import get_role, get_shard_id
   role = get_role()         # "monolith" if undeclared, else what consumer set
   shard = get_shard_id()    # None if undeclared, else what consumer set
   ```

The accessors live in [`evennia_shards/config.py`](../src/evennia_shards/config.py) and use `getattr(settings, "...", default)`. The defaults are baked into the library's read code, not into a settings file.

## Why this shape

- **Monolith consumers configure nothing.** No required declarations, no library-provided settings module to inherit from. The default behaviour (do nothing) is genuinely the default — declaring it would be redundant.
- **Non-monolith consumers add at most two lines.** Just `SHARDS_ROLE` (and `SHARD_ID` for shard role) in their existing `settings.py`. No import changes, no app registrations.
- **The library is not a Django app.** It exposes Python functions, not models/admin/signals/URLs. Adding it to `INSTALLED_APPS` is unnecessary today. If a future feature needs Django-level integration, that's the trigger to revisit — not now.
- **`getattr` defaults centralise the contract.** Library code always reads through the accessors, so the fallback value is defined in exactly one place. Adding a new setting later means adding one accessor; consumers automatically get the new default without changes.

## Reading the settings

Code that needs shard configuration — library code *or* consumer game code — should call the accessors rather than reading `settings.*` directly:

```python
from evennia_shards import get_role, get_shard_id, get_shard_url, get_router_url, get_router_shard_id
role = get_role()                  # "monolith" if undeclared
shard = get_shard_id()             # None if undeclared
url = get_shard_url("overworld")   # ValueError if SHARD_URLS not configured
                                   # KeyError if shard_id not in the dict
router = get_router_url()          # ValueError if ROUTER_URL not configured
router_id = get_router_shard_id()  # always "router" — library mandate
```

A direct `settings.SHARDS_ROLE` read raises `AttributeError` whenever the consumer hasn't declared the setting — i.e. every monolith consumer. The accessors apply the documented defaults and are the single source of truth for fallback values, so any future change to a default lands in one place.

The primary caller is library code (it reads the role to decide what to register at boot). Consumers can also call these — for instance, an admin command that prints the deployment mode — and should, rather than rolling their own `getattr` reads.

## Row-level `shard_id` and the global sentinel

The library also adds a `shard_id` column to `ObjectDB` (and likely other partitioned models in future) that tags each row with its owning shard. Most rows hold a specific shard identifier (e.g. `"shard0"`); the sentinel value `"*"` denotes a row owned by *all* shards — used for system-wide entities like global scripts that must run on every shard process. Global rows are instantiated independently on each shard, so any mutable per-instance state does not coordinate across shards without explicit cross-shard messaging.

## URL settings and redirect routing

The router and shards have separate URL settings, reflecting their different roles in the redirect flow:

- **`ROUTER_URL`** — single string, the router's WebSocket URL. Shards use `get_router_url()` to build OOC redirect URLs (sending players back to the router).
- **`SHARD_URLS`** — dict mapping shard IDs to WebSocket URLs. The router uses `get_shard_url(shard_id)` to build IC redirect URLs (sending players to a shard).

```python
ROUTER_URL = "ws://router.example.com/"
SHARD_URLS = {
    "overworld": "ws://overworld.example.com/",
    "dungeons": "ws://dungeons.example.com/",
    "pvp_arena": "wss://pvp.example.com/",
}
```

These are **WebSocket URLs** (`ws://` or `wss://`), not HTTP URLs — the library does *connection-level* redirects: when a player crosses a shard boundary, the JS in the loaded webclient page closes its current WebSocket and opens a new one to the configured target URL with `?ticket=TOKEN` appended. The page itself is not reloaded; UI state, scrollback, plugins all persist across the transition.

This is the same pattern that telnet/SSH/MUD-client redirect would use (close the connection, open a new one to the target host:port with auth) — the WebSocket URL is just one expression of it. The HTTP URL of a shard or router is not the library's concern; whether the consumer also serves an HTTP webclient is a deployment decision.

Shard IDs are flexible — name them to match your game world. Each shard instance's `SHARD_ID` must match a key in `SHARD_URLS`. In production, URLs are typically set via environment variables.

The IC routing decision comes from the character's game state: `character.location or character.home` → room's `shard_id` → `get_shard_url(shard_id)`. Returning players go back to where they were; new characters land in their start location.

## Per-shard home room

Each shard needs its own home room — the fallback location for characters and the spawn point for new ones. Evennia already has `DEFAULT_HOME` and `START_LOCATION` (both default to `"#2"`, the Limbo room created during initial setup). Shards override these in their per-instance settings file to point at a shard-specific room.

```python
# settings_shard0.py
DEFAULT_HOME = "#2"       # or whatever PK the shard's home room has
START_LOCATION = "#2"
```

The library does **not** create these rooms or manage their PKs. The consumer creates them however suits their deployment — initial setup hook, build commands, migration script — and records the PK in settings. This works for both greenfield (new game) and brownfield (existing game adding shards) deployments.

A shard only needs to know its **own** home room. The IC routing flow (`character.location or character.home` → room's `shard_id` → `get_shard_url()`) sends players to the right shard URL; the destination shard places the character based on the character's own location/home already stored in the DB.

## Consumer settings cascade

The library does not prescribe a settings layout, but the demo game uses a three-level cascade that separates per-instance config from shared shard config:

```
settings_router.py  ─┐
settings_shard0.py  ─┤── imports ── settings_common_shard_config.py ── imports ── settings.py
settings_shard1.py  ─┘
```

- **`settings.py`** — base Evennia config (`SERVERNAME`, etc.), loads `secret_settings.py`
- **`settings_common_shard_config.py`** — settings shared across all sharded instances: `ROUTER_URL`, `SHARD_URLS`, `INSTALLED_APPS += ["evennia_shards"]`, `TELNET_ENABLED = False`
- **`settings_<role>.py`** — per-instance: `SHARDS_ROLE`, `SHARD_ID`, `DEFAULT_HOME`/`START_LOCATION`, port overrides (`WEBSERVER_PORTS`, `WEBSOCKET_CLIENT_PORT`, `AMP_PORT`)

Each instance starts with `evennia start --settings settings_router.py` (or `settings_shard0.py`, etc.). The cascade keeps the URL map in one place while allowing each instance to set its own role and ports.

## Telnet

Telnet is disabled for all sharded instances (`TELNET_ENABLED = False` in the common config). The ticket-based auth flow is websocket-only — telnet has no mechanism to carry a ticket token (no URL, no query parameters). Wiring telnet into the ticket system is future work.

## HTTP webserver: router-only by default

The library assumes exactly one HTTP webserver in the deployment, hosting the webclient page, the website, and the static-asset pipeline. By default it's the router:

| Setting | Router | Shard |
|---|---|---|
| `WEBSERVER_ENABLED` | `True` | `False` |

Shards exist to host player sessions, not to serve web pages. Disabling the webserver on shards drops the entire HTTP stack on those processes (reverse-proxy, AJAX webclient, Django views, `WEB_PLUGINS_MODULE` hook chain) — they listen on the AMP port and the WebSocket port, nothing else.

This requires a small library workaround because Evennia 6.0.0 bundles the WebSocket registration inside `register_webserver` (see [deployment-topology.md](deployment-topology.md#a-note-on-evennias-coupling)). The library's Portal-services plugin (`evennia_shards/portal_services.py`) registers the WebSocket independently when `WEBSERVER_ENABLED = False`. Auto-installed by `AppConfig.ready()`; no consumer wiring required.

Consumers running their website on a separate service (Next.js, static site host, separate Django, etc.) can flip `WEBSERVER_ENABLED = False` on the router as well — the same plugin keeps the WebSocket running with no HTTP serving on the Evennia process at all.

## Localhost multi-instance ports

Each Evennia instance binds several ports. When running multiple instances on localhost for testing, each needs its own set to avoid collisions. The demo game offsets shard ports by 10 from the router's defaults:

| Port | Router | Shard0 |
|---|---|---|
| `WEBSERVER_PORTS` | `(4001, 4005)` | `(4011, 4015)` |
| `WEBSOCKET_CLIENT_PORT` | `4002` | `4012` |
| `AMP_PORT` | `4006` | `4016` |

Additional shards increment by 10 again (4021/4022/4026, etc.). `AMP_PORT` is Evennia's internal Portal↔Server IPC — not player-facing, but still needs a unique port per instance. In production (separate hosts), port offsets are unnecessary.

## Localhost multi-instance game directories

The recipe for running multiple roles locally differs by OS (Windows runs all roles from one gamedir; Unix needs symlinked view gamedirs to avoid PID-file collisions). See [deployment-topology.md § Local development](deployment-topology.md#local-development) for the full explanation and the canonical recipes.

## What this design doesn't address

- **Validation.** Nothing checks that `SHARDS_ROLE` is one of the three valid strings, or that `SHARD_ID` is set when role is `"shard"`. Validation will land with whatever code first depends on it. Pre-building it now would be forward-design.
