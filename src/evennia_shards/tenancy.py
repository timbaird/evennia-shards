# SPDX-License-Identifier: BSD-3-Clause
"""Shard tenancy: thin adapter over django-multitenant.

Bridges the library's shard vocabulary (``shard_id``, ``"*"`` global sentinel,
router exemption) onto django-multitenant's tenant context primitives. The
adapter is deliberately thin — multitenant does the filter injection, the
auto-stamp on insert, and the UPDATE WHERE clause rewriting. This module
only handles:

- The :class:`Shard` stand-in object so multitenant's ``tenant_value``
  protocol works without us declaring a Django tenant model.
- The two-element list trick (``[Shard(current), Shard("*")]``) that makes
  multitenant's auto-filter produce ``WHERE shard_id IN (current, '*')``
  natively, without us subclassing any QuerySet or Manager.
- The :func:`shard_context` context manager: scoped tenant switching for
  legitimate cross-shard operations (handoff, chargen, admin tooling).
- The :func:`clear_shard_context` helper: explicit unscoped mode for the
  router process.

The module is import-safe before Django settings are configured — it does
not read settings or models at import time.
"""

from contextlib import contextmanager

from django_multitenant.utils import (
    get_current_tenant,
    set_current_tenant,
    unset_current_tenant,
)


GLOBAL_SHARD_ID = "*"
"""Sentinel ``shard_id`` value for rows visible from every shard.

Used by globals (built-in help, system messages, certain shared world
fixtures). The auto-filter built by :func:`set_current_shard` always
includes ``"*"`` rows alongside the current shard's rows.
"""


class Shard:
    """Lightweight stand-in for a multitenant tenant model.

    django-multitenant resolves the current tenant's filter value by reading
    ``tenant.tenant_value``. By exposing that attribute on a plain class we
    avoid having to declare a Django ``Shard`` model just to satisfy the
    protocol. The class carries no state beyond the shard id and is cheap
    to instantiate.

    Equality and hashing are by ``tenant_value`` so a :class:`Shard` works
    correctly in sets, dict keys, and ``in`` checks.
    """

    __slots__ = ("tenant_value",)

    def __init__(self, shard_id: str) -> None:
        self.tenant_value = shard_id

    def __repr__(self) -> str:
        return f"Shard({self.tenant_value!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Shard):
            return self.tenant_value == other.tenant_value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.tenant_value)


def _scope_for(shard_id: str) -> list:
    """Build the two-element scope list multitenant uses for ``IN`` filtering.

    ``[Shard(shard_id), Shard(GLOBAL_SHARD_ID)]`` makes
    ``get_current_tenant_value()`` return ``[shard_id, "*"]``, which
    ``get_tenant_filters()`` then turns into
    ``{"shard_id__in": [shard_id, "*"]}`` — the auto-filter we want.
    """
    return [Shard(shard_id), Shard(GLOBAL_SHARD_ID)]


def set_current_shard(shard_id: str) -> None:
    """Set the active shard for this process (or thread).

    All ORM queries on tenant-tagged models will scope to rows whose
    ``shard_id`` is either ``shard_id`` or ``"*"``. Called once at process
    startup for shard-role processes; the router does not call this and
    instead operates unscoped via :func:`clear_shard_context`.
    """
    set_current_tenant(_scope_for(shard_id))


def clear_shard_context() -> None:
    """Drop the current shard context. Subsequent queries are unscoped.

    Used by the router process (and by admin / migration tooling) to see
    every row regardless of ``shard_id``. Equivalent to calling
    multitenant's ``unset_current_tenant()`` directly; provided here as
    the library-vocabulary alias.
    """
    unset_current_tenant()


