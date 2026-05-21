# Shard Isolation Mechanism (archived)

> **Superseded by [tenancy.md](tenancy.md).** This document describes the earlier four-chokepoint design. The chokepoint machinery (`isolation.py`, the `pre_save` / `pre_delete` signals, the `from_db` and `QuerySet.update` patches, the `shard_writes_allowed_for` bypass, and `ShardIsolationError`) does not exist in the codebase. Kept as historical reference, primarily for the design rationale in the "Decision: bespoke chokepoints vs `django-multitenant`" section — useful background for *why* the library ended up where it is.

How the library enforces the partition between shards at the Django/Evennia level — what stops one shard's process from accidentally reading, instantiating, or writing to another shard's rows.

The mechanism has two halves: **chokepoints** that catch accidental cross-shard access loudly, and a scoped **bypass** primitive (`shard_writes_allowed_for`) that legitimate cross-shard operations use to opt out. Both live in [`evennia_shards/isolation.py`](../src/evennia_shards/isolation.py) and share thread-local state, so the enforcement and the opt-in coordinate without cross-module plumbing.

## Invariants

The library enforces two invariants:

1. **A non-owning shard never instantiates an object it doesn't own.** No remote-shard `ObjectDB` row may be constructed as a Python instance on this shard. (The "owner" of a row is the shard whose `SHARD_ID` matches the row's `shard_id` column. The sentinel `"*"` denotes a row owned by all shards — see [shard-settings.md](shard-settings.md).)
2. **A non-owning shard never persists changes to an object it doesn't own.** No save, update, or delete may commit to a remote-shard row.

These together prevent cache poisoning, cross-shard data corruption, and silent state divergence.

## Router exemption

The router is exempt from all four chokepoints. It needs unrestricted access to ObjectDB because it is the coordinator for OOC operations that span shards:

- **Reads**: deserializing `_last_puppet` to resolve which character (and which shard) to redirect to on login.
- **Writes**: character creation (chargen) — the router creates characters and stamps them with the target shard's `shard_id`. The stamp is derived from the new character's `db_location_id` row's `shard_id` by a wrapper around `Account.create_character`; see [library-integration-risks.md](library-integration-risks.md#accountcreate_character-wrapper) and `evennia_shards/chargen.py`.
- **Deletes**: character deletion is an OOC operation handled by the router.

The isolation rule is: **shards are isolated from each other; the router is trusted**. In the chokepoint logic, the check becomes "am I a shard and is this object owned by a different shard?" rather than "is this object owned by a different shard?"

## The four chokepoints

