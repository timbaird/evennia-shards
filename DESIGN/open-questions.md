# Open Questions

A tracker for design questions that have been raised in conversation but not yet resolved. As questions are worked through, they should be removed from this file and folded into whichever permanent doc is the right home (a settled design doc, a stub for a future doc, or just a captured decision).

This is not a place for speculation or invented problems — only questions that have come up in actual project conversation.

## Open

### Account-side messaging: which shard owns a logged-in account?

The library ships `obj_msg` and `account_msg` bus primitives plus a `send_cross_shard_message` sender helper that handles `ObjectDB` targets (characters, items, etc.). `ObjectDB` rows carry a `shard_id` column, so the sender helper can route an `obj_msg` to the right shard by reading the target's `shard_id`. `AccountDB` does not — there is no per-account shard column, and conceptually an account isn't *on* a shard the way a character is.

What's open is the **routing question** for an account-side equivalent: when a sender wants to deliver an `account_msg` (OOC tell, system notification, login banner) to an account that isn't currently puppeting any character on this shard, where does the message go?

- **If the account is logged in elsewhere**, the message needs to reach whichever shard or router currently holds their session.
- **If the account isn't logged in at all**, there's no current "owner" — the message is either undeliverable, queued for next login, or handled by a different mechanism entirely.
- **If the account is multi-session** (`MULTISESSION_MODE` 2/3), the answer compounds: deliver to all? primary? most-recent?

The bus primitive (`account_msg` in `messagebus.py`) handles delivery once a message *arrives* at the right shard. The unresolved part is the **directory layer** — how does the sender know which shard to address the bus row to. Possible shapes:

- **A session-tracking table.** Rows like `(account_pk, hosting_shard_id, last_seen)` updated on connect/disconnect. Sender consults the table.
- **Router-routed.** All `account_msg` traffic goes via the router, which knows session location from its own bookkeeping.
- **Broadcast and ignore.** Send to all shards; the one currently hosting the account delivers. Wasteful but simplest.

Each shape has trade-offs around latency, reliability under shard restart, and what happens during the brief windows when an account is between sessions.

This was raised as a follow-up when the `obj_msg` and `account_msg` primitives shipped on 2026-05-03 (see [progress.md](progress.md)). The `account_msg` primitive is in place; the routing layer is not.

### What cross-shard player interactions are feasible?

Some interactions are clearly impossible: real-time combat across shards requires mutation of state on another shard, ruled out by the first principle that any game object exists on exactly one shard. Some are clearly feasible: scry/locate spells that read character location via `.values()` queries against the shared DB, with no instantiation on the reading shard.

What sits in between — parties, follower trains, ambient effects on remote characters, channel-bridged interactions — depends on the messaging primitives the library provides and on creative use of them. The full set of feasible cross-shard interactions is implementation-discovery work, not pre-determinable.

Originally captured in `consumer-constraints.md` as "cross-shard player interactions are second-class" — that constraint was too strong; removed pending discovery.

### Can the room-to-shard partition be reshuffled without downtime?

The library partitions at the room level — every `ObjectDB` row carries a `shard_id`, and the `cross_shard_move` primitive can change a character's owning shard at runtime. What's unclear is whether the *room layout itself* can be reshuffled without downtime: moving rooms between shards (changing `shard_id` on a room and its contents) while a process is running, or carving out a new shard from rooms previously owned by another. The current working assumption is that such reshuffles are planned downtime events with full redeploy; whether the live machinery could support hot reshuffles without losing players is open. Likely deferred until a real consumer game wants to reorganise their world live.
