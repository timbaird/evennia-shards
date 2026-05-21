# evennia-shards

> Making split deployment a config option in Evennia.

A drop-in extension to [Evennia](https://www.evennia.com/) that adds optional split deployment and horizontal sharding via configuration alone. Install it and the game runs as vanilla Evennia. Flip a config setting and the same code runs as a split deployment (auth process separate from game process). Flip another, and it runs as full multi-shard.

> **Status: working MVP, not production-ready.** Phase 1 (router + shards, ticket auth, IC/OOC redirects, cross-shard character + inventory move, chargen, primitive cross-shard messaging) is functionally complete and live-smoke-verified end-to-end against three demo gamedirs. The API has not yet been exercised by a real consumer game. See [DESIGN/progress.md](DESIGN/progress.md) for the running milestone log; [DESIGN/INDEX.md](DESIGN/INDEX.md) is the design wiki.

## What this is

A small, additive enhancement library. Three modes are selected per Evennia process via a single config setting:

- **`monolith`** *(default)* — single process does everything. The library is dormant; you get vanilla Evennia.
- **`router`** — auth front door. Owns `AccountDB`, runs login and the OOC menu, redirects players to a shard via single-use tickets on `@ic`.
- **`shard`** — game world. Loads its slice of the world, accepts ticket-based session attaches from the router or other shards.

The library does not impose its own room or character base classes — it provides infrastructure (per-row shard partition enforced via the [django-multitenant](https://github.com/citusdata/django-multitenant) auto-filter, cross-shard character move, ticket auth, message-bus primitives) and lets the consumer game keep its own typeclasses.

## What this is *not*

- Not a fork of Evennia. The library imports from upstream Evennia; Evennia stays untouched.
- Not a parallel Evennia distribution.
- Not a "rewrite your stack to scale" project.
- Not a multi-region, multi-database, or multi-datacenter design.
- Not a solution for *"what if we had millions of players"* — that is explicitly deferred.

The design is scoped to the **single-Postgres era**: from one Evennia process today through however many shards run against a single, vertically scaled Postgres. The working theory is that Evennia's per-process bottleneck is its single-threaded Twisted reactor — game logic, ticks, scripts, and player commands all share one thread — while Postgres handles concurrent connections and aggregate load comfortably. Horizontal scaling for Evennia therefore means adding *Evennia* processes, not databases; a single vertically scaled Postgres should absorb the load of many shards before its own limits bite. We haven't benchmarked at scale, and "many" is qualitative — if a real game pushes through that frontier, the architectural assumptions here will need revisiting. Scoping to single-Postgres keeps the design surface small in the meantime. See [the archived handover](DESIGN/archive/evennia-shards-HANDOVER.md#project-identity-and-positioning) for the original positioning statement and out-of-scope list.

## Quick start

The repo ships three demo gamedirs under [`examples/`](examples/) — `demo_router`, `demo_shard0`, `demo_shard1` — that exercise the library end-to-end on a single machine.

```bash
# Create a virtualenv and install Evennia
python -m venv venv
source venv/Scripts/activate    # on Windows; use venv/bin/activate elsewhere
pip install evennia

# Clone and install evennia-shards in editable mode
git clone https://github.com/timbaird/evennia-shards.git
cd evennia-shards
pip install -e .
```

Each demo gamedir runs as its own Evennia process with its own `settings.py` declaring its `SHARDS_ROLE` and `SHARD_ID`. See [`examples/README.md`](examples/README.md) for the run-three-processes recipe.

For consumer games installing the library as a dependency (rather than developing it), `pip install evennia-shards` will be the install path once the package is published.

## Documentation

All technical documentation lives in [DESIGN/](DESIGN/). Start at [DESIGN/INDEX.md](DESIGN/INDEX.md) for the doc map and reading paths.

Notable entry points:

- **[DESIGN/INDEX.md](DESIGN/INDEX.md)** — map of all design documents.
- **[DESIGN/progress.md](DESIGN/progress.md)** — running log of milestones with links to evidence (test results, design docs, code changes).
- **[DESIGN/documentation-structure.md](DESIGN/documentation-structure.md)** — what belongs in CLAUDE.md vs README.md vs DESIGN/, and conventions for new design docs.
- **[DESIGN/archive/evennia-shards-HANDOVER.md](DESIGN/archive/evennia-shards-HANDOVER.md)** — the original brainstorm session that started this project. Archived as historical context; current decisions extend and refine it.
- **[CLAUDE.md](CLAUDE.md)** — instructions for LLM agents working in this repo.

## Project relationships

This library was extracted from scaling work originally done for the [FullCircleMUD (FCM)](https://fcmud.world) project. FCM is the intended first consumer game and will adopt the library as a dependency. The library is deliberately game-agnostic; FCM-specific concerns stay in FCM. See [the origin section of the archived handover](DESIGN/archive/evennia-shards-HANDOVER.md#origin-why-this-is-a-separate-project) for the original rationale.

## License

BSD 3-Clause — see [LICENSE](LICENSE). Same family as Evennia's license.

## Contributing

Not yet open to outside contributions. Once the library has been exercised by at least one consumer game (FCM) and the API has had a chance to settle through real use, contribution guidelines will be added.