def _install_global_query_decorators() -> None:
    """Install the global Django query decorators multitenant relies on.

    Normally ``TenantModelMixin.__init__`` runs these on the first model
    instantiation. Because we late-bind onto ``ObjectDB`` rather than
    putting the mixin in its MRO, the ``__init__`` side-effects never
    fire automatically — so we trigger them here at install time.

    What each decorator does, briefly:

    - ``DeleteQuery.get_compiler`` — injects the tenant filter into the
      WHERE clause of DELETE statements.
    - ``Collector.related_objects`` / ``Collector.delete`` — propagates
      the tenant filter through cascade deletes so child rows aren't
      collected from other shards.
    - ``UpdateQuery.update_batch`` — same as above, for ``QuerySet.update``
      and bulk ``UPDATE WHERE pk IN (...)`` statements.
    - ``create_forward_many_to_many_manager`` — stamps the tenant id on
      M2M through-table inserts when added via the related manager.

    Each is idempotent via a ``_sign`` marker attribute set by the wrap
    helpers; re-running is a no-op.
    """
    import django
    from django.db.models.deletion import Collector
    from django.db.models.fields.related_descriptors import (
        create_forward_many_to_many_manager,
    )
    from django.db.models.sql import DeleteQuery, UpdateQuery
    from django_multitenant.deletion import related_objects
    from django_multitenant.mixins import wrap_forward_many_to_many_manager
    from django_multitenant.query import (
        wrap_delete,
        wrap_get_compiler,
        wrap_update_batch,
    )

    if not hasattr(DeleteQuery.get_compiler, "_sign"):
        DeleteQuery.get_compiler = wrap_get_compiler(DeleteQuery.get_compiler)
        Collector.related_objects = related_objects
        Collector.delete = wrap_delete(Collector.delete)

    if not hasattr(create_forward_many_to_many_manager, "_sign"):
        django.db.models.fields.related_descriptors.create_forward_many_to_many_manager = (
            wrap_forward_many_to_many_manager(create_forward_many_to_many_manager)
        )

    if not hasattr(UpdateQuery.get_compiler, "_sign"):
        UpdateQuery.update_batch = wrap_update_batch(UpdateQuery.update_batch)


