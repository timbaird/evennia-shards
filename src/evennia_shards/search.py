# SPDX-License-Identifier: BSD-3-Clause
"""Shard-aware global search.

Substitute for ``caller.search(name, global_search=True)`` in code that
must run on a sharded process and needs to see rows on *every* shard.
The default ``ObjectDB.objects`` manager carries the multitenant
auto-filter, so a stock global search would silently miss foreign rows
— exactly the rows this helper exists to find.

The helper does the lookup in two steps:

1. ``values_list`` for ``(pk, shard_id, db_key)`` of every matching row,
   run inside ``shard_context(None)`` to escape the auto-filter and see
   every shard. The values-only query never instantiates a row.
2. Once the match's shard is known:
   - If the match is local (current shard or the global ``"*"`` sentinel),
     load the instance via the regular (auto-filtered) ORM so callers
     get full Evennia semantics for the local path.
   - If the match is on another shard, return the metadata only —
     callers route via the library's cross-shard primitives instead of
     loading.

The result is a :class:`ShardSearchResult` with explicit ``state``
(``"found"`` / ``"not_found"`` / ``"multiple"``) and the per-match data
the caller needs to dispatch.

Scope note: this initial cut handles dbref lookups (``#42``),
case-insensitive exact ``db_key`` matches, alias matches (Tag rows
with ``db_tagtype="alias"``), and the caller-relative specials
``"me"`` / ``"self"`` / ``"here"`` that vanilla ``caller.search``
supports. Partial / fuzzy name matching (vanilla's regex fallback
when exact fails) is not implemented yet; if a consumer needs it the
helper can grow without changing the call shape.
"""

from dataclasses import dataclass, field
from typing import Optional

from .config import get_shard_id
from .tenancy import shard_context


@dataclass
class ShardSearchResult:
    """Outcome of :func:`shard_aware_global_search`.

    Three mutually-exclusive states drive caller dispatch:

    - ``"found"``: exactly one match. ``pk``, ``shard_id``, ``db_key``
      are populated. ``obj`` is the loaded instance when the match is
      local; ``None`` when the match is on another shard.
    - ``"not_found"``: no match. All instance/metadata fields ``None``.
    - ``"multiple"``: more than one match. ``candidates`` holds a list
      of ``(pk, shard_id, db_key)`` triples for the caller to render
      disambiguation against.
    """

    state: str  # "found" | "not_found" | "multiple"
    obj: Optional[object] = None
    pk: Optional[int] = None
    shard_id: Optional[str] = None
    db_key: Optional[str] = None
    candidates: list = field(default_factory=list)

    @property
    def is_local(self) -> bool:
        """True iff the match is loadable on this process."""
        if self.state != "found":
            return False
        return self.shard_id == get_shard_id() or self.shard_id == "*"

    @property
    def is_cross_shard(self) -> bool:
        """True iff the match exists but lives on a different shard."""
        return self.state == "found" and not self.is_local


