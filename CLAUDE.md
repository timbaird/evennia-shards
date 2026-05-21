# CLAUDE.md

Instructions for Claude (and other LLM agents) working in this repository.

## What this project is

`evennia-shards` is a drop-in extension to [Evennia](https://www.evennia.com/) that adds optional split deployment and horizontal sharding via configuration alone. Tagline: **"Making split deployment a config option in Evennia."**

For the big-picture overview, read [README.md](README.md).
For the design wiki, read [DESIGN/INDEX.md](DESIGN/INDEX.md).

## Project status

**Working MVP, not production-ready.** Shard partition enforcement (django-multitenant auto-filter on `ObjectDB`), ticket-based WebSocket auth, cross-shard character + inventory move, chargen wrapper, cross-shard `@tel`, and primitive cross-shard messaging (`obj_msg` / `account_msg` / `room_msg`) are all shipped and live-smoke-verified end-to-end against three demo gamedirs. For the current state read [DESIGN/progress.md](DESIGN/progress.md). The library has not yet been exercised by a real consumer game.

## Where to read first

For any non-trivial task, start by reading in this order:

1. [README.md](README.md) — what the project is, status, quick start.
2. [DESIGN/INDEX.md](DESIGN/INDEX.md) — map of all design docs.
3. [DESIGN/documentation-structure.md](DESIGN/documentation-structure.md) — what goes in CLAUDE.md vs README.md vs DESIGN/, and naming conventions.
4. [DESIGN/archive/evennia-shards-HANDOVER.md](DESIGN/archive/evennia-shards-HANDOVER.md) — *archived* original brainstorm. Useful historical context; not authoritative.

## Load-bearing architectural principles

These are the principles every implementation decision must respect. They are restated from the handover because they shape day-to-day choices and getting them wrong is expensive to undo.

1. **Default to "library does nothing" in `monolith` mode.** Any feature must justify its presence in monolith. Gate it on `SHARDS_ROLE != "monolith"` if it doesn't add value there.
2. **Branch at registration time, not per call.** Don't register the override in monolith mode in the first place. Per-call branching is acceptable only when registration-time branching is genuinely impossible.
3. **The library does not own game concepts.** Rooms, characters, zones, items belong to the consumer game. The library provides infrastructure (ticket system, redirect protocol, handoff lifecycle, tenancy enforcement via django-multitenant, cross-shard message bus, sender-side helpers). When tempted to add a game concept, ask whether it's actually game-specific and should stay in the consumer.
4. **No FCM-specific assumptions.** This library was extracted from work on FullCircleMUD (FCM). Anything FCM-specific creeping into the library is a code smell. Zone names, economy concepts, NFT references, FCM-specific typeclass names — all stay in FCM. Default to "consumer concern" when uncertain.
5. **Cache invariant by construction, not discipline.** Use `.values()` queries, mode-specific code registration, and clear ownership boundaries. Don't rely on developers remembering not to cache things.
6. **Web-first.** Telnet support is a deliberate post-PoC concern.
7. **Resist runtime config registries.** Source-controlled Python constants in the consumer game are the canonical config surface for things like zone→shard maps. Reshuffles are planned events, not hot operations.
8. **Single-Postgres bound.** The whole design is scoped to "from one Evennia process today through however many shards run against a single, vertically scaled Postgres." Beyond that is explicitly deferred.
9. **Use the role accessors, not raw settings reads.** Code that needs `SHARDS_ROLE` or `SHARD_ID` — library code *or* consumer game code — should call `evennia_shards.get_role()` / `get_shard_id()` rather than `settings.SHARDS_ROLE`. The accessors apply the documented defaults; raw `settings.SHARDS_ROLE` raises `AttributeError` whenever the consumer hasn't declared the setting (i.e. every monolith consumer). See [DESIGN/shard-settings.md](DESIGN/shard-settings.md).

## Out of scope

See the [explicit out-of-scope list in the archived handover](DESIGN/archive/evennia-shards-HANDOVER.md#things-explicitly-out-of-scope) before proposing features. Recurring "is this in scope?" questions:

- Multi-Postgres / read replicas — **no.**
- Cross-region / multi-datacenter — **no.**
- Live (non-reconnect) session migration — **no.**
- Cross-shard combat / parties / follower trains — **no.**
- A runtime registry for zone→shard mapping — **no, it's a Python constant.**
- Telnet redirect protocol — **deferred.**

## Working conventions

- **Editing design docs.** Update or add design documents whenever an architectural decision is made or refined. Capture the *why*, not just the *what*. Index new docs in [DESIGN/INDEX.md](DESIGN/INDEX.md).
- **CLAUDE.md vs README.md vs DESIGN/.** See [DESIGN/documentation-structure.md](DESIGN/documentation-structure.md) for the split. CLAUDE.md is for Claude-facing instructions; README.md is for humans landing on the repo; DESIGN/ is the technical wiki.
- **Don't put implementation detail in this file or README.** Link out to DESIGN/ instead. Keep CLAUDE.md and README.md stable; let DESIGN/ churn.
- **License.** BSD 3-Clause. New source files should carry a short SPDX header (`# SPDX-License-Identifier: BSD-3-Clause`) once code starts landing.

## Documentation discipline (load-bearing)

Design documents in `DESIGN/` must reflect decisions **actually discussed and agreed on with the project owner**. They are not a place to forward-design the system from first principles or extrapolate "reasonable defaults" from a starting point.

**Rules:**

1. **Only capture what was discussed and agreed.** If the conversation establishes a principle (e.g. "the library mandates a gateway helper, everything else is consumer choice"), do not extrapolate it into specifics that were not raised (e.g. a numbered adoption checklist, a decision tree, specific API shapes, naming conventions).
2. **Flag open questions explicitly.** Where a topic has been raised but not resolved, write `[TBD — needs discussion: <what is open>]` in the doc. Future sessions then pick the topic up deliberately rather than inheriting unagreed assumptions.
3. **Distinguish handover content from in-conversation decisions.** The [archived handover](DESIGN/archive/evennia-shards-HANDOVER.md) is a brainstorm artifact, not authoritative. Restating handover content in new docs is acceptable when it provides necessary context, but mark it as such (e.g. *"Per the original handover: ..."*) rather than presenting it as a decision freshly made or as canonical project intent.
4. **Smaller is better.** A doc that captures three discussed points faithfully is more useful than one that captures three discussed points plus seven invented ones. Resist the urge to fill out sections "for completeness."

If a session catches itself writing content that goes beyond what was discussed, stop and either remove the extrapolation or convert it to a `[TBD]` marker. Documentation that puts unagreed decisions in the project's mouth is worse than documentation that has gaps.

## Repository layout

```
evennia-shards/
├── CLAUDE.md                  # this file
├── README.md
├── LICENSE                    # BSD 3-Clause
├── pyproject.toml
├── runtests.py                # standalone test runner (no consumer gamedir needed)
├── DESIGN/                    # design wiki (humans + LLMs)
├── src/
│   └── evennia_shards/        # library code (src layout)
│       └── tests.py           # unit tests (run via runtests.py)
├── tests/                     # standalone test settings (test_settings.py, urls.py)
└── examples/
    ├── demo_router/           # router-role demo gamedir
    ├── demo_shard0/           # shard-role demo gamedir (source of truth — others symlink to it)
    └── demo_shard1/           # second shard for multi-shard testing
```

## Tools and environment

- Python 3.10+ (pinned via `pyproject.toml`).
- Evennia is a runtime dependency (`pip install evennia`).
- Postgres required only for multi-shard mode; SQLite is fine for monolith and single-shard split.
- No Redis dependency. The cross-shard message bus uses a Postgres-polled `messages` table via Twisted `LoopingCall` — see [DESIGN/cross-shard-message-bus.md](DESIGN/cross-shard-message-bus.md). Earlier drafts considered `channels_redis` but it was not adopted.