def install_tenancy_on_objectdb() -> None:
    """Late-bind django-multitenant tenancy onto Evennia's ``ObjectDB``.

    We can't put :class:`~django_multitenant.mixins.TenantModelMixin` /
    :class:`~django_multitenant.mixins.TenantManagerMixin` in ``ObjectDB``'s
    MRO via subclassing — Evennia and its consumers depend on the exact
    ``ObjectDB`` class. So we attach the relevant behaviour at runtime:

    1. **TenantMeta** — tells multitenant which column on this model is
       the tenant field (``shard_id``).
    2. **tenant_* properties** — copied from ``TenantModelMixin``. They
       don't use ``super()`` so they transplant cleanly.
    3. **save / _do_update / __setattr__** — wrapped (not copied) because
       ``TenantModelMixin``'s versions call bare ``super()`` which would
       resolve incorrectly outside the mixin's own MRO. The wrappers
       inline the mixin's logic and call the captured original method.
    4. **Global query decorators** — see
       :func:`_install_global_query_decorators`.
    5. **ObjectDB.objects** — replaced with a manager that mixes
       ``TenantManagerMixin`` into Evennia's existing ``ObjectDBManager``.
       The new MRO is ``TenantManagerMixin`` → ``ObjectDBManager`` →
       ``TypedObjectManager`` → ``SharedMemoryManager``, so the idmapper's
       ``.get(pk=...)`` cache-lookup behaviour is preserved underneath
       the auto-filter.

    Idempotent via a marker attribute on ``ObjectDB``.
    """
    from django.db import models
    from django.db.utils import NotSupportedError
    from django_multitenant.mixins import TenantModelMixin
    from django_multitenant.utils import (
        get_current_tenant,
        get_current_tenant_value,
        get_tenant_field,
        get_tenant_filters,
    )
    from evennia.objects.models import ObjectDB

    if getattr(ObjectDB, "_evennia_shards_tenancy_installed", False):
        return

    # 0. Late-bind the ``shard_id`` field onto ObjectDB. The column
    #    exists in the database via the library's migrations; this
    #    teaches the Django ORM about it so multitenant can read /
    #    filter / stamp it. Migrated from ``isolation.install_chokepoints``
    #    where it lived prior to the multitenant adoption.
    if not any(f.name == "shard_id" for f in ObjectDB._meta.get_fields()):
        ObjectDB.add_to_class(
            "shard_id",
            models.CharField(max_length=64, null=True, blank=True, db_index=True),
        )

    # 1. Declare ObjectDB as a tenant-tagged model. Multitenant reads
    #    `cls.TenantMeta.tenant_field_name` to figure out which column
    #    to filter / stamp / update-scope on.
    class TenantMeta:
        tenant_field_name = "shard_id"

    ObjectDB.TenantMeta = TenantMeta

    # 2. Copy the three identity properties from TenantModelMixin.
    #    They read `self.TenantMeta` / `self.shard_id` directly — no
    #    super() involvement — so a straight copy works.
    ObjectDB.tenant_field = TenantModelMixin.tenant_field
    ObjectDB.tenant_value = TenantModelMixin.tenant_value
    ObjectDB.tenant_object = TenantModelMixin.tenant_object

    # 3a. Wrap save(). TenantModelMixin.save uses `super().save()` —
    #     if copied bare, super() resolves to TenantModelMixin's MRO
    #     parent (object), bypassing ObjectDB.save entirely. The
    #     wrapper inlines the mixin's logic and calls the captured
    #     original method.
    _original_save = ObjectDB.save

    def _tenant_aware_save(self, *args, **kwargs):
        if hasattr(self, "_try_update_tenant"):
            raise NotSupportedError(
                "Tenant column of a row cannot be updated."
            )
        tenant_value = get_current_tenant_value()
        # Auto-stamp on insert. Multitenant's stock ``set_object_tenant``
        # skips list-valued tenants, but our current-tenant shape is
        # always ``[Shard(current), Shard("*")]`` — a list. Stamp with
        # the first item (the active shard), not the global sentinel.
        if self.tenant_value is None and tenant_value:
            if isinstance(tenant_value, list):
                stamp = tenant_value[0]
            else:
                stamp = tenant_value
            setattr(self, self.tenant_field, stamp)
        # NOTE: upstream ``TenantModelMixin.save`` does a temporary
        # ``set_current_tenant(self.tenant_value)`` to scope UPDATEs at
        # the row's own tenant. That logic doesn't compose with our
        # two-element ``[Shard, Shard("*")]`` list shape (it would
        # collapse the IN-filter to a single shard and lose globals).
        # Skipping it: the ``_do_update`` wrapper below applies the
        # full ``shard_id IN (current, '*')`` filter to the UPDATE
        # WHERE clause, which correctly matches current-shard rows,
        # globals, and silently no-ops on foreign rows.
        return _original_save(self, *args, **kwargs)

    ObjectDB.save = _tenant_aware_save

    # 3b. Wrap _do_update — the per-row UPDATE path. Adds the tenant
    #     filter to the WHERE clause so `obj.save()` only writes rows
    #     owned by the current tenant (foreign rows silently no-op).
    _original_do_update = ObjectDB._do_update

    def _tenant_aware_do_update(self, base_qs, *args, **kwargs):
        # Signature-tolerant: Django 6.0 added ``returning_fields`` as
        # an extra positional, and there may be more drift in future
        # versions. We only need to mutate ``base_qs``; everything else
        # is forwarded untouched.
        if get_current_tenant():
            tenant_filter = get_tenant_filters(self.__class__)
            base_qs = base_qs.filter(**tenant_filter)
        return _original_do_update(self, base_qs, *args, **kwargs)

    ObjectDB._do_update = _tenant_aware_do_update

    # 3c. Wrap __setattr__ — flags attempts to change the tenant column
    #     after a row exists. The save() wrapper above checks for this
    #     flag and raises NotSupportedError. Together they enforce
    #     "tenant column is immutable after insert."
    #
    #     ``Model.__init__`` assigns ``self._state`` as its first attribute,
    #     so this wrapper runs before ``_state`` exists. Anything that
    #     relies on ``self._state`` or the tenant metadata bails out
    #     early during construction and just delegates to the raw setter.
    _original_setattr = ObjectDB.__setattr__

    def _tenant_aware_setattr(self, attrname, val):
        if not hasattr(self, "_state"):
            # Model still being initialised — tenant tracking is a
            # post-construction concern. Skip cleanly.
            return _original_setattr(self, attrname, val)
        try:
            tenant_field_attr = self.tenant_field
            tenant_field_name = get_tenant_field(self).name
        except (AttributeError, ValueError):
            return _original_setattr(self, attrname, val)

        # Only the case "setting the tenant column on an existing row"
        # needs the immutability check. Everything else (db_key, _state,
        # other field assignments, etc.) just delegates. Critical: we
        # must NOT read ``self.tenant_value`` here unless we know we're
        # in that case — accessing it triggers DeferredAttribute loading
        # if shard_id wasn't in the original query's field set, which
        # in turn fires refresh_from_db, re-enters from_db, re-enters
        # __init__, calls __setattr__ again, infinite recursion.
        is_tenant_attr = attrname in (tenant_field_attr, tenant_field_name)
        if is_tenant_attr and not self._state.adding:
            new_value_differs = (
                val
                and self.tenant_value
                and val != self.tenant_value
                and val != self.tenant_object
            )
            if new_value_differs:
                _original_setattr(self, "_try_update_tenant", True)
        return _original_setattr(self, attrname, val)

    ObjectDB.__setattr__ = _tenant_aware_setattr

    # 4. Install the global Django decorators that wrap delete/update
    #    queries with the tenant filter at the SQL level.
    _install_global_query_decorators()

    # 5. Patch ``get_queryset`` and ``bulk_create`` on the manager
    #    class — the read-side of the tenancy. We patch the existing
    #    manager class rather than swapping in a new manager instance:
    #
    #    - ``contribute_to_class`` doesn't actually replace an existing
    #      manager registration; Django's deduplication takes the first
    #      one in ``_meta.local_managers`` by name, so a swap is silently
    #      ignored.
    #    - Patching the class is the same pattern ``isolation.py`` uses
    #      for the ``QuerySet.update`` chokepoint — light, idempotent
    #      via marker attribute, and it carries through to any consumer
    #      subclass automatically.
    manager_cls = type(ObjectDB.objects)
    if not getattr(manager_cls, "_evennia_shards_tenant_patched", False):
        _original_get_queryset = manager_cls.get_queryset

        def _tenant_aware_get_queryset(self):
            # Multitenant's stock ``get_queryset`` builds a queryset
            # from scratch (``self._queryset_class(self.model)``),
            # losing the ``using`` and ``hints`` arguments the original
            # passes. Wrap-and-filter instead: keep the original's
            # queryset and apply the tenant filter on top.
            queryset = _original_get_queryset(self)
            if get_current_tenant():
                kwargs = get_tenant_filters(self.model)
                return queryset.filter(**kwargs)
            return queryset

        _original_bulk_create = manager_cls.bulk_create

        def _tenant_aware_bulk_create(self, objs, **kwargs):
            # Auto-stamp each unsaved instance with the current shard
            # before bulk inserting. Django bypasses pre_save for
            # bulk_create, so we have to stamp explicitly. Same
            # list-aware stamping logic as the save() wrapper above.
            tenant_value = get_current_tenant_value()
            if tenant_value:
                if isinstance(tenant_value, list):
                    stamp = tenant_value[0]
                else:
                    stamp = tenant_value
                for obj in objs:
                    if obj.tenant_value is None:
                        setattr(obj, obj.tenant_field, stamp)
            return _original_bulk_create(self, objs, **kwargs)

        manager_cls.get_queryset = _tenant_aware_get_queryset
        manager_cls.bulk_create = _tenant_aware_bulk_create
        manager_cls._evennia_shards_tenant_patched = True

    ObjectDB._evennia_shards_tenancy_installed = True


