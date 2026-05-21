# Shard-aware global search

The substitute for `caller.search(name, global_search=True)` in code that runs on a sharded process and needs to see rows on *every* shard. Returns enough metadata for the caller to decide whether to use a loaded instance (local match) or route via cross-shard primitives (foreign match), without ever instantiating a foreign row.

## The problem

`ObjectDB.objects` carries the multitenant auto-filter (`WHERE shard_id IN (current, '*')`). A stock global search on a shard process therefore silently sees only local + global rows; foreign-shard rows are invisible. Anywhere the library or a consumer needs to find an object by name across the whole game world — admin commands, `@tel`, future cross-shard tells / who / where features — that's the wrong shape.

## The shape

```python
from evennia_shards import shard_aware_global_search, ShardSearchResult

result: ShardSearchResult = shard_aware_global_search(
    caller,
    name,
    tag=None,           # optional
    tag_category=None,  # optional, only consulted if tag is set
)
```

Inputs:

- `caller` — the `ObjectDB` instance triggering the lookup. Required only because the caller-relative specials (`"me"` / `"self"` / `"here"`) resolve against it; for any other input shape, `caller` is passed through unread.
- `name` — one of:
  - dbref (`"#42"`),
  - case-insensitive exact `db_key`,
  - case-insensitive object alias (Tag row with `db_tagtype="alias"`),
  - the caller-relative specials `"me"` / `"self"` (→ `caller`) or `"here"` (→ `caller.location`).
- `tag` (optional) — narrow the lookup to objects carrying this tag. When set, only rows with a matching tag participate in the search. Useful for scoping a key namespace to a smaller domain (e.g. a zone) so the same key can be reused without ambiguity across that domain.
- `tag_category` (optional) — only consulted when `tag` is set. When omitted, any category for the given tag key matches.

One output: a `ShardSearchResult` dataclass.

`ShardSearchResult` fields:

| Field | Type | Populated when |
|---|---|---|
| `state` | `"found"` / `"not_found"` / `"multiple"` | always |
| `obj` | loaded `ObjectDB` instance or `None` | `state == "found"` and match is local |
| `pk` | int | `state == "found"` |
| `shard_id` | str | `state == "found"` |
| `db_key` | str | `state == "found"` |
| `candidates` | list of `(pk, shard_id, db_key)` triples | `state == "multiple"` |
| `is_local` | bool (property) | derived from `shard_id` and `get_shard_id()` |
| `is_cross_shard` | bool (property) | derived: `state == "found"` and not `is_local` |

Three states drive caller dispatch:

- **`found`** — exactly one match. If `is_local`, `obj` is the loaded instance and the caller can use it as it would a vanilla search result. If `is_cross_shard`, `obj` is `None`; the caller routes via `cross_shard_move` (or another cross-shard primitive) using `pk` and `shard_id`.
- **`not_found`** — no match. The caller emits its own not-found message.
- **`multiple`** — more than one match. `candidates` is populated so the caller can render its own disambiguation prompt with shard context (e.g. "found `room` on shard0 (#5) and shard1 (#2238) — specify by dbref").

## The mechanism

Three stages, in order:

0. **Caller-relative short-circuit.** `name` is lowered and stripped; if it's `"me"` / `"self"`, the helper returns the caller as the match without hitting the database. If it's `"here"`, the helper reads `caller.location` (always local by construction — the caller is on this process) and returns that. These tokens never reach the SQL path. Vanilla `caller.search` handles them the same way.
1. **SQL-level metadata query, scope-escaped.** The queryset is *constructed inside* `with shard_context(None):` so the multitenant auto-filter doesn't bake into the `WHERE` clause: `ObjectDB.objects.filter(...).values_list("pk", "shard_id", "db_key")` then sees rows on every shard. The match predicate is `db_key` OR an alias tag (`db_tags__db_key` AND `db_tags__db_tagtype="alias"`) — `.distinct()` collapses the m2m duplicates the OR produces. For dbref input the predicate is just `pk=`. When set, `db_tags__db_key` / `db_tags__db_category` for tag scoping is composed as a separate `filter()` call so it gets its own join (the alias join and the tag-scope join then restrict the parent ObjectDB row independently — which is the intended semantics). `values_list` keeps the read SQL-only — no row instantiation, no FK dereferences, so leaving the scope here can't accidentally load a foreign row's related objects.
2. **Conditional instantiation.** Once the match's shard is known:
   - If the match is local (`shard_id == get_shard_id()` or `shard_id == "*"`), load the instance via the regular (auto-filtered) ORM (`ObjectDB.objects.get(pk=...)`). The match's shard is known to be in scope, so the auto-filter passes the row through.
   - If the match is on another shard, the helper returns the metadata only.

The caller never receives a foreign-shard instance.

### Why the alias predicate uses `db_tagtype`, not `db_category`

Aliases in Evennia are stored in their own tag-type slot (`AliasHandler._tagtype = "alias"`, see `evennia/typeclasses/tags.py`). `db_category` on a Tag row is orthogonal — aliases can carry any category. The match predicate therefore checks `db_tagtype="alias"`, mirroring vanilla `ObjectDBManager.object_search`. A non-alias tag whose key happens to match the search string (e.g. a zone tag named `"sword"`) won't match, which is the correct behaviour.

### Why tag scoping and shard scoping compose

Tag filtering narrows by the m2m join through `db_tags`; shard filtering would narrow by the `shard_id` column on `ObjectDB`. The two are orthogonal — different columns / joins — and Django ANDs them into the same SQL WHERE. Either can be added independently; combining both just narrows the candidate set further. Every row that survives the filter still carries `shard_id`, so the helper's routing logic (`is_local` / `is_cross_shard`) works unchanged regardless of how many filters were applied.

In FCM's design, a zone never spans shards — so when the consumer scopes by zone tag, the tag scope effectively pre-scopes by shard too (every zone-tagged row will have the zone's owning shard_id). The helper still reports the shard_id explicitly, so callers don't have to know that invariant to dispatch correctly.

## Consumer usage pattern

The helper is the recommended substitute for any `caller.search(name, global_search=True)` call site in code that runs on a sharded process. The substitution is one-for-one — vanilla's three search calls in `CmdTeleport.parse`, for example, become three calls to `shard_aware_global_search` in `ShardAwareCmdTeleport.parse` (see [library-integration-risks.md](library-integration-risks.md) § `CmdTeleport` for the worked example).

## Scope

The current implementation handles:

- dbref lookups (`"#42"`).
- Case-insensitive exact `db_key` matches.
- Case-insensitive object alias matches (Tag rows with `db_tagtype="alias"`).
- Caller-relative specials `"me"` / `"self"` (→ `caller`) and `"here"` (→ `caller.location`).
- Optional tag scoping via `tag` / `tag_category` to narrow the key namespace (e.g. zone-scoped lookup).

The current implementation does not handle:

- Partial / fuzzy name matches (vanilla `caller.search`'s regex fallback when exact fails).
- Locality-aware preference ordering when both local and remote matches exist for the same name.

These are extensions that fit cleanly under the same call signature when a consumer needs them. The helper's contract — name (plus optional tag) in, `ShardSearchResult` out — is stable.

## Related

- [`library-integration-risks.md`](library-integration-risks.md) § `CmdTeleport` — the first consumer of this helper.
- [`tenancy.md`](tenancy.md) — the underlying multitenant auto-filter this helper escapes.
