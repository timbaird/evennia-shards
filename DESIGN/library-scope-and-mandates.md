# Library Scope and Mandates

What this library provides and what it deliberately leaves to the consumer. The aim is the smallest possible mandatory surface — every requirement should be defensible by reference to the cross-shard correctness contract.

## Scope bound

The library is bound to the **single-Postgres era** — from one Evennia process today through however many shards run against a single, vertically scaled Postgres. Per the [archived handover](../archive/evennia-shards-HANDOVER.md#project-identity-and-positioning), anything beyond that bound is explicitly out of scope.

## Library primitives (working set)

The library's value is a small set of cross-shard primitives rather than a consumer-facing typeclass mandate. Shipped primitives:

1. **Cross-shard message bus.** Foundational. A way for one shard's process to send a message to another. Postgres-table-polled via Twisted `LoopingCall`; library-shipped kinds include `obj_msg` and `account_msg` (player-facing message delivery) plus internal `ping` / `ping_received` / `undeliverable_reply`. See [cross-shard-message-bus.md](cross-shard-message-bus.md).
2. **Cross-shard messaging helper.** `send_cross_shard_message(target_pk, kwargs, target_typeclass=None)` — sender-side wrapper with single `.values_list` lookup, local-vs-remote dispatch, and typeclass filter (defaults to `BASE_CHARACTER_TYPECLASS`). Built on top of the bus's `obj_msg` primitive.
3. **Cross-shard movement.** `cross_shard_move(obj, target_shard, target_location_pk)` — atomic `qs.update` to retag the row's `shard_id` + `db_location_id`, recursive inventory move, idmapper eviction, per-session redirect via the ticket flow. See `evennia_shards/handoff.py` and [tenancy.md](tenancy.md) for the write-path detail.
4. **Cross-shard query helpers** *(implicit, not packaged as a primitive).* `.values()` / `.values_list()` reads of rows owned by another shard return data without instantiating typeclass objects locally (preserving the cache invariant). Used internally throughout the library; consumers can use the same idiom directly via Django.

Concrete patterns built on these primitives — e.g. a `CrossShardExit` typeclass that lets `east` cross shards transparently, or specialised cross-shard tells / channel propagation built on `send_cross_shard_message` — are not library responsibilities. They are candidates for `evennia_shards/contrib/` (analogous to `evennia/contrib/`), where the library developers or community may publish opt-in implementations consumers can import, extend, or ignore. The library core ships the primitives; contrib ships the patterns; the consumer chooses what to use.

## External dependencies

- **`django-multitenant`** (4.1.x). Used for tenant-context-driven auto-filtering at the SQL layer; see [tenancy.md](tenancy.md). The library wraps multitenant's mixins onto Evennia's `ObjectDB` at runtime rather than via subclassing.
- **No messaging dependency.** The cross-shard message bus uses a Postgres `messages` table polled via Twisted `LoopingCall` — see [cross-shard-message-bus.md](cross-shard-message-bus.md). The only infrastructure required by the library is Postgres, which Evennia already requires.

## Mandate: none

The library has no consumer-facing typeclass mandate. Consumers write rooms, characters, exits, and items the way they always did; cross-shard cases are handled inside the library's primitives (movement, messaging, tenancy). Any room can be a cross-shard movement target; any character on a remote shard can receive a message; nothing requires special marking.

This is a deliberate departure from the original handover sketch, which proposed a `ShardGatewayMixin` for "boundary rooms." The mixin idea was deprecated in favour of the primitive-based approach because:

- The mixin's three jobs (stable identification, cross-shard data access, handoff landing) all reduce to *"every row carries `(shard_id, pk)`"* plus the primitives, with no consumer-side marking required.
- A primitive-based design keeps the library invisible in the common case — the consumer's typeclasses don't change, the consumer's commands don't change, only the consumer's `settings.py` flips it from monolith into split mode.
- "Boundary rooms" as a concept conflates a *game-design* choice (which rooms are dramatic crossing points) with a *deployment* concern (which rows live on which shard). The library cares about the deployment concern only; the game-design concern is the consumer's.

The single integration step the library does ask of consumers is settings-level: declare `SHARDS_ROLE` and `SHARD_ID`, add `evennia_shards` to `INSTALLED_APPS` in non-monolith roles, and call `start_message_bus()` from `at_server_start`. There is no typeclass mandate beyond that.

## What the library does not provide

- **Game concepts.** The library does not ship typeclasses for rooms, characters, items, exits, doors, or any other game-domain entity. These belong to the consumer.
- **Build helpers for game entities.** No `get_or_create_room`, no exit factories, no NPC builders. Patterns for these may be documented elsewhere as advisory, but the library does not ship the code.
- **Operational policy.** The library does not dictate when or how the consumer's build script runs.
- **Higher-level organisational concepts** (zones, regions, areas, biomes). Whether and how a consumer groups rooms is their world-design choice; the library partitions at the **room** level, not at any higher organisational level.

## Two principles that drove the scoping

- **The library does nothing in monolith mode.** Any feature must justify its presence in monolith. Non-trivial behaviour is gated on `SHARDS_ROLE != "monolith"`. This keeps the "library is dormant by default" guarantee strong.
- **The library does not own game concepts.** The temptation to ship "useful" helpers (room factories, world bootstrappers, generic exit builders) is real and should be resisted. Every such helper imposes opinions on the consumer that may not fit their existing patterns. The smallest contribution that makes split deployment a config option is what should ship; future additions should pass the same test.
  - *Worked example.* `cross_shard_move` does not validate that the target location is a Room (vs. a Character, Item, Exit, etc.). A library-level "rooms only" rule would forbid legitimate consumer use cases — moving a character into a vehicle, mount, or container on another shard. The primitive validates only that the target row exists and is on the target shard; choice of valid destinations is the consumer's. See the function's docstring for the long form.