def bootstrap_tenant_context() -> None:
    """Initialise the multitenant tenant context for this process.

    Reads the deployment role from ``config.get_role()`` and sets the
    tenant context to match:

    - **shard** processes scope to ``[Shard(SHARD_ID), Shard("*")]`` —
      they see their own rows plus global ``"*"`` rows.
    - **router** processes are explicitly unscoped — full visibility,
      since the router coordinates across shards.
    - **monolith** processes do nothing — the library is not in
      ``INSTALLED_APPS`` in this mode, so this branch should be
      unreachable, but is handled defensively for direct callers.

    Called once from ``EvenniaShardsConfig.ready()`` at process startup.
    Idempotent: re-running it from the same role produces the same
    context.
    """
    from .config import ROLE_MONOLITH, ROLE_ROUTER, ROLE_SHARD, get_role, get_shard_id

    role = get_role()
    if role == ROLE_SHARD:
        set_current_shard(get_shard_id())
    elif role == ROLE_ROUTER:
        clear_shard_context()
    elif role == ROLE_MONOLITH:
        # Defensive: the library should not be loaded in monolith mode
        # at all, but if a consumer ever does, leave the context
        # untouched (unscoped — equivalent to no multitenant at all).
        return


@contextmanager
def shard_context(shard_id: str | None):
    """Temporarily switch shard context inside the ``with`` block.

    Inside the block, queries scope as if the process were ``shard_id``.
    On block exit (normal, exception, return, break) the previous context
    is restored. Passing ``shard_id=None`` runs the block unscoped.

    The library uses this for:

    - **Handoff** (`handoff.py`): write the new owner's row under the
      target shard's identity, then return to the source shard's scope.
    - **Chargen on router** (`chargen.py`): stamp a new character with
      the start-location's shard_id by entering that shard's context
      for the save.
    - **Admin / recovery tooling**: act-as a specific shard for a brief
      operation without changing the process-wide context.

    Nesting is safe: each ``with`` records the active context at entry
    and restores it on exit, so inner blocks composing with outer ones
    behave correctly.

    Example::

        with shard_context("shard1"):
            remote = ObjectDB.objects.get(pk=remote_pk)
            remote.db_location_id = local_room_pk
            remote.save()
        # back to whatever context was active before the block
    """
    previous = get_current_tenant()
    try:
        if shard_id is None:
            unset_current_tenant()
        else:
            set_current_tenant(_scope_for(shard_id))
        yield
    finally:
        if previous is None:
            unset_current_tenant()
        else:
            set_current_tenant(previous)
