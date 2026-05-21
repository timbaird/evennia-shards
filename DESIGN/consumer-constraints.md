# Consumer Constraints

What this library demands of the consumer game that adopts it. Adoption is not free — the architecture imposes constraints that propagate into the consumer's world design, code structure, and feature set.

This document captures constraints explicitly discussed.

## Constraints

### Any game object exists on exactly one shard

Every game object — room, character, item, exit — exists on exactly one shard. Accounts exist only on the router. (For this rule, treat the router as a special-purpose shard that holds accounts; "shard" then stands generally for any process that owns a partition of the system's data.)

Several consequences flow from this first principle:

- Code on shard A cannot hold a Python reference to an object that lives on shard B. References resolve only inside the process where the object exists.
- Cross-shard reads must not pull objects into the reader's idmapper. Use `.values()` queries returning dicts, not typeclass instances.
- Any feature that mutates state on an object the current process does not own is incompatible with the architecture; mutations must be routed to (or scheduled by) the owning process.
- Any interaction requiring multiple characters to be in the same room (combat, in-room trade) is shard-local by construction: a room exists on one shard, so any character in it must be on that shard. Cross-shard combat is therefore impossible.

### Cross-shard movement requires a safe character state

Cross-shard movement should only happen when the character is in a "safe" state — not in combat, not casting, no in-flight delayed callbacks that assume the character stays on this shard. The library's `cross_shard_move` primitive does not enforce this itself (per principle 3: "in combat" is a game concept); the consumer is responsible for calling the primitive only when their game's safe-state predicate holds. Consumer-side typeclass code (a `CrossShardExit`, a teleport command, an admin tool) is the right place for that check.

### No live mid-action session migration

Crossing a shard boundary is a brief reconnect on the web client. Features that span a multi-step interaction across the boundary (a long ritual whose middle step is on another shard, a chase across multiple shards with transitions in flight) are incompatible with the handoff model. Ties directly to the safe-state requirement above.

### Single Postgres

The architecture assumes one logical Postgres database, vertically scaled. Read replicas, sharded databases, multi-region writes are out of scope and the library will not develop them.

### Cross-shard movement is a narrative beat

The library treats cross-shard movement as a UX concept. The brief reconnect (visible to telnet, invisible to web clients) is acceptable precisely because the transition is narratively distinct from regular movement. Consumer world design should make cross-shard transitions feel like deliberate beats — portals, docks, trailheads, passages, fast-travel — rather than indistinguishable from regular exits.

The library ships the `cross_shard_move` primitive; concrete patterns built on it — e.g. a `CrossShardExit` typeclass that lets `east` cross shards transparently — may land in `evennia_shards/contrib/` over time, contributed by the library developers or community. Such an implementation would weaken this constraint: once `east` *can* cross a boundary indistinguishably from a normal exit, the transition stops feeling like a deliberate beat. That's not a library decision — consumers choose whether to import a transparent contrib exit, write their own deliberate-beat exit, or ignore both and call the primitive directly. The trade-off (UX latency on every move vs. narrative distinctiveness on shard crossings) is a consumer-side game-design choice.
