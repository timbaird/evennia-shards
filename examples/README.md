# Demo game — multi-instance localhost setup

This directory contains a demo Evennia game configured for multi-instance
testing of the evennia-shards library.

The mechanism for running N processes locally differs by OS — Windows runs
all roles directly from `demo_shard0/`, Unix runs each role from its own
symlinked view gamedir. The full rationale (PID-file behaviour on each OS,
why view dirs exist) lives in
[`DESIGN/deployment-topology.md` § Local development](../DESIGN/deployment-topology.md#local-development);
this file just ships the practical recipes.

## Windows: run all roles from `demo_shard0/`

No symlinks, no view directories. Open one terminal per role, `cd` into
`demo_shard0/`, and start each role with its own `--settings` flag.

The directory layout is just one real gamedir:

```
examples/
  demo_shard0/      The single real game directory. All code, all settings
                    files, the shared SQLite database, all PID/log files
                    (per-role names are not needed on Windows — see the
                    deployment-topology doc for the OS-split rationale).
```

The `demo_router/` and `demo_shard1/` directories in this repo are Unix
artefacts (git-stored symlinks); ignore them on Windows.

See **Quick start** below for the commands.

## Unix (Linux, macOS, WSL): symlinked view gamedirs

Each non-canonical role runs from its own view gamedir that symlinks back
to `demo_shard0/` for code and settings but owns its own `server/`
subdirectory (so PID files and logs land in per-role paths and don't
collide).

```
examples/
  demo_shard0/      The "real" game directory. Contains all game code
                    (commands, typeclasses, web, world), all settings
                    files, and the shared SQLite database.

  demo_router/      Symlinked instance for the router role. Links to
                    demo_shard0's code and settings, but has its own
                    server/ directory for PID files and logs.

  demo_shard1/      Symlinked instance for a second shard. Same
                    pattern as demo_router. Active in SHARD_URLS.
```

### What's symlinked vs real

Each symlinked instance (`demo_router`, `demo_shard1`) contains:

- **Symlinks** to `demo_shard0/`: `commands/`, `typeclasses/`, `web/`,
  `world/`, `server/conf/`, `server/.static/`, `.gitignore`
- **Real (instance-specific)**: `server/`, `server/__init__.py`,
  `server/logs/`

This means PID files and logs are per-instance (no collisions), while
game code and settings are shared (edit once, applies everywhere).

### Shared database

All instances share the same SQLite database at
`demo_shard0/server/evennia.db3`. This is configured in
`settings_common_shard_config.py` using `os.path.realpath(__file__)`
to resolve symlinks back to the real conf directory.

## Quick start

### Boot order (both OSes)

Boot `shard0` first. Evennia's initial-setup runs on the first
`evennia migrate` / `evennia start` it sees and creates the bootstrap
rows (`#1` superuser character, `#2` Limbo). The tenancy install
auto-stamps those rows with the booting process's shard
ID, so **whichever role boots first owns Limbo**.

Chargen happens on the **router** (it's an OOC operation), so the
router's `START_LOCATION` is what determines where new characters
spawn. The library's chargen wrapper looks up that row's `shard_id`
and stamps the new character to match. Booting `shard0` first means
Limbo (`#2`) lands on `shard0`, and the router's default
`START_LOCATION = "#2"` resolves to a `shard0`-owned row — meaning
new players land on `shard0`. Point the router's `START_LOCATION` at
a room on a different shard to change where chargen spawns to.

### Windows recipe

Open three terminals. All three `cd` into `demo_shard0/`. No view
gamedirs needed.

```powershell
# Terminal 1 — shard0 first (so Limbo is shard0's)
cd examples\demo_shard0
evennia migrate --settings settings_shard0      # one-time, on a fresh DB
evennia start --settings settings_shard0

# Terminal 2 — router
cd examples\demo_shard0
evennia start --settings settings_router

# Terminal 3 (optional) — shard1
cd examples\demo_shard0
evennia start --settings settings_shard1
```

### Unix recipe

Three terminals, each in the view gamedir for its role.

```bash
# Terminal 1 — shard0
cd examples/demo_shard0
evennia migrate --settings settings_shard0      # one-time, on a fresh DB
evennia start --settings settings_shard0

# Terminal 2 — shard1
cd examples/demo_shard1
evennia start --settings settings_shard1

# Terminal 3 — router
cd examples/demo_router
evennia start --settings settings_router
```

### Connect (both OSes)

- **Router webclient**: http://localhost:4001 — log in here. After `@ic`
  you're redirected to the character's owning shard.
- **Shard0 webclient**: http://localhost:4011 (direct shard access; for
  smoke testing only, normal play goes through the router).
- **Shard1 webclient**: http://localhost:4021 (same).

### Stopping

`evennia stop --settings settings_<role>` from the directory you started
the role from. On Windows that's always `demo_shard0/`; on Unix it's the
view gamedir for that role (`demo_router/`, `demo_shard0/`, or
`demo_shard1/`).

On Windows specifically, the stop signal is a console-group Ctrl+C — it
stops every process started from that terminal. Keep one role per
terminal so stop is per-role.

## Port assignments

| Port               | Router | Shard0 | Shard1 |
|--------------------|--------|--------|--------|
| `WEBSERVER_PORTS`  | 4001   | 4011   | 4021   |
| `WEBSOCKET_CLIENT` | 4002   | 4012   | 4022   |
| `AMP_PORT`         | 4006   | 4016   | 4026   |

Telnet is disabled for all sharded instances (ticket auth is
websocket-only).

## Settings cascade

All per-instance settings files import from a shared middle layer:

```
settings_router.py  ─┐
settings_shard0.py  ─┤── settings_common_shard_config.py ── settings.py
settings_shard1.py  ─┘
```

- `settings.py` — base Evennia config
- `settings_common_shard_config.py` — shared DB path, `SHARD_URLS`,
  `ROUTER_URL`, `INSTALLED_APPS`, `TELNET_ENABLED = False`
- `settings_<role>.py` — per-instance: `SHARDS_ROLE`, `SHARD_ID`,
  `DEFAULT_HOME`, port overrides

## Recreating the symlinked directories (Unix only)

If you need to recreate `demo_router` or `demo_shard1` from scratch on a
Unix host (Linux, macOS, WSL):

```bash
cd examples

# demo_router
mkdir -p demo_router/server/logs
touch demo_router/server/__init__.py
ln -s ../demo_shard0/commands demo_router/commands
ln -s ../demo_shard0/typeclasses demo_router/typeclasses
ln -s ../demo_shard0/web demo_router/web
ln -s ../demo_shard0/world demo_router/world
ln -s ../../demo_shard0/server/conf demo_router/server/conf
ln -s ../../demo_shard0/server/.static demo_router/server/.static
ln -s ../demo_shard0/.gitignore demo_router/.gitignore

# demo_shard1 (same pattern)
mkdir -p demo_shard1/server/logs
touch demo_shard1/server/__init__.py
ln -s ../demo_shard0/commands demo_shard1/commands
ln -s ../demo_shard0/typeclasses demo_shard1/typeclasses
ln -s ../demo_shard0/web demo_shard1/web
ln -s ../demo_shard0/world demo_shard1/world
ln -s ../../demo_shard0/server/conf demo_shard1/server/conf
ln -s ../../demo_shard0/server/.static demo_shard1/server/.static
ln -s ../demo_shard0/.gitignore demo_shard1/.gitignore
```