| # | Hook | Covers | Rule |
|---|---|---|---|
| 1 | **`Model.from_db()` override on `ObjectDB`** | All read paths that construct an instance from DB row data — normal queryset iteration, `raw()`, `select_related()`. Verified in Django source: three call sites, all use this method. | Refuses construction when `row.shard_id != current_shard` (and isn't `"*"`). |
| 2 | **`pre_save` signal handler on `ObjectDB`** | Every `instance.save()` (whether triggered by code, typeclass factory, or post-load resave). | Refuses if `instance.shard_id != current_shard`. *(The same signal also runs the auto-stamp logic for new rows where `shard_id is None`; the two behaviours coexist via the same handler.)* |
| 3 | **`pre_delete` signal handler on `ObjectDB`** | Both `instance.delete()` and `qs.delete()` — Django fires `pre_delete` per affected row, even for queryset bulk deletes (it has to, for cascade handling). | Refuses if `instance.shard_id != current_shard`. |
| 4 | **`QuerySet.update()` override** *(in a thin custom QuerySet on the `ObjectDB` manager)* | Queryset bulk updates — the one write operation Django does **not** fire signals for. | Refuses if the queryset would touch any row whose `shard_id` is neither current nor `"*"`. Loud failure, consistent with the other three chokepoints. |

Plus, for ownership handoff: `instance.flush_from_cache()` — Evennia's idmapper exposes this; the handoff protocol calls it on the source shard after writing the new ownership. The legitimate writes themselves go through the bypass primitive described below.

## Why this set is sufficient

- **Reads are exhaustive.** Every Django read path that produces a `Model` instance from a row goes through `from_db()`. Verified by source inspection — three call sites (`ModelIterable`, `RawModelIterable`, `RelatedPopulator`), no others.
- **Writes are exhaustive.** Instance saves fire `pre_save`; instance deletes fire `pre_delete`; queryset deletes fire `pre_delete` per row; queryset updates are caught by the QuerySet override.
- **Each chokepoint is a low-level Django-native extension point.** No idmapper subclassing, no broad manager replacement that filters every query, no monkey-patching beyond `add_to_class` and signal connections.
- **Failure mode is "raise and surface,"** not silent skip. If any chokepoint fires, it means a leak vector reached a place it shouldn't have — the stack trace points at the calling code so the bug can be fixed.

## What is *deliberately* not enforced

The chokepoints cover **instantiation, persistence, and per-row mutation**. They do not cover scalar SQL inspection or row-data extraction that never builds an instance:

- **Aggregate operations** (`count()`, `exists()`, `aggregate(...)`) return cross-shard answers. They don't construct instances or modify data; they return scalars. Wrong-but-not-damaging. Consumers should be aware that "how many characters exist in this table?" is a global question.
- **`.values()` / `.values_list()`** return dicts/tuples of column data directly from rows, never going through `from_db`. So `Character.objects.values("db_key", "shard_id", "db_location_id")` happily pulls field data for remote rows. No instance is constructed and nothing is persisted, so neither invariant is violated — but consumers should know that row-data inspection is a global question, the same way aggregates are.
- **Raw cursor SQL** (`with connection.cursor() as cur: cur.execute(...)`) bypasses every chokepoint by definition. Discipline-dependent — the same as it would be in any Django app. Documented as a consumer responsibility.
- **Pickling/unpickling typeclass instances** bypasses `from_db`. Rare in practice; not currently a concern. Can be addressed if it ever surfaces.

Worth disambiguating: **`Manager.raw()` *is* covered** — its iteration goes through `RawModelIterable`, which is one of `from_db`'s three call sites. People sometimes lump `raw()` with cursor SQL, but Django actually hooks raw queries through the same construction path.

The general framing: the library prevents **cache poisoning, cross-shard data corruption, and silent state divergence**. It does not provide an information wall — a shard process can still ask scalar or row-data questions about rows it doesn't own. If we wanted information walls we'd be in schema-based multi-tenancy territory.

## How this meets the architectural goals

- **Library does nothing in monolith mode.** Hooks register only when the library is in `INSTALLED_APPS`, which the consumer's settings only do when `SHARDS_ROLE != "monolith"`.
- **No surprising consumer-side behaviour change.** Normal Evennia code — `obj.save()`, `obj.delete()`, `Character.objects.get(pk=42)`, FK lookups — works exactly as before in monolith and works correctly-scoped in shard mode.
- **Targeted enforcement.** Each chokepoint solves one specific concern without affecting unrelated behaviour. Aggregates and reads of single rows on the current shard pay zero overhead.
- **Clear failure mode.** Invariant violations raise visibly rather than silently producing wrong data — easy to find and fix during development.

## Bypass: `shard_writes_allowed_for`

The chokepoints catch the >99% of accidental cross-shard access that is the *bug* case. The library also ships a scoped opt-in for the rare *legitimate* cross-shard operations — ownership handoff, recovery tooling, data migrations — that need to write across the partition deliberately:

```python
from evennia_shards import shard_writes_allowed_for

with shard_writes_allowed_for(character):
    character.shard_id = "shard1"
    character.location_id = remote_room_pk
    character.save()
# back outside — chokepoints active again
```

Inside the `with` block, all four chokepoints skip enforcement for the listed objects. Caller takes responsibility for the integrity of the writes performed inside.

**How it tracks identity.** Two thread-local sets, populated by entering the bypass and torn down on exit:

- `id(instance)` — used by `pre_save` and `pre_delete`, which receive the model instance directly. Works for both saved and unsaved rows (a freshly-created object has no pk yet but always has a Python id).
- `(concrete_model, pk)` — used by `from_db` (which receives `cls` + raw row values) and `QuerySet.update` (which queries affected pks via `values_list`). Keys are stored under the *concrete* model class via `_meta.concrete_model`, so a bypass entered with a typeclass instance (a Django proxy of `ObjectDB`) matches a `from_db` call where `cls` is `ObjectDB` itself.

**Scoping.** The `with` block's exit removes only the entries this call added. Nesting is safe — an outer bypass keeps its objects authorised when an inner bypass exits. On exception, cleanup still runs (the contextmanager's `finally`).

**What it doesn't do.** It is *not* a "disable shard isolation globally" switch. The bypass set is keyed per-object; objects not listed remain protected, even inside the `with` block. There is no "allow all writes" mode, deliberately — the bypass should be sharp and targeted.

**Composing with `transaction.atomic()`.** For multi-write operations like `cross_shard_move`, callers combine the bypass with `transaction.atomic()` so that the writes either all commit or all roll back. The two primitives compose freely; the bypass doesn't impose any transaction semantics of its own.

For ownership handoff specifically, idmapper eviction (`instance.flush_from_cache()`) is the third moving part — eviction happens inside the same atomic block so a flush failure rolls the DB write back, and a defensive eviction also runs in the `except` branch so a rolled-back move doesn't leave a stale Python instance (with the mutated `shard_id`) in the source process's idmapper. The `cross_shard_move` primitive in [`evennia_shards/handoff.py`](../src/evennia_shards/handoff.py) composes the three (bypass + atomic + flush) into a single operation; consumers writing their own cross-shard orchestration code use the bypass directly with their own composition.

## Cross-process cache staleness