def shard_aware_global_search(
    caller,
    name: str,
    tag: Optional[str] = None,
    tag_category: Optional[str] = None,
) -> ShardSearchResult:
    """Look up an object by name across all shards, chokepoint-safely.

    Args:
        caller: the ``ObjectDB`` instance triggering the lookup. Currently
            unused — included for symmetry with ``caller.search`` so
            future locality / permission-scoped behaviour can be added
            without changing the signature.
        name: dbref string (``"#42"``) or db_key (case-insensitive
            exact match).
        tag: optional tag key to narrow the search to objects carrying
            this tag. When set, only rows with a matching tag are
            considered. The common consumer pattern is to scope the
            lookup to a zone (e.g. ``tag="millholm", tag_category="zone"``)
            so a single key can be reused across zones without ambiguity.
        tag_category: optional tag category. Only consulted when ``tag``
            is set. When omitted, any category for the given tag key
            matches.

    Returns:
        :class:`ShardSearchResult` — see class docstring for the
        state-driven contract.
    """
    from evennia.objects.models import ObjectDB

    name = name.strip()
    if not name:
        return ShardSearchResult(state="not_found")

    # Caller-relative specials. Vanilla caller.search resolves these
    # before hitting the DB: "me"/"self" → the caller; "here" → the
    # caller's location. Always local by construction — the caller is
    # running this command on this process, so the caller row (and any
    # location it's currently in) are reachable without any cross-shard
    # routing. Degenerate case: a caller whose db_location_id points at
    # a foreign-shard room will return None for caller.location (the
    # auto-filter excludes the foreign row from the FK dereference) —
    # treated as "no location" in the "here" branch below.
    lowered = name.lower()
    if lowered in ("me", "self"):
        return ShardSearchResult(
            state="found",
            obj=caller,
            pk=caller.pk,
            shard_id=caller.shard_id,
            db_key=caller.db_key,
        )
    if lowered == "here":
        loc = caller.location
        if loc is None:
            return ShardSearchResult(state="not_found")
        return ShardSearchResult(
            state="found",
            obj=loc,
            pk=loc.pk,
            shard_id=loc.shard_id,
            db_key=loc.db_key,
        )

    # dbref lookup
    pk_to_search: Optional[int] = None
    if name.startswith("#"):
        try:
            pk_to_search = int(name[1:])
        except ValueError:
            pk_to_search = None

    # Escape the multitenant auto-filter so we can see rows on every
    # shard. The queryset must be *built* inside the context — our
    # patched ``get_queryset`` reads ``get_current_tenant()`` at the
    # moment ``ObjectDB.objects`` is dereferenced, not when the query
    # executes. ``values_list`` keeps the read SQL-only — no row
    # instantiation, no FK dereferences, so leaving the scope here
    # can't accidentally load a foreign row's related objects.
    with shard_context(None):
        if pk_to_search is not None:
            qs = ObjectDB.objects.filter(pk=pk_to_search)
        else:
            # Match by db_key OR by an object alias. Aliases are stored
            # as Tag rows with db_tagtype="alias" — note tagtype, NOT
            # category; AliasHandler._tagtype = "alias" in
            # evennia/typeclasses/tags.py. The AND inside the OR keeps
            # the two tag predicates pinned to the same Tag row (within
            # one filter() call, repeated db_tags__* lookups share one
            # join), so we don't accidentally match objects that
            # happen to carry a non-alias tag with the target key.
            # distinct() collapses duplicates produced by the m2m OR.
            # Matches vanilla's pattern in evennia/objects/manager.py
            # ObjectDBManager.object_search.
            from django.db.models import Q
            qs = ObjectDB.objects.filter(
                Q(db_key__iexact=name)
                | (Q(db_tags__db_key__iexact=name)
                   & Q(db_tags__db_tagtype__iexact="alias"))
            ).distinct()

        # Optional tag filter. Composes onto the same queryset — Django
        # joins ObjectDB to its m2m Tag table and ANDs the predicates
        # into one SQL WHERE clause. Cross-shard scoping and tag
        # filtering are orthogonal — each narrows the result set
        # independently, and any row that makes it past the filter
        # still carries shard_id for the routing logic below.
        if tag is not None:
            qs = qs.filter(db_tags__db_key=tag)
            if tag_category is not None:
                qs = qs.filter(db_tags__db_category=tag_category)

        rows = list(qs.values_list("pk", "shard_id", "db_key"))

    if not rows:
        return ShardSearchResult(state="not_found")
    if len(rows) > 1:
        return ShardSearchResult(state="multiple", candidates=rows)

    matched_pk, matched_shard, matched_key = rows[0]
    is_local = matched_shard == get_shard_id() or matched_shard == "*"

    if is_local:
        # Load the instance via the regular ORM — safe because the row
        # is on this shard (or the global sentinel) and from_db will
        # accept it.
        try:
            obj = ObjectDB.objects.get(pk=matched_pk)
        except ObjectDB.DoesNotExist:
            # Race: row vanished between values_list and get. Treat as
            # not-found.
            return ShardSearchResult(state="not_found")
        return ShardSearchResult(
            state="found",
            obj=obj,
            pk=matched_pk,
            shard_id=matched_shard,
            db_key=matched_key,
        )

    # Cross-shard match — return metadata only; caller routes via
    # cross_shard_move or equivalent primitive.
    return ShardSearchResult(
        state="found",
        obj=None,
        pk=matched_pk,
        shard_id=matched_shard,
        db_key=matched_key,
    )
