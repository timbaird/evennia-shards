# Deployment Topology

How a consumer game using this library is structured for development and production. This document captures decisions explicitly discussed.

## Core property: same code, different config

Every process running the consumer's game runs identical Python code from one source folder (locally) or one git repo (in production). What makes a process behave as a "router" or a "shard0" or a "shard1" is the settings module it loads at startup, which in turn is selected by an environment variable. There is no per-role codebase, no per-role build, no per-role branch.

This is the architectural commitment that makes the library worth building: divergence between roles is expressed as configuration, not as separate engineering artifacts.

## Local development

Every role in split mode is just another `evennia start` invocation, against the **same source files** and the **same database**, with the only difference being which settings file the launcher loads. Per-role config is selected with `--settings settings_<role>` (the demo's naming convention) or equivalently by setting `DJANGO_SETTINGS_MODULE` for that one invocation. Each process is locked into its role from startup until the operator stops it.

The settings file naming convention (`settings_router.py` / `settings_shard0.py` / `settings_shard1.py` plus a shared `settings_common_shard_config.py`) is *demonstrated* by the demo gamedirs, not *mandated* by the library — see [shard-settings.md](shard-settings.md#consumer-settings-cascade) for the cascade pattern.

Monolith mode is just one terminal: `cd <game folder> && evennia start` with the base settings.

**Boot order matters on a fresh database.** Evennia's `initial_setup` creates Limbo (`#2`) and the superuser character (`#1`); the tenancy install auto-stamps those rows with whichever shard is currently saving. **Boot `shard0` first** so Limbo and the superuser land with `shard_id="shard0"` — a row created under the router (which runs unscoped, so the auto-stamp is skipped) would land `shard_id=NULL` and not be a valid IC destination without backfilling.

**The mechanism for running N processes locally varies by OS** because Evennia's PID-file mechanism varies by OS. The two cases are written up explicitly below; this is the canonical reference for local multi-process setup, and other docs (e.g. [shard-settings.md](shard-settings.md), [`examples/README.md`](../examples/README.md)) point back here.

### Windows: same gamedir, N terminals

On Windows, N processes can be started directly from the same gamedir with no setup beyond writing the per-role settings files. Boot order: shard0 first (see *Boot order* above), then the router, then any additional shards.

```
# Terminal 1 — shard0 first (claims Limbo / superuser on a fresh DB)
cd <game folder>
evennia start --settings settings_shard0

# Terminal 2 — router
cd <game folder>
evennia start --settings settings_router

# Terminal 3 (optional) — shard1
cd <game folder>
evennia start --settings settings_shard1
```

This works because Evennia's launcher gates the `--pidfile` argument it passes to `twistd` on `os.name != "nt"` (`evennia_launcher.py:529-532`). On Windows that branch is skipped entirely: `twistd` never writes `<gamedir>/server/server.pid` or `<gamedir>/server/portal.pid`, so two `evennia start` invocations from the same folder don't fight over those files.

`evennia stop` on Windows uses Win32 console-group signals (`GenerateConsoleCtrlEvent`, `evennia_launcher.py:1618-1631`) rather than a PID-file lookup. The signal goes to every process spawned from the same console — which is why each role lives in its own terminal: stopping one terminal stops only the processes started from that terminal.

### Unix (Linux, macOS, WSL): view gamedirs

On Unix the launcher *does* pass `--pidfile=<gamedir>/server/server.pid` (and the portal equivalent) to `twistd`, and `twistd` writes the PID integer to that path on start. Two `evennia start` invocations from the same gamedir collide on those files: the second one either refuses to start (sees a stale PID) or stomps on the first's PID record. The path is hardcoded into the launcher (`evennia_launcher.py:1824-1825`) and is not settings-overridable.

The workaround is **view gamedirs**: a thin directory per role that symlinks back to the canonical gamedir for code and settings, but owns its own `server/__init__.py` and `server/logs/` (so PID and log files land in per-role paths). The shared database is reached via `os.path.realpath(__file__)` in `settings_common_shard_config.py`, which resolves the symlinked conf path back to the canonical `server/` directory regardless of which view launched the process.

The demo ships the recipe and the symlink commands in [`examples/README.md`](../examples/README.md). Library-shipped view-creation tooling was considered and explicitly *not* shipped — the demo's recipe is small, the Unix case is the only one that needs it, and inventing a cross-OS helper turns out to be more surface than the problem warrants (see [progress.md](progress.md) for the decision).

### Shared properties (both OSes)

Regardless of OS, all processes:

- Read the same source files from the same directory tree.
- Share one database (SQLite locally, configured in the base settings).
- Run in parallel for the duration of the dev session — terminals stay open; nothing is switched between roles mid-session.

## Production deployment

The same shape, scaled up. Discussed in the context of Railway as the example platform:

- One git repository for the consumer's game.
- N+1 platform services (one router, N shards), all deployed from the same repository on the same branch.
- Each service has its own environment variable set: `SHARDS_ROLE`, `SHARD_ID` (where applicable), per-service URLs/ports.
- All services share one Postgres instance.
- Multi-shard deployments use the Postgres-backed message bus for cross-shard messaging (see [cross-shard-message-bus.md](cross-shard-message-bus.md)) — no infrastructure beyond the shared Postgres.

Redis was originally considered for the cross-shard message bus (the archived handover and earlier drafts referenced `channels_redis`). It was not adopted: the Postgres-table-polled `LoopingCall` design covers the same ground without adding a runtime dependency.

When a commit is pushed, the platform redeploys all services in parallel from the new commit. Code stays in lockstep across roles by definition — there is no "shard A is on a different commit than shard B" failure mode.

The library's only required role-distinguishing settings are `SHARDS_ROLE` and `SHARD_ID` (see [shard-settings.md](shard-settings.md)); `DJANGO_SETTINGS_MODULE` is the env var that selects which settings file to load, and is Django's own — the library doesn't prescribe additional env vars. Each service's public WebSocket URL is communicated via `SHARD_URLS` (a dict on the router pointing at every shard) and `ROUTER_URL` (a single URL on each shard pointing at the router); both are documented in [shard-settings.md](shard-settings.md#url-settings-and-redirect-routing).

## Mirror property: local and production share shape

| Aspect | Local dev | Production |
|---|---|---|
| Source on disk | One folder | One git repo |
| Running processes | 3 (terminals) | 3 (platform services) |
| What differentiates | `DJANGO_SETTINGS_MODULE` per terminal | Env vars per service |
| Shared database | One SQLite | One Postgres |
| Shared message bus (multi-shard) | Postgres-polled (shared with main DB) | Postgres-polled (no additional infra) |

The shape is identical; the scale differs. Local dev is the same topology as production, not a simplified approximation.

## HTTP webserver topology: one webserver, somewhere

The library assumes the consumer game has **exactly one HTTP webserver** in the deployment, hosting the webclient page, the website, the static-asset pipeline, and the Django admin. By default that webserver lives on the **router** process — turn it on with `WEBSERVER_ENABLED = True` in the router's settings, leave it off (`WEBSERVER_ENABLED = False`) on every shard.

**Shards never serve HTTP.** They exist to host player sessions; the only network ports they need are the WebSocket (now), and telnet/SSH (when those land). Running a full HTTP stack on every shard — reverse-proxy from Portal to Server, Django webclient view, static-asset serving, the AJAX webclient fallback, the `WEB_PLUGINS_MODULE` hook chain — is gratuitous when no browser ever loads a page from the shard.

**External-website case.** A consumer running their website on a separate service entirely (Next.js, static site host, separate Django, whatever) can flip `WEBSERVER_ENABLED = False` on the router as well. The library treats router and shard symmetrically: any process with `WEBSERVER_ENABLED = False` runs WebSocket-only. The library doesn't care where the consumer's website actually lives — only that exactly one place renders the webclient page and serves the static assets that page references.

### A note on Evennia's coupling

Achieving "WebSocket-only" mode required some unpicking on the library's part. Evennia 6.0.0 (and earlier) registers the webclient WebSocket service **inside** the HTTP webserver setup — specifically nested in the loop that builds the reverse-proxy in [`PortalServerFactory.register_webserver`](../../venv/Lib/site-packages/evennia/server/portal/service.py#L177). Setting `WEBSERVER_ENABLED = False` cleanly disables the HTTP stack but takes the WebSocket down with it.

The WebSocket has no architectural reason to be coupled to the HTTP webserver — it's a separate Twisted `TCPServer` on a separate port, listening for an entirely different protocol, sharing no state with the HTTP reverse-proxy, the AJAX webclient, or the Django views. The bundling appears to be incidental to the way `register_webserver` was originally laid out, not a deliberate design choice. (Compare the telnet and SSH protocols, which Evennia registers as top-level services on the Portal factory directly — exactly the level the WebSocket should be at.)

To work around this, the library provides a Portal-services plugin (`evennia_shards/portal_services.py`) that registers the WebSocket independently when `WEBSERVER_ENABLED = False`. The plugin is a no-op when the webserver *is* enabled (Evennia's normal flow registers the WS, doing it twice would EADDRINUSE). See [library-integration-risks.md](library-integration-risks.md#portal-services-plugin) for the full coupling write-up.

## Why one repo, not N

Discussed and agreed: maintaining N repositories for N roles re-introduces the failure mode the architecture exists to prevent — divergent commits across services causing inconsistent behaviour at shard boundaries. One repo means one commit hash describes the system, one CI pipeline gates every deploy, and reverts are atomic.

This is the same reasoning Kubernetes applies to its one-image-many-pods model.

## Library development: the demo gamedirs

The library's repository contains three demo gamedirs under `examples/` (`demo_router`, `demo_shard0`, `demo_shard1`) — one per role. They are the consumer pattern in miniature: real Evennia gamedirs (generated via `evennia --init`) that depend on `evennia_shards` exactly as a real consumer game would, with their game code shared via symlinks (`demo_shard0` is the source of truth; the other two link to its `commands/`, `typeclasses/`, `web/`, `world/`, `server/conf/`). See [`examples/README.md`](../examples/README.md) for the layout and run-three-processes recipe.

Discussed and agreed:

- The demos' purpose is to drive library development (run them to test library changes end-to-end).
- They are **not shipped** as part of the pip package — `[tool.setuptools.packages.find]` includes only `evennia_shards*`, excluding `examples/`, `tests/`, and `DESIGN/`.
- They are **not a starting template** that consumers should clone. Real consumer games live in their own separate repositories.
- Real consumer games depend on the library via `pip install evennia-shards` (once published) or `pip install git+https://github.com/.../evennia-shards.git`. They do not derive from or copy the demos.

## Development phasing (followed during build)

The library was built up across four cases, each a distinct deployment shape exercising a new capability. All four are functionally complete; see [progress.md](progress.md) for the running milestone log with evidence pointers.

1. **Monolith.** Library installed; `SHARDS_ROLE` setting exposed but defaulting to `monolith`; library is dormant; game runs as native Evennia.
2. **Split: router + 1 shard.** Auth/web/OOC on the router; the entire IC world on the shard. Exercises the ticket-based redirect protocol end-to-end.
3a. **Multi-shard navigation.** Two or more shards, each owning part of the IC world. Exercises the cache invariant and cross-shard handoff via the `cross_shard_move` primitive (atomic `qs.update` to retag the row, recursive inventory move, idmapper eviction, per-session ticket redirect).
3b. **Multi-shard messaging.** Postgres-backed cross-shard message bus with player-facing delivery primitives (`obj_msg`, `account_msg`) and the `send_cross_shard_message` helper. Specialised consumer-level patterns (cross-shard tells, channel propagation) are deferred to `evennia_shards/contrib/`.

Each case strictly builds on the previous.

Case 1 is qualitatively different from the others: it is a prerequisite gate ("did installing the library break anything?") rather than a feature milestone. It is testable using Evennia's own test suite — running `evennia test evennia` with `evennia_shards` installed should pass exactly as it does without the library. If Evennia's tests fail with the library present, the library is not genuinely transparent in monolith mode and that failure is the first thing to fix. This made Case 1 a strong, automatable verification gate before work on Cases 2/3a/3b began.