The chokepoints enforce correct *write* behaviour. A separate concern is **read staleness**: when one process updates a row (e.g. `cross_shard_move` changes `shard_id` and `db_location_id`), other processes' in-memory caches — both the idmapper and Evennia's Attribute-handler cache — still hold the old values. Two specific cache layers cause problems:

### Idmapper (`SharedMemoryModelBase.__call__`)

Evennia's `SharedMemoryModelBase` metaclass caches model instances by pk. Django's `Model.from_db()` calls `cls(*values)`, which the metaclass intercepts — if the pk is already cached, it returns the cached instance **ignoring the fresh DB values**. This makes Django's `refresh_from_db()` a no-op for cached instances: the queryset evaluation goes through `from_db`, gets back the stale cached object, and `refresh_from_db()` copies fields from that stale object back to itself.

**Fix pattern:** call `instance.flush_from_cache(force=True)` before `refresh_from_db()`. Evicting the cache entry forces `from_db` to construct a fresh instance from the DB row, which `refresh_from_db()` then copies into the caller's reference.

Applied in:
- `ShardAwareCmdIC.func()` — router reads character's `shard_id` for redirect target.
- `_is_redirectable_character()` — router's `at_post_login` checks character's `shard_id`.
- `make_shard_at_post_login` wrapper — shard's `at_post_login` refreshes `_last_puppet` before auto-puppet.

### Account Attribute-handler cache

Evennia's `AttributeHandler` (the `.db` accessor) caches deserialized Attribute values on the model instance. When code sets `account.db._last_puppet = character`, the handler writes to the DB *and* caches the live Python object. If another process later updates the same Attribute row, this process's cache still holds the old Python reference — with stale field values baked in.

This is distinct from the idmapper: even after flushing the idmapper entry, the Attribute cache can still serve a direct reference to the old Python object (which the idmapper no longer tracks).

**Fix pattern:** the shard-side `at_post_login` wrapper reads `_last_puppet` from the Attribute cache (getting the potentially-stale object), then flushes and refreshes it. This updates the stale object's fields in-place from the DB, so when Evennia's original `at_post_login` hands it to `puppet_object`, the fields are current.

### General principle

Any code path that reads an `ObjectDB` field which may have been updated by another process must flush the idmapper entry and refresh from the DB before acting on it. The `flush_from_cache(force=True)` + `refresh_from_db()` pair is the standard pattern. `values_list()` queries bypass both caches entirely and are safe for cross-process reads that don't need an instance.

## Decision: bespoke chokepoints vs `django-multitenant`

Two approaches were on the table:

- **Bespoke chokepoints** *(this document)* — four narrow Django-native hooks (`from_db`, `pre_save`, `pre_delete`, `QuerySet.update`) plus a `shard_id` column. No external dependency.
- **`django-multitenant`** — off-the-shelf library providing `tenant_id` row-tagging + auto-filtering manager. Conceptually the same row-based partitioning shape; differs in *how* isolation is enforced (filter all queries to scope vs. raise on out-of-scope access).

Both were prototyped in parallel (the bespoke approach on this branch, `django-multitenant` on its own branch). After the bespoke spike landed end-to-end with all four chokepoints + tests + the cross-shard message bus on top, the `django-multitenant` branch was discontinued without merging. The reasons:

- **Clean composition with Evennia's idmapper.** Evennia's `SharedMemoryManager` is a custom Django manager that overrides `get_queryset()`. `django-multitenant`'s auto-filtering also works by manager composition through the same seam — running both required a layered manager whose interaction with the idmapper's `(class, pk)` cache would have needed careful prototyping. The chokepoint approach uses Django-native extension points (`from_db`, signals, queryset method patch) that don't touch the manager, so the idmapper composition question doesn't arise.
- **Loud failures, not silent filtering.** A chokepoint that catches a leak raises with a stack trace pointing at the calling code. An auto-filtering manager simply hides the wrong-shard rows — wrong data scoped away, but the underlying bug (code that shouldn't be looking at remote rows) becomes invisible. For a library positioning itself as an extension to Evennia, the loud failure mode is the safer default during development.
- **No external runtime dependency.** `django-multitenant` was originally built for Citus (Postgres distributed sharding extension); it claims plain-Postgres support but that was an open question on the comparison branch. The bespoke approach has zero new dependencies — it's just library code wired through Django/Evennia primitives the consumer is already using.
- **Minimal blast radius.** The chokepoint surface is four hook registrations plus one column. It is straightforward to read, reason about, and remove. `django-multitenant` integration would have meant taking on a third-party library's release cadence, model, and edge cases as part of the library's own contract.

Aggregate / `.values()` / `.values_list()` behaviour was *not* a deciding factor — both approaches can express scoped or unscoped aggregates by placing or omitting a `WHERE` clause, so it doesn't differentiate them meaningfully.

The `django-multitenant` branch and the open questions tied to it (manager composition with the idmapper, plain-Postgres compatibility) are no longer being explored; both are resolved by this decision in the sense that they no longer block any choice we need to make.
