# SPDX-License-Identifier: BSD-3-Clause
"""Unit tests for the evennia-shards library."""

import unittest

from django.test import override_settings
from django_multitenant.utils import (
    get_current_tenant,
    get_current_tenant_value,
    unset_current_tenant,
)
from evennia.objects.models import ObjectDB
from evennia.utils.test_resources import BaseEvenniaTestCase

from evennia_shards.tenancy import (
    GLOBAL_SHARD_ID,
    Shard,
    bootstrap_tenant_context,
    clear_shard_context,
    install_tenancy_on_objectdb,
    set_current_shard,
    shard_context,
)

TYPECLASS = "evennia.objects.objects.DefaultObject"


def _forge_db_shard(pk: int, shard_id: str | None) -> None:
    """Force a row's ``shard_id`` via raw SQL — bypasses the multitenant
    auto-filter and the tenant-column immutability check.

    Used by tests that need to set up "this row lives on another shard"
    scenarios without going through any of the library's safeguards.
    Raw cursor SQL routes around Django's ORM entirely, so the patched
    manager, ``__setattr__``, and global query decorators all stay
    out of the way.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE objects_objectdb SET shard_id=%s WHERE id=%s",
            [shard_id, pk],
        )


class ShardObjectTests(unittest.TestCase):
    """The Shard stand-in exists only to satisfy multitenant's
    ``tenant.tenant_value`` protocol. Verify the surface."""

    def test_tenant_value_attribute(self):
        self.assertEqual(Shard("shard0").tenant_value, "shard0")
        self.assertEqual(Shard(GLOBAL_SHARD_ID).tenant_value, "*")

    def test_repr_includes_shard_id(self):
        self.assertEqual(repr(Shard("shard0")), "Shard('shard0')")

    def test_equality_by_shard_id(self):
        self.assertEqual(Shard("shard0"), Shard("shard0"))
        self.assertNotEqual(Shard("shard0"), Shard("shard1"))

    def test_equality_with_non_shard_is_notimplemented(self):
        # Returning NotImplemented lets Python fall back to the other
        # side's __eq__; sanity-check the != case too.
        self.assertNotEqual(Shard("shard0"), "shard0")
        self.assertNotEqual(Shard("shard0"), 0)

    def test_hash_matches_equality(self):
        # Set membership depends on hash + eq agreeing.
        self.assertEqual(
            len({Shard("a"), Shard("a"), Shard("b")}),
            2,
        )

    def test_global_shard_id_constant(self):
        # The "*" sentinel is hard-wired across the codebase; the
        # constant is the canonical source.
        self.assertEqual(GLOBAL_SHARD_ID, "*")


class TenancyContextTests(unittest.TestCase):
    """The set/clear/shard_context primitives are the public surface for
    switching the multitenant scope. Each test isolates context state via
    setUp/tearDown so a leak in one test doesn't pollute the next."""

    def setUp(self):
        # Start every test from a clean unscoped state — even if a
        # previous test (or an outer harness) left a tenant set.
        unset_current_tenant()

    def tearDown(self):
        unset_current_tenant()

    def test_baseline_is_unscoped(self):
        self.assertIsNone(get_current_tenant())

    def test_set_current_shard_stores_two_element_list(self):
        # Multitenant's IN-filter mode triggers on a list; the list
        # shape — [shard, global] — is what makes the auto-filter
        # produce WHERE shard_id IN (current, '*').
        set_current_shard("shard0")
        tenant = get_current_tenant()
        self.assertEqual(tenant, [Shard("shard0"), Shard("*")])

    def test_set_current_shard_value_extraction(self):
        # get_current_tenant_value reads `.tenant_value` off each item
        # in the list. This is what gets fed to the SQL filter.
        set_current_shard("shard0")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_clear_shard_context_returns_to_unscoped(self):
        set_current_shard("shard0")
        clear_shard_context()
        self.assertIsNone(get_current_tenant())

    def test_shard_context_switches_inside_block(self):
        set_current_shard("shard0")
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_none_runs_unscoped(self):
        # Passing None into the context manager is the "act as router"
        # escape — visible to every row inside the block, restored
        # after.
        set_current_shard("shard0")
        with shard_context(None):
            self.assertIsNone(get_current_tenant())
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_restores_after_exception(self):
        # `finally` must run on exception so a raised block doesn't
        # leak the inner scope into subsequent code.
        set_current_shard("shard0")
        with self.assertRaises(RuntimeError):
            with shard_context("shard1"):
                self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
                raise RuntimeError("boom")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_restores_from_unscoped_baseline(self):
        # Starting unscoped and entering a context should restore back
        # to unscoped, not leave the entry scope active.
        self.assertIsNone(get_current_tenant())
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertIsNone(get_current_tenant())

    def test_shard_context_nests_three_deep(self):
        # Each nested level records its entry context and restores it
        # on exit; verifies the previous-context capture is per-call,
        # not shared.
        set_current_shard("shard0")
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
            with shard_context("shard2"):
                self.assertEqual(get_current_tenant_value(), ["shard2", "*"])
                with shard_context("shard3"):
                    self.assertEqual(get_current_tenant_value(), ["shard3", "*"])
                self.assertEqual(get_current_tenant_value(), ["shard2", "*"])
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    def test_shard_context_around_unscoped_outer(self):
        # If the outer context is unscoped (router process), entering
        # and exiting a shard_context should leave us unscoped again.
        with shard_context("shard1"):
            self.assertEqual(get_current_tenant_value(), ["shard1", "*"])
        self.assertIsNone(get_current_tenant())


class BootstrapTenantContextTests(unittest.TestCase):
    """Tests the role -> tenant-context decision invoked from apps.ready().

    Uses ``override_settings`` to simulate each deployment role. The
    helper is the single point where the role string is translated into
    a multitenant call, so these tests pin down that contract."""

    def setUp(self):
        unset_current_tenant()

    def tearDown(self):
        unset_current_tenant()

    @override_settings(SHARDS_ROLE="shard", SHARD_ID="shard0")
    def test_shard_role_scopes_to_own_shard_plus_global(self):
        bootstrap_tenant_context()
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    @override_settings(SHARDS_ROLE="router")
    def test_router_role_runs_unscoped(self):
        # Start from a scoped state so we can verify the helper
        # actively clears, not just leaves things alone.
        set_current_shard("shard0")
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])
        bootstrap_tenant_context()
        self.assertIsNone(get_current_tenant())

    @override_settings(SHARDS_ROLE="monolith")
    def test_monolith_role_leaves_context_untouched(self):
        # Defensive branch: in monolith the library shouldn't be loaded
        # at all, but if a consumer calls the helper directly, it
        # should not modify any existing tenant state.
        set_current_shard("shard0")
        bootstrap_tenant_context()
        self.assertEqual(get_current_tenant_value(), ["shard0", "*"])

    @override_settings(SHARDS_ROLE="shard", SHARD_ID="shard1")
    def test_shard_role_is_idempotent(self):
        # Re-running bootstrap from the same role should produce the
        # same context. apps.ready() runs once per process, but this
        # guards against double-load scenarios (dev reload, tests).
        bootstrap_tenant_context()
        first = get_current_tenant_value()
        bootstrap_tenant_context()
        second = get_current_tenant_value()
        self.assertEqual(first, second)
        self.assertEqual(first, ["shard1", "*"])


class InstallTenancyShapeTests(unittest.TestCase):
    """Verifies the install function attached the right machinery to
    ObjectDB. These are pure inspection — they don't touch the DB."""

    def test_install_marker_is_set(self):
        # apps.ready() runs the install during test startup; the marker
        # must be present so any subsequent install call is a no-op.
        self.assertTrue(
            getattr(ObjectDB, "_evennia_shards_tenancy_installed", False)
        )

    def test_install_is_idempotent(self):
        # Re-running install must not error or alter the class further.
        # Marker stays set, manager class stays the same.
        before = ObjectDB.objects.__class__
        install_tenancy_on_objectdb()
        self.assertIs(ObjectDB.objects.__class__, before)
        self.assertTrue(ObjectDB._evennia_shards_tenancy_installed)

    def test_tenant_meta_declares_shard_id(self):
        # Multitenant resolves the tenant column by reading TenantMeta.
        # The contract: tenant_field_name maps to the shard_id column.
        self.assertTrue(hasattr(ObjectDB, "TenantMeta"))
        self.assertEqual(ObjectDB.TenantMeta.tenant_field_name, "shard_id")

    def test_tenant_field_property_returns_shard_id(self):
        # The instance-level lookup multitenant uses to figure out the
        # column at filter / stamp time.
        # We can read the property via the class with a dummy access
        # path, but checking on a transient instance is more honest.
        obj = ObjectDB(db_key="probe", db_typeclass_path=TYPECLASS)
        self.assertEqual(obj.tenant_field, "shard_id")

    def test_manager_class_is_patched(self):
        # The install patches ``get_queryset`` and ``bulk_create`` on
        # the existing manager class (rather than swapping in a new
        # subclass — Django's manager dedup makes that approach silently
        # ineffective). Verify the patch marker is in place and the
        # idmapper-aware base class is still in the chain.
        from evennia.utils.idmapper.manager import SharedMemoryManager

        manager_cls = ObjectDB.objects.__class__
        self.assertTrue(
            getattr(manager_cls, "_evennia_shards_tenant_patched", False)
        )
        self.assertIn(SharedMemoryManager, manager_cls.__mro__)


class AutoStampAndFilterTests(BaseEvenniaTestCase):
    """Integration tests: ObjectDB creation under shard context stamps
    the row, queries auto-scope to the current shard, raw-SQL-forged
    foreign rows are invisible. Requires DB — uses BaseEvenniaTestCase.

    These tests run with the process-wide tenant set to ``shard0`` (by
    apps.ready() reading the test settings). They verify the auto-filter
    is doing real work, not just configured."""

    def test_create_auto_stamps_current_shard(self):
        # Auto-stamp on insert: the new row gets shard_id="shard0"
        # (the first item of the current [shard0, *] tenant list) —
        # not "*", and not None.
        obj = ObjectDB.objects.create(
            db_key="auto_stamp_probe",
            db_typeclass_path=TYPECLASS,
        )
        self.assertEqual(obj.shard_id, "shard0")

    def test_local_row_visible_via_default_manager(self):
        # Sanity: a freshly-created shard0 row is visible to a shard0
        # query. If the filter were inverted this would silently fail.
        obj = ObjectDB.objects.create(
            db_key="local_probe",
            db_typeclass_path=TYPECLASS,
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            1,
        )

    def test_global_row_visible_from_shard(self):
        # Rows stamped with the "*" sentinel must be visible from any
        # shard scope — the IN-filter includes "*" alongside the
        # current shard.
        obj = ObjectDB.objects.create(
            db_key="global_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "*")
        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            1,
        )

    def test_foreign_row_invisible_via_default_manager(self):
        # Forge a row's shard_id to a different shard via raw SQL,
        # then query through the default manager. The auto-filter
        # must exclude it — count is zero, .get raises DoesNotExist.
        obj = ObjectDB.objects.create(
            db_key="foreign_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "shard1")

        # The idmapper cache may still hold the instance from before
        # the forge, so flush before re-querying.
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()

        self.assertEqual(
            ObjectDB.objects.filter(pk=obj.pk).count(),
            0,
        )
        with self.assertRaises(ObjectDB.DoesNotExist):
            ObjectDB.objects.get(pk=obj.pk)

    def test_bulk_create_auto_stamps_each_instance(self):
        # bulk_create bypasses Django's normal save() path, so the
        # auto-stamp on save() doesn't run. The patched manager method
        # must stamp each unsaved instance before delegating.
        objs = [
            ObjectDB(db_key=f"bulk_{i}", db_typeclass_path=TYPECLASS)
            for i in range(3)
        ]
        for obj in objs:
            self.assertIsNone(obj.shard_id)  # pre-stamp

        ObjectDB.objects.bulk_create(objs)

        for obj in objs:
            self.assertEqual(obj.shard_id, "shard0")

        # And the rows actually landed in the DB with that stamp —
        # round-trip via the auto-filtered manager to confirm.
        keys = [f"bulk_{i}" for i in range(3)]
        rows = list(
            ObjectDB.objects.filter(db_key__in=keys)
            .values_list("db_key", "shard_id")
        )
        self.assertEqual(sorted(rows), [(k, "shard0") for k in keys])

    def test_bulk_delete_scopes_to_current_shard(self):
        # ``QuerySet.delete()`` and the underlying Collector route
        # through the wrap_delete + related_objects decorators that
        # multitenant installs. A bulk delete that names both local and
        # foreign rows must only touch the local row.
        local = ObjectDB.objects.create(
            db_key="bulk_del_local", db_typeclass_path=TYPECLASS,
        )
        foreign = ObjectDB.objects.create(
            db_key="bulk_del_foreign", db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(foreign.pk, "shard1")

        from evennia.utils.idmapper.models import flush_cache

        flush_cache()

        # Default-scoped queryset: filter sees local only (foreign is
        # already excluded by the auto-filter). Naming both pks
        # explicitly in pk__in proves the SQL WHERE clause still
        # carries the tenant filter — the foreign row survives.
        ObjectDB.objects.filter(pk__in=[local.pk, foreign.pk]).delete()

        # Local is gone; foreign survives. Read past the auto-filter
        # to confirm both halves of the assertion.
        with shard_context(None):
            survivors = set(
                ObjectDB.objects.filter(pk__in=[local.pk, foreign.pk])
                .values_list("pk", flat=True)
            )
        self.assertEqual(survivors, {foreign.pk})


class ShardContextReadTests(BaseEvenniaTestCase):
    """Verifies that ``shard_context(...)`` correctly switches the
    auto-filter scope for reads. Test bodies use ``.count()`` /
    ``.exists()`` / ``.values()`` rather than ``.get()`` or queryset
    iteration — instance materialisation is exercised separately in
    :class:`CrossShardWriteTests`.

    The auto-filter is the single thing all read primitives funnel
    through, so verifying it via ``.count()`` establishes that the
    filter is being applied with the right scope. Full materialisation
    paths inherit the same filter."""

    def _make_row(self, key: str, shard_id: str) -> int:
        # Helper: create a row under the default shard0 context (so
        # auto-stamp + chokepoints behave) and forge its shard_id
        # via raw SQL afterwards. Returns the pk.
        obj = ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS
        )
        if shard_id != "shard0":
            _forge_db_shard(obj.pk, shard_id)
        # Flush the idmapper so subsequent queries don't pick up the
        # cached instance with its original shard_id.
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()
        return obj.pk

    def test_foreign_row_visible_inside_its_own_shard_context(self):
        # A row stamped shard1 is invisible to shard0 by default, but
        # becomes visible when scope switches to shard1 via the
        # context manager.
        pk = self._make_row("inside_own_context", "shard1")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_local_row_invisible_inside_foreign_shard_context(self):
        # The inverse: a shard0 row is normally visible, but inside
        # shard_context("shard1") falls out of scope.
        pk = self._make_row("local_in_foreign_context", "shard0")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)

    def test_global_row_visible_in_any_shard_context(self):
        # The "*" sentinel is the OR-arm of every shard's IN-filter,
        # so a "*"-stamped row must be visible from every scope.
        pk = self._make_row("global_in_any_context", "*")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard2"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_unscoped_context_sees_all_shards(self):
        # shard_context(None) is the "act as router" escape. Without a
        # tenant set, no auto-filter is injected — every row is in
        # scope regardless of shard_id.
        shard0_pk = self._make_row("unscoped_shard0_probe", "shard0")
        shard1_pk = self._make_row("unscoped_shard1_probe", "shard1")
        global_pk = self._make_row("unscoped_global_probe", "*")

        with shard_context(None):
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=global_pk).count(), 1
            )

    def test_scope_restored_after_context_exit(self):
        # Entering and exiting shard_context must leave the
        # process-wide scope unchanged — otherwise a context switch
        # leaks visibility into subsequent code.
        pk = self._make_row("scope_restoration_probe", "shard0")
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)
        with shard_context("shard1"):
            self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 0)
        # Same query, same row, scope restored.
        self.assertEqual(ObjectDB.objects.filter(pk=pk).count(), 1)

    def test_nested_contexts_filter_each_at_their_own_scope(self):
        # Each level of nesting sees only its own shard + globals;
        # inner exits restore to the immediate outer's scope, not all
        # the way to the process baseline.
        shard0_pk = self._make_row("nested_shard0_probe", "shard0")
        shard1_pk = self._make_row("nested_shard1_probe", "shard1")
        global_pk = self._make_row("nested_global_probe", "*")

        # Outer baseline: shard0.
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard0_pk).count(), 1
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard1_pk).count(), 0
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=global_pk).count(), 1
        )

        with shard_context("shard1"):
            # shard1 + global visible; shard0 hidden.
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 0
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=global_pk).count(), 1
            )

            with shard_context("shard2"):
                # Only globals visible — neither shard0 nor shard1.
                self.assertEqual(
                    ObjectDB.objects.filter(pk=shard0_pk).count(), 0
                )
                self.assertEqual(
                    ObjectDB.objects.filter(pk=shard1_pk).count(), 0
                )
                self.assertEqual(
                    ObjectDB.objects.filter(pk=global_pk).count(), 1
                )

            # Restored to shard1 scope, not shard0.
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard0_pk).count(), 0
            )
            self.assertEqual(
                ObjectDB.objects.filter(pk=shard1_pk).count(), 1
            )

        # Restored to shard0 baseline.
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard0_pk).count(), 1
        )
        self.assertEqual(
            ObjectDB.objects.filter(pk=shard1_pk).count(), 0
        )

    def test_exists_respects_shard_context(self):
        # ``.exists()`` takes a different SQL path than ``.count()``
        # (SELECT 1 LIMIT 1 vs SELECT COUNT(*)) but goes through the
        # same queryset filter. Sanity-check both paths.
        pk = self._make_row("exists_probe", "shard1")
        self.assertFalse(ObjectDB.objects.filter(pk=pk).exists())
        with shard_context("shard1"):
            self.assertTrue(ObjectDB.objects.filter(pk=pk).exists())

    def test_values_respects_shard_context(self):
        # ``.values()`` returns dicts directly without going through
        # from_db, but does flow through the manager's get_queryset
        # and therefore the auto-filter. Same scoping behaviour as
        # full querysets.
        pk = self._make_row("values_probe", "shard1")
        self.assertEqual(
            list(ObjectDB.objects.filter(pk=pk).values("db_key")), []
        )
        with shard_context("shard1"):
            self.assertEqual(
                list(ObjectDB.objects.filter(pk=pk).values("db_key")),
                [{"db_key": "values_probe"}],
            )


class CrossShardWriteTests(BaseEvenniaTestCase):
    """Verifies cross-shard reads (full materialisation) and writes
    (creates that auto-stamp under foreign shard_context). These paths
    were previously blocked by ``isolation.py``'s ``from_db`` and
    ``pre_save`` chokepoints; they pass cleanly under the multitenant
    model where the auto-filter is the single boundary."""

    def test_materialise_foreign_row_inside_its_shard_context(self):
        # Forge a row to shard1 via raw SQL, then materialise it
        # inside shard_context("shard1"). The auto-filter includes
        # the row (scope ["shard1", "*"]), from_db fires, instance
        # constructed — full round-trip via .get().
        obj = ObjectDB.objects.create(
            db_key="materialise_probe",
            db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, "shard1")
        from evennia.utils.idmapper.models import flush_cache

        flush_cache()

        with shard_context("shard1"):
            row = ObjectDB.objects.get(pk=obj.pk)
            self.assertEqual(row.shard_id, "shard1")
            self.assertEqual(row.db_key, "materialise_probe")

    def test_create_inside_foreign_shard_context_stamps_new_row(self):
        # Inside shard_context("shard1"), creating a new ObjectDB
        # auto-stamps shard_id="shard1" (the first item of the tenant
        # list, not the global sentinel). The row is round-tripped via
        # DB to confirm it actually persisted with the right stamp,
        # not just in-memory.
        with shard_context("shard1"):
            obj = ObjectDB.objects.create(
                db_key="cross_shard_create_probe",
                db_typeclass_path=TYPECLASS,
            )
            self.assertEqual(obj.shard_id, "shard1")
            from evennia.utils.idmapper.models import flush_cache

            flush_cache()
            fresh = ObjectDB.objects.get(pk=obj.pk)
            self.assertEqual(fresh.shard_id, "shard1")


# === Test fixtures ===================================================
#
# Minimal stand-ins for Evennia session / account infrastructure. They
# let the cross_shard_move tests assert on session.msg() side-effects
# and account.db._last_puppet writes without spinning up the real
# server. Ported from the legacy test suite — neither structure
# changed under multitenant.


class _FakeSession:
    """Minimal session stand-in for cross_shard_move and hook tests."""

    def __init__(self, address="127.0.0.1"):
        self.address = address
        self.puppet = None
        self.puid = None
        self.oob_messages = {}
        self.protocol_flags = {}
        self.flag_updates = {}

    def msg(self, **kwargs):
        self.oob_messages.update(kwargs)

    def update_flags(self, **flags):
        self.flag_updates.update(flags)


class _FakeAttributes:
    """Stand-in for AccountDB.attributes."""

    def __init__(self, store=None):
        self._store = dict(store or {})

    def get(self, name, default=None):
        return self._store.get(name, default)

    def reset_cache(self):
        # No-op: the fake doesn't keep a separate cache to invalidate.
        pass


class _FakeAccount:
    """Account stand-in for cross_shard_move and hook tests.

    Exposes ``db._last_puppet`` for the redirect helper and recorders
    (``account_messages``, ``connect_channel_messages``, ``at_look_calls``)
    for asserting on hook side-effects.
    """

    def __init__(self, pk=1, characters=None, key="Player1"):
        self._saved_attrs = {}
        self.id = pk
        self.pk = pk
        self.key = key
        self._characters = characters or []
        self.db = self  # db.X delegates to self.X
        self.attributes = _FakeAttributes()
        # Default False so hooks reading ``account.db._shards_at_ooc_menu``
        # don't AttributeError on the fake (vanilla AttributeHandler
        # returns None silently for unset attrs, but here ``db = self``
        # so the attribute has to actually exist).
        self._shards_at_ooc_menu = False
        # Recorders for hook side-effect assertions.
        self.account_messages = []
        self.connect_channel_messages = []
        self.at_look_calls = []

    @property
    def _last_puppet(self):
        return self._saved_attrs.get("_last_puppet")

    @_last_puppet.setter
    def _last_puppet(self, value):
        self._saved_attrs["_last_puppet"] = value

    @property
    def characters(self):
        return self._characters

    def msg(self, text=None, session=None, **kwargs):
        if text is not None:
            self.account_messages.append(text)

    def _send_to_connect_channel(self, message):
        self.connect_channel_messages.append(message)

    def at_look(self, target=None, session=None, **kwargs):
        self.at_look_calls.append({"target": target, "session": session})
        return "OOC menu"

    def get_puppet(self, session):
        return session.puppet

    def search(self, searchdata, candidates=None, search_object=True, quiet=True):
        """Simple name-match search against candidates."""
        if candidates:
            return [c for c in candidates if c.key == searchdata]
        return []

    @property
    def locks(self):
        return self

    def check_lockstring(self, account, lockstring):
        return False  # non-Builder for simplicity

    def unpuppet_object(self, session):
        for s in (session if isinstance(session, (list, tuple)) else [session]):
            s.puppet = None


class _FakeCaller:
    """Caller stand-in for admin- and teleport-command tests.

    Captures ``msg(...)`` calls and resolves ``search(...)`` to a
    pre-set target object (or ``None``). Carries the location /
    permissions / lock surface ``ShardAwareCmdTeleport`` exercises;
    admin commands ignore those fields.
    """

    def __init__(self, search_returns=None, is_admin=True, access_allowed=True):
        import types

        self.messages = []
        self.search_returns = search_returns
        # Set on the instance so it never accidentally collides with the
        # destination pk during ShardAwareCmdTeleport short-circuit checks.
        self.db_location_id = None
        # ``.location`` is read by the cross-shard announce wiring.
        # None is the in-limbo / no-source-room case.
        self.location = None
        # ``.key`` and ``.pk`` feed the cross-shard success message and
        # announce text.
        self.key = "fakecaller"
        self.pk = 1
        # Lock-check surfaces. ``permissions.check`` is consulted on
        # the caller; ``access`` is consulted on the obj being teleported.
        # Defaults match the admin-bypass path so tests that don't care
        # about locks stay green. Override via kwargs when testing the
        # lock branch itself.
        self.permissions = types.SimpleNamespace(
            check=lambda name: is_admin,
        )
        self._access_allowed = access_allowed

    def access(self, accessing_obj, access_type, default=False):
        return self._access_allowed

    def search(self, searchdata):
        return self.search_returns

    def msg(self, text=None, **kwargs):
        if text is not None:
            self.messages.append(text)


class _FakeSessionHandler:
    """Stand-in for the per-character SessionHandler. Provides ``.all()``
    so cross_shard_move can iterate puppeting sessions."""

    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def all(self):
        return list(self._sessions)


class _FakeCharacter:
    """Minimal character stand-in for router-side hook tests.

    The router-side override reads ``shard_id`` directly and calls
    ``flush_from_cache`` / ``refresh_from_db`` as cache-bust steps.
    Both are no-ops in tests — the row's ``shard_id`` is set
    statically at construction.
    """

    def __init__(self, key, pk, shard_id="shard0"):
        self.key = key
        self.id = pk
        self.pk = pk
        self.shard_id = shard_id
        self.name = key

    def flush_from_cache(self, force=False):
        pass

    def refresh_from_db(self, fields=None):
        pass


@override_settings(
    SHARD_ID="shard0",
    SHARDS_ROLE="shard",
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CrossShardMoveTests(BaseEvenniaTestCase):
    """``cross_shard_move`` end-to-end under multitenant.

    The primitive's external contract is unchanged from the chokepoint
    era — same args, same return type, same side effects — so most
    tests port directly from the legacy suite. The internal mechanism
    swapped ``obj.shard_id = X; obj.save()`` inside a
    ``shard_writes_allowed_for`` bypass for plain
    ``qs.update(shard_id=X, db_location_id=Y)``. The qs.update bypasses
    the tenant-column immutability check on ``__setattr__``, which is
    what makes the cross-shard write possible at all under the new
    model.

    Validation failures raise ``ValueError`` (was
    ``ShardIsolationError``).
    """

    def _make_target_room(self, target_shard="shard1"):
        room = ObjectDB.objects.create(
            db_key="target_room", db_typeclass_path=TYPECLASS
        )
        _forge_db_shard(room.pk, target_shard)
        return room

    def _make_char(self, n_sessions=0):
        """Create a real ObjectDB row + stub a fake session handler onto it.

        Each fake session shares one fake account on its ``.account``
        attribute — the primitive reads ``session.account`` for redirects.
        ``char.__dict__["sessions"] = ...`` shadows the lazy_property
        descriptor on this one instance, bypassing the typeclass's
        normal attribute machinery.
        """
        char = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS
        )
        fake_account = _FakeAccount(pk=42)
        fake_sessions = []
        for i in range(n_sessions):
            sess = _FakeSession(address=f"10.0.0.{i + 1}")
            sess.account = fake_account
            fake_sessions.append(sess)
        char.__dict__["sessions"] = _FakeSessionHandler(fake_sessions)
        return char, fake_account, fake_sessions

    def _make_item(self, name, location):
        return ObjectDB.objects.create(
            db_key=name, db_typeclass_path=TYPECLASS, db_location=location,
        )

    def _read_row(self, pk, *fields):
        """Read columns from a row regardless of which shard owns it.

        Used by post-move assertions — once a row's been moved to
        another shard, the default auto-filter excludes it from
        queries on this process. ``shard_context(None)`` lifts the
        filter for the read.
        """
        with shard_context(None):
            row = (
                ObjectDB.objects.filter(pk=pk)
                .values_list(*fields)
                .first()
            )
        return row

    # ── happy path ──────────────────────────────────────────────────

    def test_move_no_sessions_succeeds(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        # Row updated at the DB level. Read unscoped — the auto-filter
        # would exclude shard1 from a shard0 process.
        persisted = self._read_row(char.pk, "shard_id", "db_location_id")
        self.assertEqual(persisted, ("shard1", target.pk))

        self.assertEqual(result.objects_moved, 1)
        self.assertEqual(result.sessions_redirected, 0)
        self.assertEqual(result.failures, [])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_move_with_one_session_redirects(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertIn("shard_redirect", sessions[0].oob_messages)
        ticket = Ticket.objects.first()
        self.assertEqual(ticket.to_shard, "shard1")
        self.assertEqual(ticket.character_id, char.pk)

    def test_move_with_multiple_sessions_redirects_each(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=3)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.sessions_redirected, 3)
        self.assertEqual(Ticket.objects.count(), 3)
        for sess in sessions:
            self.assertIn("shard_redirect", sess.oob_messages)

    # ── validation failures ─────────────────────────────────────────

    def test_target_shard_not_configured_raises(self):
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "nonexistent_shard", target.pk)
        self.assertIn("nonexistent_shard", str(ctx.exception))

        # No move happened — char still on shard0, visible via default scope.
        persisted = (
            ObjectDB.objects.filter(pk=char.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(persisted, "shard0")
        self.assertEqual(Ticket.objects.count(), 0)

    def test_target_location_does_not_exist_raises(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "shard1", 999999)
        self.assertIn("999999", str(ctx.exception))

    def test_target_location_on_wrong_shard_raises(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        # Local room (auto-stamped shard0) used as target while
        # target_shard="shard1" → mismatch.
        local_room = ObjectDB.objects.create(
            db_key="local_room", db_typeclass_path=TYPECLASS
        )

        with self.assertRaises(ValueError) as ctx:
            cross_shard_move(char, "shard1", local_room.pk)
        msg = str(ctx.exception)
        self.assertIn("shard0", msg)
        self.assertIn("shard1", msg)

    # ── atomicity / failure handling ────────────────────────────────

    def test_atomic_rollback_on_eviction_failure(self):
        """If anything inside the atomic block raises, the DB rolls back.

        Reworked from the legacy ``test_atomic_rollback_on_save_failure``
        — the legacy version monkey-patched ``obj.save`` to raise, but
        the multitenant rewrite uses ``qs.update`` (not ``save``), so
        we inject failure at a different point: monkey-patch
        ``obj.flush_from_cache`` to raise. It runs inside the same
        atomic block, after the row update; the rollback should
        undo the update.
        """
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Ticket

        char, _, sessions = self._make_char(n_sessions=1)
        target = self._make_target_room()

        def failing_flush(*args, **kwargs):
            raise RuntimeError("simulated eviction failure")
        object.__setattr__(char, "flush_from_cache", failing_flush)

        with self.assertRaises(RuntimeError):
            cross_shard_move(char, "shard1", target.pk)

        # Atomic rollback: row should still be on shard0 (visible
        # to default scope).
        persisted = self._read_row(char.pk, "shard_id", "db_location_id")
        self.assertEqual(persisted[0], "shard0")
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", sessions[0].oob_messages)

    def test_session_redirect_failure_captured_in_result(self):
        """One session's redirect raises — the move commits, other
        sessions redirect, failure recorded in ``result.failures``."""
        from evennia_shards import cross_shard_move

        char, _, sessions = self._make_char(n_sessions=2)
        target = self._make_target_room()

        def raising_msg(**kwargs):
            raise RuntimeError("simulated network failure")
        sessions[1].msg = raising_msg

        result = cross_shard_move(char, "shard1", target.pk)

        persisted = self._read_row(char.pk, "shard_id")
        self.assertEqual(persisted, ("shard1",))

        self.assertEqual(result.sessions_redirected, 1)
        self.assertEqual(len(result.failures), 1)
        failed_session, failed_exc = result.failures[0]
        self.assertIs(failed_session, sessions[1])
        self.assertIsInstance(failed_exc, RuntimeError)

    # ── inventory recursion ─────────────────────────────────────────

    def test_move_contents_shard_ids_updated(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item1 = self._make_item("sword", char)
        item2 = self._make_item("shield", char)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        with shard_context(None):
            shards = dict(
                ObjectDB.objects.filter(pk__in=[char.pk, item1.pk, item2.pk])
                .values_list("pk", "shard_id")
            )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[item1.pk], "shard1")
        self.assertEqual(shards[item2.pk], "shard1")

    def test_move_nested_contents(self):
        """Char → bag → gem: the whole tree moves."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        bag = self._make_item("bag", char)
        gem = self._make_item("gem", bag)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 3)
        with shard_context(None):
            shards = dict(
                ObjectDB.objects.filter(pk__in=[char.pk, bag.pk, gem.pk])
                .values_list("pk", "shard_id")
            )
        self.assertEqual(shards[char.pk], "shard1")
        self.assertEqual(shards[bag.pk], "shard1")
        self.assertEqual(shards[gem.pk], "shard1")

    def test_move_no_contents(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 1)

    def test_move_contents_idmapper_eviction(self):
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cache = ObjectDB.__dbclass__.__instance_cache__
        self.assertIn(item.pk, cache)

        cross_shard_move(char, "shard1", target.pk)

        self.assertNotIn(char.pk, cache)
        self.assertNotIn(item.pk, cache)

    def test_move_contents_location_unchanged(self):
        """Items' ``db_location_id`` still points at char's pk after move
        (FK target's identity doesn't change across shards)."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        item = self._make_item("sword", char)
        target = self._make_target_room()

        cross_shard_move(char, "shard1", target.pk)

        loc = self._read_row(item.pk, "db_location_id")
        self.assertEqual(loc, (char.pk,))

    def test_move_contents_globals_left_alone(self):
        """Items stamped ``shard_id="*"`` (globals) are not re-stamped
        — the explicit ``shard_id=current`` filter in the contents
        update excludes them."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        normal_item = self._make_item("sword", char)
        global_item = self._make_item("global_buff", char)
        _forge_db_shard(global_item.pk, "*")
        target = self._make_target_room()

        result = cross_shard_move(char, "shard1", target.pk)

        # Char + normal_item moved; global_item untouched.
        self.assertEqual(result.objects_moved, 2)
        global_shard = (
            ObjectDB.objects.filter(pk=global_item.pk)
            .values_list("shard_id", flat=True)
            .first()
        )
        self.assertEqual(global_shard, "*")

    # ── flush_from_cache bus message ────────────────────────────────

    def test_move_inserts_flush_from_cache_bus_message(self):
        """After a cross-shard move, a ``flush_from_cache`` row is
        queued for the destination shard."""
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Message

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        Message.objects.filter(kind="flush_from_cache").delete()
        cross_shard_move(char, "shard1", target.pk)

        msgs = list(Message.objects.filter(kind="flush_from_cache"))
        self.assertEqual(len(msgs), 1)
        msg = msgs[0]
        self.assertEqual(msg.to_shard, "shard1")
        self.assertEqual(msg.from_shard, "shard0")
        self.assertEqual(msg.payload, {"pks": [target.pk]})

    def test_same_shard_move_skips_flush_send(self):
        """A move whose target equals the current shard does not send
        a flush_from_cache bus message (the bus refuses same-shard
        sends; the gate skips them upstream)."""
        from evennia_shards import cross_shard_move
        from evennia_shards.models import Message

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room(target_shard="shard0")

        Message.objects.filter(kind="flush_from_cache").delete()
        cross_shard_move(char, "shard0", target.pk)

        self.assertEqual(
            Message.objects.filter(kind="flush_from_cache").count(), 0,
        )

    def test_flush_send_failure_does_not_roll_back_move(self):
        """A bus failure on the post-move flush_from_cache send is
        logged but doesn't roll back the move itself."""
        from unittest import mock
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()

        with mock.patch(
            "evennia_shards.messagebus.send_message",
            side_effect=RuntimeError("bus down"),
        ):
            result = cross_shard_move(char, "shard1", target.pk)

        self.assertEqual(result.objects_moved, 1)
        char_shard = self._read_row(char.pk, "shard_id")
        self.assertEqual(char_shard, ("shard1",))

    # ── multitenant-specific gap-fillers ────────────────────────────

    def test_moved_row_invisible_to_source_shard_default_query(self):
        """After the move, the source shard's auto-filter excludes the
        moved row from default queries. This is the multitenant
        equivalent of the old ``from_db`` chokepoint refusal — the
        row is foreign to source, so it just isn't in scope anymore.
        """
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()
        char_pk = char.pk

        cross_shard_move(char, "shard1", target.pk)

        # From shard0's context (the test process), the auto-filter
        # excludes shard1 rows. The default manager doesn't see it.
        self.assertEqual(
            ObjectDB.objects.filter(pk=char_pk).count(), 0,
        )
        with self.assertRaises(ObjectDB.DoesNotExist):
            ObjectDB.objects.get(pk=char_pk)

    def test_moved_row_visible_under_target_shard_context(self):
        """The moved row is visible when scope switches to the target
        shard's context. Confirms the move actually placed the row on
        target_shard (not just hidden it from source)."""
        from evennia_shards import cross_shard_move

        char, _, _ = self._make_char(n_sessions=0)
        target = self._make_target_room()
        char_pk = char.pk

        cross_shard_move(char, "shard1", target.pk)

        with shard_context("shard1"):
            self.assertEqual(
                ObjectDB.objects.filter(pk=char_pk).count(), 1,
            )
            row = ObjectDB.objects.get(pk=char_pk)
            self.assertEqual(row.shard_id, "shard1")
            self.assertEqual(row.db_location_id, target.pk)

    def test_immutability_check_still_protects_against_save_path(self):
        """Even after cross_shard_move's ``qs.update`` succeeds, the
        ``shard_id``-immutability check on ``__setattr__`` + ``save``
        still refuses ordinary user code that tries to mutate the
        tenant column. The escape hatch (``qs.update``) is deliberately
        narrower than ``save()`` so casual misuse still fails loudly.
        """
        from django.db.utils import NotSupportedError

        local = ObjectDB.objects.create(
            db_key="probe", db_typeclass_path=TYPECLASS,
        )
        # Reset so the new shard_id assignment is treated as a mutation.
        self.assertEqual(local.shard_id, "shard0")

        local.shard_id = "shard1"  # flags _try_update_tenant
        with self.assertRaises(NotSupportedError):
            local.save()


# ---------------------------------------------------------------------------
# at_post_login override (router-side: shard_aware_at_post_login)
# ---------------------------------------------------------------------------


@override_settings(
    SHARDS_ROLE="router",
    SHARD_ID="router",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
)
class AtPostLoginRouterTests(BaseEvenniaTestCase):
    """Direct tests for ``shard_aware_at_post_login`` on routers.

    The override replaces Evennia's ``at_post_login`` on routers,
    intercepting the ``AUTO_PUPPET_ON_LOGIN=True`` branch and converting
    it to a ticket redirect (or, on fallback, the OOC menu). The router
    runs unscoped (see ``bootstrap_tenant_context``), so the override's
    ``ObjectDB`` reads are unaffected by tenant filtering.
    """

    def test_valid_last_puppet_redirects_and_runs_prelude(self):
        """``_last_puppet`` on a real shard → redirect + prelude side-effects."""
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        session = _FakeSession(address="10.0.0.1")

        shard_aware_at_post_login(account, session=session)

        # Prelude side-effects ran.
        self.assertEqual(session.oob_messages.get("logged_in"), {})
        self.assertEqual(len(account.connect_channel_messages), 1)
        self.assertIn("connected", account.connect_channel_messages[0])

        # Redirect happened: ticket created and shard_redirect OOB sent.
        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, 7)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")
        self.assertIn("shard_redirect", session.oob_messages)

        # Fallback path did NOT run.
        self.assertEqual(account.at_look_calls, [])

    def test_no_last_puppet_falls_through_to_ooc_menu_silently(self):
        """``_last_puppet=None`` → OOC menu, no warning, no ticket."""
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        account = _FakeAccount(pk=7)
        session = _FakeSession()

        with self.assertNoLogs("evennia", level="WARNING"):
            shard_aware_at_post_login(account, session=session)

        self.assertEqual(session.oob_messages.get("logged_in"), {})
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)
        self.assertEqual(len(account.at_look_calls), 1)
        self.assertIn("OOC menu", account.account_messages)

    def test_unusable_shard_id_warns_and_falls_through(self):
        """``_last_puppet`` set with broken ``shard_id`` → warning + OOC menu."""
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        for bad_shard_id in (None, "*", "unknown_shard"):
            with self.subTest(shard_id=bad_shard_id):
                Ticket.objects.all().delete()
                char = _FakeCharacter("Bob", pk=42, shard_id=bad_shard_id)
                account = _FakeAccount(pk=7)
                account.db._last_puppet = char
                session = _FakeSession()

                shard_aware_at_post_login(account, session=session)

                self.assertEqual(Ticket.objects.count(), 0)
                self.assertNotIn("shard_redirect", session.oob_messages)
                self.assertEqual(len(account.at_look_calls), 1)

    def test_at_ooc_menu_flag_skips_auto_redirect(self):
        """``account.db._shards_at_ooc_menu=True`` → OOC menu, no redirect.

        The flag is the persistent OOC-intent signal. Covers the
        refresh / reconnect / next-day-login path: no fresh ticket
        auth on this connection, but the persisted flag is True so
        the OOC menu is rendered.
        """
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        account.db._shards_at_ooc_menu = True
        session = _FakeSession()

        shard_aware_at_post_login(account, session=session)

        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)
        self.assertEqual(len(account.at_look_calls), 1)

    def test_ticket_authed_protocol_flag_persists_to_account(self):
        """``protocol_flags.SHARDS_TICKET_AUTHED=True`` → persist + OOC menu.

        Fresh @ooc arrival flow: Portal sets ``protocol_flags`` on the
        ticket-auth path, AMP-syncs to Server. The override reads the
        flag, persists OOC intent to the account, and renders the OOC
        menu (suppressing auto-puppet even with a redirectable
        ``_last_puppet`` set).
        """
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        account.db._shards_at_ooc_menu = False
        session = _FakeSession()
        session.protocol_flags["SHARDS_TICKET_AUTHED"] = True

        shard_aware_at_post_login(account, session=session)

        self.assertTrue(account.db._shards_at_ooc_menu)
        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)
        self.assertEqual(len(account.at_look_calls), 1)

    @override_settings(AUTO_PUPPET_ON_LOGIN=False)
    def test_auto_puppet_disabled_renders_ooc_menu_unconditionally(self):
        """``AUTO_PUPPET_ON_LOGIN=False`` → OOC menu, no redirect.

        If the consumer has disabled auto-puppet, the library's
        override must not auto-redirect either — vanilla's else-branch
        always renders the OOC menu in that case.
        """
        from evennia_shards.hooks import shard_aware_at_post_login
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        session = _FakeSession()

        shard_aware_at_post_login(account, session=session)

        self.assertEqual(Ticket.objects.count(), 0)
        self.assertNotIn("shard_redirect", session.oob_messages)
        self.assertEqual(len(account.at_look_calls), 1)


# ---------------------------------------------------------------------------
# at_post_login override (shard-side: make_shard_at_post_login)
# ---------------------------------------------------------------------------


@override_settings(
    SHARDS_ROLE="shard",
    SHARD_ID="shard0",
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class AtPostLoginShardTests(BaseEvenniaTestCase):
    """Direct tests for ``make_shard_at_post_login`` on shards.

    The shard-side override is a thin wrapper around Evennia's original
    ``at_post_login``. It flushes the idmapper / Attribute cache for
    ``_last_puppet`` so vanilla puppet_object works against the current
    DB state. Under multitenant, if the character has been moved off
    this shard while the account was offline, ``refresh_from_db`` raises
    ``ObjectDB.DoesNotExist`` (the auto-filter excludes the foreign row);
    the wrapper catches that and nulls out ``_last_puppet`` so the
    original falls through to the OOC menu.
    """

    def _make_wrapped(self):
        """Build a wrapped at_post_login + the call recorder it wraps."""
        from evennia_shards.hooks import make_shard_at_post_login

        calls = []

        def original_at_post_login(account, session=None, **kwargs):
            calls.append({
                "last_puppet": account.db._last_puppet,
                "session": session,
            })

        return make_shard_at_post_login(original_at_post_login), calls

    def test_normal_refresh_passes_through_to_original(self):
        """Local character row exists → refresh_from_db succeeds → original fires."""
        wrapped, calls = self._make_wrapped()

        char = ObjectDB.objects.create(
            db_key="local_char", db_typeclass_path=TYPECLASS,
        )
        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        session = _FakeSession()

        wrapped(account, session=session)

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]["last_puppet"], char)
        self.assertIs(calls[0]["session"], session)

    def test_foreign_row_clears_last_puppet_and_passes_through(self):
        """Character moved off this shard → DoesNotExist → ``_last_puppet`` cleared.

        The new (multitenant-specific) failure-mode coverage: if another
        process moved the character to a different shard while the
        account was offline, the row is foreign to this shard and the
        auto-filter excludes it. ``refresh_from_db`` raises
        ``DoesNotExist``; the wrapper catches it, nulls
        ``_last_puppet``, then calls original — which sees None and
        falls through to the OOC menu.
        """
        wrapped, calls = self._make_wrapped()

        char = ObjectDB.objects.create(
            db_key="foreign_char", db_typeclass_path=TYPECLASS,
        )
        # Force the row onto another shard via raw SQL — simulates a
        # cross_shard_move that happened on a different process.
        _forge_db_shard(char.pk, "shard1")

        account = _FakeAccount(pk=7)
        account.db._last_puppet = char
        session = _FakeSession()

        wrapped(account, session=session)

        # Original still fired (auto-puppet flow proceeds), but with
        # _last_puppet cleared so vanilla renders the OOC menu.
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["last_puppet"])
        self.assertIsNone(account.db._last_puppet)

    def test_no_last_puppet_passes_through_unchanged(self):
        """``_last_puppet=None`` → skip refresh, call original directly."""
        wrapped, calls = self._make_wrapped()

        account = _FakeAccount(pk=7)
        session = _FakeSession()

        wrapped(account, session=session)

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["last_puppet"])

    def test_character_without_flush_from_cache_passes_through(self):
        """Defensive: non-ObjectDB ``_last_puppet`` → skip refresh.

        Some test/edge configurations may store a non-ObjectDB object
        as ``_last_puppet`` (e.g. a fake). The wrapper's
        ``hasattr(character, 'flush_from_cache')`` guard ensures these
        don't crash the login flow.
        """
        wrapped, calls = self._make_wrapped()

        class _PlainObj:
            pass

        account = _FakeAccount(pk=7)
        account.db._last_puppet = _PlainObj()
        session = _FakeSession()

        wrapped(account, session=session)

        self.assertEqual(len(calls), 1)


# ---------------------------------------------------------------------------
# Consumer-override detection (warn_if_at_post_login_overridden)
# ---------------------------------------------------------------------------


class WarnIfAtPostLoginOverriddenTests(BaseEvenniaTestCase):
    """Detect consumer overrides of ``Account.at_post_login``.

    The library patches ``DefaultAccount.at_post_login`` directly. A
    consumer subclass that overrides ``at_post_login`` shadows the
    library's patch via Python MRO unless the override calls
    ``super()``. The detector walks the MRO and returns True iff an
    override is present below ``DefaultAccount``.
    """

    def test_default_account_returns_false(self):
        """``DefaultAccount`` itself is the patch target — no warning."""
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        self.assertFalse(
            warn_if_at_post_login_overridden(DefaultAccount, "router")
        )
        self.assertFalse(
            warn_if_at_post_login_overridden(DefaultAccount, "shard")
        )

    def test_subclass_with_intermediate_override_returns_true(self):
        """Override at any level between leaf and ``DefaultAccount`` triggers."""
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class MidLevelAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                pass

        class LeafAccount(MidLevelAccount):
            pass

        self.assertTrue(
            warn_if_at_post_login_overridden(LeafAccount, "router")
        )

    def test_subclass_without_override_returns_false(self):
        """Subclass with no ``at_post_login`` override: no warning."""
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class PassThroughAccount(DefaultAccount):
            pass

        self.assertFalse(
            warn_if_at_post_login_overridden(PassThroughAccount, "router")
        )
        self.assertFalse(
            warn_if_at_post_login_overridden(PassThroughAccount, "shard")
        )

    def test_subclass_with_override_returns_true(self):
        """Consumer override is detected."""
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class ShadowingAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                pass  # consumer override that does NOT call super()

        self.assertTrue(
            warn_if_at_post_login_overridden(ShadowingAccount, "router")
        )
        self.assertTrue(
            warn_if_at_post_login_overridden(ShadowingAccount, "shard")
        )

    def test_subclass_with_super_calling_override_still_returns_true(self):
        """A correct (super-calling) override still triggers the warning.

        Detection is by ``__dict__`` membership and can't distinguish a
        well-behaved override from a shadowing one. The false-positive
        cost is one log line at startup; documented as deliberate in
        the function's docstring.
        """
        from evennia.accounts.accounts import DefaultAccount

        from evennia_shards.hooks import warn_if_at_post_login_overridden

        class CooperativeAccount(DefaultAccount):
            def at_post_login(self, session=None, **kwargs):
                super().at_post_login(session=session, **kwargs)

        self.assertTrue(
            warn_if_at_post_login_overridden(CooperativeAccount, "router")
        )


# ---------------------------------------------------------------------------
# Character creation wrapper (router-side: make_shard_aware_create_character)
# ---------------------------------------------------------------------------


@override_settings(SHARDS_ROLE="router", SHARD_ID="router")
class ShardAwareCreateCharacterTests(BaseEvenniaTestCase):
    """Direct tests for ``make_shard_aware_create_character``.

    The wrapper sits on the router-side ``Account.create_character`` seam
    (the converging point for ``CmdCharCreate``,
    ``AUTO_CREATE_CHARACTER_WITH_ACCOUNT``, and the guest path). On
    successful chargen it reads the new character's start-location row's
    ``shard_id`` via ``.values_list`` and stamps the character to match,
    overwriting the ``NULL`` left by the unscoped router's skipped
    auto-stamp. Tests use real ``ObjectDB`` rows for the location lookup;
    vanilla ``create_character`` is a stub callable that returns a
    pre-built character row.

    The test class clears the tenant context in ``setUp`` and restores
    it in ``tearDown``. ``@override_settings`` changes the settings
    dict but does not re-run ``bootstrap_tenant_context()``, and the
    suite-wide bootstrap leaves the process scoped to ``shard0``; for
    the wrapper to behave router-like (unscoped) the tests have to
    manage that context explicitly.
    """

    def setUp(self):
        self._previous_tenant = get_current_tenant()
        clear_shard_context()

    def tearDown(self):
        if self._previous_tenant is None:
            clear_shard_context()
        else:
            from django_multitenant.utils import set_current_tenant
            set_current_tenant(self._previous_tenant)

    def _make_room(self, shard_id):
        """Create an ObjectDB row to act as the start location."""
        room = ObjectDB.objects.create(
            db_key="start_room", db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(room.pk, shard_id)
        return room

    def _make_character(self, location):
        """Create an ObjectDB row to act as the new character.

        Under multitenant on the router (unscoped), the auto-stamp on
        insert is skipped, so the new row lands with ``shard_id=NULL``.
        """
        char = ObjectDB.objects.create(
            db_key="newchar",
            db_typeclass_path=TYPECLASS,
            db_location=location,
        )
        return char

    def _stub_original(self, character, errs=None):
        """Build a vanilla-shaped ``create_character`` stub.

        Records its kwargs so tests can assert pass-through.
        """
        recorder = {"args": None, "kwargs": None}

        def _original(self, *args, **kwargs):
            recorder["args"] = args
            recorder["kwargs"] = kwargs
            return character, errs

        return _original, recorder

    def _persisted_shard(self, pk):
        """Read a row's shard_id directly, bypassing the auto-filter."""
        with shard_context(None):
            return (
                ObjectDB.objects.filter(pk=pk)
                .values_list("shard_id", flat=True)[0]
            )

    def test_stamps_shard_id_from_start_location(self):
        """Happy path: new character's shard_id matches start room's."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="shard0")
        char = self._make_character(location=room)
        # Sanity: unscoped router auto-stamp is skipped — row lands NULL.
        self.assertIsNone(char.shard_id)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        result_char, result_errs = wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIs(result_char, char)
        self.assertIsNone(result_errs)
        # Persisted update_fields=["shard_id"] write.
        self.assertEqual(self._persisted_shard(char.pk), "shard0")
        # In-memory instance also updated.
        self.assertEqual(char.shard_id, "shard0")

    def test_passes_through_when_vanilla_returns_none(self):
        """Vanilla refused (e.g. slot limit) → wrapper returns same tuple."""
        from evennia_shards.chargen import make_shard_aware_create_character

        errs = ["You have reached the max characters for this account."]
        original, recorder = self._stub_original(None, errs=errs)
        wrapped = make_shard_aware_create_character(original)

        result_char, result_errs = wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(result_char)
        self.assertEqual(result_errs, errs)
        self.assertEqual(recorder["args"], ("Bob",))

    def test_unstamped_start_location_leaves_unstamped(self):
        """Start room shard_id=None → character left as ``NULL``.

        Wrapper logs a warning; we assert on the side-effect (no
        overwrite) rather than the log.
        """
        from evennia_shards.chargen import make_shard_aware_create_character

        room = ObjectDB.objects.create(
            db_key="start_room", db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(room.pk, None)
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(self._persisted_shard(char.pk))

    def test_global_start_location_leaves_unstamped(self):
        """Start room shard_id="*" → character left as ``NULL``."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="*")
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(self._persisted_shard(char.pk))

    def test_router_owned_start_location_leaves_unstamped(self):
        """Start room shard_id="router" → no overwrite."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="router")
        char = self._make_character(location=room)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(self._persisted_shard(char.pk))

    def test_no_db_location_leaves_unstamped(self):
        """Character created without db_location → no overwrite."""
        from evennia_shards.chargen import make_shard_aware_create_character

        char = ObjectDB.objects.create(
            db_key="newchar", db_typeclass_path=TYPECLASS,
        )
        self.assertIsNone(char.db_location_id)

        original, _ = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        wrapped(_FakeAccount(pk=7), "Bob")

        self.assertIsNone(self._persisted_shard(char.pk))

    def test_kwargs_flow_through_to_vanilla(self):
        """Wrapper does not mutate args/kwargs going into vanilla."""
        from evennia_shards.chargen import make_shard_aware_create_character

        room = self._make_room(shard_id="shard0")
        char = self._make_character(location=room)

        original, recorder = self._stub_original(char)
        wrapped = make_shard_aware_create_character(original)

        sentinel = object()
        wrapped(
            _FakeAccount(pk=7),
            "Bob",
            "A description.",
            typeclass="my.path",
            character_typeclass="my.path",
            extra=sentinel,
        )

        self.assertEqual(recorder["args"], ("Bob", "A description."))
        self.assertEqual(recorder["kwargs"]["typeclass"], "my.path")
        self.assertEqual(recorder["kwargs"]["character_typeclass"], "my.path")
        self.assertIs(recorder["kwargs"]["extra"], sentinel)


# ---------------------------------------------------------------------------
# Library admin commands (CmdShardCheck, CmdCrossShardDig)
# ---------------------------------------------------------------------------


@override_settings(
    SHARD_ID="shard0", SHARDS_ROLE="shard",
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CmdShardCheckTests(BaseEvenniaTestCase):
    """``CmdShardCheck`` reports an object's shard_id (ORM + raw SQL)."""

    def _make_cmd(self, args="", search_returns=None):
        from evennia_shards.commands import CmdShardCheck

        cmd = CmdShardCheck()
        cmd.args = args
        cmd.caller = _FakeCaller(search_returns=search_returns)
        return cmd

    def test_no_args_shows_usage(self):
        cmd = self._make_cmd("")
        cmd.func()
        self.assertEqual(len(cmd.caller.messages), 1)
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_unknown_target_returns_silently(self):
        # search() returning None means "not found" — Evennia has
        # already messaged the caller. Command should add nothing.
        cmd = self._make_cmd("ghost", search_returns=None)
        cmd.func()
        self.assertEqual(cmd.caller.messages, [])

    def test_reports_shard_id_for_target(self):
        target = ObjectDB.objects.create(db_key="x", db_typeclass_path=TYPECLASS)
        cmd = self._make_cmd("x", search_returns=target)
        cmd.func()

        # Both ORM and raw-SQL probes report the value.
        joined = "\n".join(cmd.caller.messages)
        self.assertIn("ORM:", joined)
        self.assertIn("DB:", joined)
        self.assertIn("shard0", joined)


@override_settings(
    SHARD_ID="shard0", SHARDS_ROLE="shard",
    SHARD_URLS={
        "shard0": "ws://localhost:4011/",
        "shard1": "ws://localhost:4021/",
    },
)
class CmdCrossShardDigTests(BaseEvenniaTestCase):
    """``CmdCrossShardDig`` creates a room stamped with a target shard's id.

    Under multitenant the wrapper uses ``shard_context(target_shard)``
    around ``create_object`` so the auto-stamp lands the target shard
    on insert — no post-creation re-stamp needed.
    """

    def _make_cmd(self, args=""):
        from evennia_shards.commands import CmdCrossShardDig

        cmd = CmdCrossShardDig()
        cmd.args = args
        cmd.caller = _FakeCaller()
        return cmd

    def test_no_args_shows_usage(self):
        cmd = self._make_cmd("")
        cmd.func()
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_one_arg_shows_usage(self):
        cmd = self._make_cmd("shard1")
        cmd.func()
        self.assertIn("Usage:", cmd.caller.messages[0])

    def test_unknown_shard_id_reports_error_no_room_created(self):
        before = ObjectDB.objects.count()
        cmd = self._make_cmd("nonexistent_shard MyRoom")
        cmd.func()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("nonexistent_shard", msg)
        self.assertIn("not configured", msg)
        # Validation precedes creation: row count unchanged.
        self.assertEqual(ObjectDB.objects.count(), before)

    def test_creates_room_stamped_to_target_shard(self):
        cmd = self._make_cmd("shard1 TargetLimbo")
        cmd.func()

        # Read with auto-filter escape — the new row lives on shard1,
        # which the suite's default shard0 scope would exclude.
        with shard_context(None):
            rows = list(
                ObjectDB.objects.filter(db_key="TargetLimbo")
                .values_list("shard_id", "db_location_id")
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], ("shard1", None))

        # Success message includes the target shard.
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("TargetLimbo", msg)
        self.assertIn("shard1", msg)


class AdminCommandAutoInstallTests(BaseEvenniaTestCase):
    """Library admin commands auto-install into ``CharacterCmdSet``.

    The install happens via a wrapper around ``evennia._init`` registered
    in ``AppConfig.ready()``. The test runner calls ``evennia._init()``
    explicitly, so by the time tests run the patch is in place.
    """

    def test_character_cmdset_contains_library_commands(self):
        from evennia.commands.default.cmdset_character import CharacterCmdSet

        cmdset = CharacterCmdSet()
        cmdset.at_cmdset_creation()
        keys = {cmd.key for cmd in cmdset.commands}
        self.assertIn("@shard_check", keys)
        self.assertIn("cross_shard_dig", keys)


# ---------------------------------------------------------------------------
# ShardAwareCmdIC (router-side redirect, shard-side reject)
# ---------------------------------------------------------------------------


def _make_ic_cmd(args="", account=None, session=None, characters=None):
    """Build a ShardAwareCmdIC instance wired up for testing."""
    from evennia_shards.commands import ShardAwareCmdIC

    cmd = ShardAwareCmdIC()
    cmd.args = args
    cmd.raw_string = f"ic {args}"
    cmd.session = session or _FakeSession()
    if account is None:
        account = _FakeAccount(characters=characters or [])
    cmd.account = account
    cmd.caller = account
    cmd._messages = []

    def _msg(text, **kwargs):
        cmd._messages.append(text)

    cmd.msg = _msg
    return cmd


@override_settings(
    SHARDS_ROLE="shard", SHARD_ID="shard0",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
)
class ShardAwareCmdICShardTests(BaseEvenniaTestCase):
    """IC command on a shard tells the player to return to the router."""

    def test_shard_rejects_ic(self):
        cmd = _make_ic_cmd(args="Bob")
        cmd.func()
        self.assertEqual(len(cmd._messages), 1)
        self.assertIn("Leave this character", cmd._messages[0])

    def test_shard_rejects_ic_no_args(self):
        cmd = _make_ic_cmd(args="")
        cmd.func()
        self.assertEqual(len(cmd._messages), 1)
        self.assertIn("Leave this character", cmd._messages[0])


@override_settings(
    SHARDS_ROLE="router", SHARD_ID="router",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
)
class ShardAwareCmdICRouterTests(BaseEvenniaTestCase):
    """IC command on the router creates a ticket and redirects."""

    def test_router_creates_ticket_and_redirects(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession()
        cmd = _make_ic_cmd(args="Bob", characters=[char], session=session)
        cmd.func()

        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, cmd.account.id)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")

        self.assertIn("shard_redirect", session.oob_messages)
        url = session.oob_messages["shard_redirect"][0][0]
        self.assertIn("ws://localhost:4011/?ticket=", url)
        self.assertIn(ticket.token, url)

    def test_router_sets_last_puppet(self):
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(characters=[char])
        cmd = _make_ic_cmd(args="Bob", account=account)
        cmd.func()
        self.assertIs(account.db._last_puppet, char)

    def test_router_no_args_uses_last_puppet(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(characters=[char])
        account.db._last_puppet = char
        session = _FakeSession()
        cmd = _make_ic_cmd(args="", account=account, session=session)
        cmd.func()

        self.assertEqual(Ticket.objects.count(), 1)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_router_no_args_no_last_puppet_shows_usage(self):
        cmd = _make_ic_cmd(args="")
        cmd.func()
        self.assertTrue(any("Usage:" in m for m in cmd._messages))

    def test_router_character_no_shard_id_gives_error(self):
        char = _FakeCharacter("Bob", pk=42, shard_id=None)
        cmd = _make_ic_cmd(args="Bob", characters=[char])
        cmd.func()
        self.assertTrue(any("no shard assignment" in m for m in cmd._messages))

    def test_router_character_global_shard_gives_error(self):
        char = _FakeCharacter("Bob", pk=42, shard_id="*")
        cmd = _make_ic_cmd(args="Bob", characters=[char])
        cmd.func()
        self.assertTrue(any("no shard assignment" in m for m in cmd._messages))

    def test_router_character_not_found(self):
        cmd = _make_ic_cmd(args="Nobody", characters=[])
        cmd.func()
        self.assertTrue(any("not a valid character" in m for m in cmd._messages))

    def test_router_ip_pinned_in_ticket(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession(address="10.0.0.1")
        cmd = _make_ic_cmd(args="Bob", characters=[char], session=session)
        cmd.func()

        ticket = Ticket.objects.first()
        self.assertEqual(ticket.client_ip, "10.0.0.1")

    def test_router_ic_clears_ooc_menu_flag(self):
        """ic on the router clears account.db._shards_at_ooc_menu."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(characters=[char])
        account.db._shards_at_ooc_menu = True
        cmd = _make_ic_cmd(args="Bob", account=account)
        cmd.func()

        self.assertFalse(account.db._shards_at_ooc_menu)


# ---------------------------------------------------------------------------
# ShardAwareCmdOOC (shard-side redirect to router)
# ---------------------------------------------------------------------------


def _make_ooc_cmd(account=None, session=None, puppet=None):
    """Build a ShardAwareCmdOOC instance wired up for testing."""
    from evennia_shards.commands import ShardAwareCmdOOC

    cmd = ShardAwareCmdOOC()
    cmd.args = ""
    cmd.raw_string = "ooc"
    if session is None:
        session = _FakeSession()
    if puppet is not None:
        session.puppet = puppet
    cmd.session = session
    if account is None:
        account = _FakeAccount()
    cmd.account = account
    cmd.caller = account
    cmd._messages = []

    def _msg(text, **kwargs):
        cmd._messages.append(text)

    cmd.msg = _msg
    return cmd


@override_settings(
    SHARDS_ROLE="shard", SHARD_ID="shard0",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
    ROUTER_URL="ws://localhost:4001/",
)
class ShardAwareCmdOOCShardTests(BaseEvenniaTestCase):
    """OOC command on a shard creates a ticket and redirects to router."""

    def test_shard_with_puppet_creates_ticket_and_redirects(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession()
        cmd = _make_ooc_cmd(session=session, puppet=char)
        cmd.func()

        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].character_id, 42)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_shard_no_puppet_with_last_puppet_redirects(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char
        session = _FakeSession()
        cmd = _make_ooc_cmd(account=account, session=session)
        cmd.func()

        self.assertEqual(Ticket.objects.first().character_id, 42)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_shard_no_puppet_no_last_puppet_redirects_with_zero(self):
        """Broken state: no puppet, no _last_puppet → ticket with character_id=0."""
        from evennia_shards.models import Ticket

        session = _FakeSession()
        cmd = _make_ooc_cmd(session=session)
        cmd.func()

        self.assertEqual(Ticket.objects.first().character_id, 0)
        self.assertIn("shard_redirect", session.oob_messages)

    def test_shard_ip_pinned_in_ticket(self):
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        session = _FakeSession(address="10.0.0.1")
        cmd = _make_ooc_cmd(session=session, puppet=char)
        cmd.func()

        self.assertEqual(Ticket.objects.first().client_ip, "10.0.0.1")

    def test_shard_ticket_to_shard_is_router(self):
        """OOC tickets target the router, not a shard."""
        from evennia_shards.models import Ticket

        from evennia_shards.config import get_router_shard_id

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        cmd = _make_ooc_cmd(puppet=char)
        cmd.func()

        self.assertEqual(Ticket.objects.first().to_shard, get_router_shard_id())

    def test_shard_does_not_mutate_last_puppet(self):
        """OOC does not clear _last_puppet (only IC writes that flag)."""
        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount()
        account.db._last_puppet = char
        cmd = _make_ooc_cmd(account=account, puppet=char)
        cmd.func()

        self.assertIs(account.db._last_puppet, char)


# ---------------------------------------------------------------------------
# shard_aware_global_search (escapes the auto-filter to see every shard)
# ---------------------------------------------------------------------------


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class ShardAwareGlobalSearchTests(BaseEvenniaTestCase):
    """``shard_aware_global_search`` — see rows on every shard.

    Under multitenant, ``ObjectDB.objects`` carries the tenant
    auto-filter; a stock global search would miss foreign rows
    entirely. The helper wraps its ``values_list`` in
    ``shard_context(None)`` to escape that filter, then dispatches
    on the match's shard — loading the instance if it's local,
    returning metadata only if it's on another shard.
    """

    def _make_local(self, key="local_room"):
        return ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS,
        )

    def _make_remote(self, key="remote_room", shard="shard1"):
        obj = ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS,
        )
        _forge_db_shard(obj.pk, shard)
        return obj

    # ── basic name resolution ─────────────────────────────────────

    def test_not_found_when_no_match(self):
        from evennia_shards import shard_aware_global_search

        result = shard_aware_global_search(None, "nothing_here")
        self.assertEqual(result.state, "not_found")
        self.assertIsNone(result.obj)
        self.assertIsNone(result.pk)
        self.assertFalse(result.is_local)
        self.assertFalse(result.is_cross_shard)

    def test_empty_name_is_not_found(self):
        from evennia_shards import shard_aware_global_search

        self.assertEqual(
            shard_aware_global_search(None, "").state, "not_found"
        )

    def test_whitespace_only_name_is_not_found(self):
        from evennia_shards import shard_aware_global_search

        self.assertEqual(
            shard_aware_global_search(None, "   ").state, "not_found"
        )

    def test_db_key_match_is_case_insensitive(self):
        from evennia_shards import shard_aware_global_search

        self._make_local(key="MixedCase")
        for variant in ("mixedcase", "MIXEDCASE", "MixedCase"):
            result = shard_aware_global_search(None, variant)
            self.assertEqual(result.state, "found", variant)
            self.assertEqual(result.db_key, "MixedCase")

    def test_dbref_match(self):
        from evennia_shards import shard_aware_global_search

        target = self._make_local()
        result = shard_aware_global_search(None, f"#{target.pk}")
        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, target.pk)

    def test_dbref_with_garbage_falls_through_to_key_lookup(self):
        from evennia_shards import shard_aware_global_search

        result = shard_aware_global_search(None, "#notanint")
        self.assertEqual(result.state, "not_found")

    # ── local vs cross-shard routing ──────────────────────────────

    def test_local_match_populates_instance(self):
        from evennia_shards import shard_aware_global_search

        target = self._make_local(key="local_target")
        result = shard_aware_global_search(None, "local_target")

        self.assertEqual(result.state, "found")
        self.assertIsNotNone(result.obj)
        self.assertEqual(result.obj.pk, target.pk)
        self.assertEqual(result.pk, target.pk)
        self.assertEqual(result.shard_id, "shard0")
        self.assertTrue(result.is_local)
        self.assertFalse(result.is_cross_shard)

    def test_cross_shard_match_omits_instance(self):
        from evennia_shards import shard_aware_global_search

        target = self._make_remote(key="remote_target", shard="shard1")
        result = shard_aware_global_search(None, "remote_target")

        self.assertEqual(result.state, "found")
        self.assertIsNone(result.obj)
        self.assertEqual(result.pk, target.pk)
        self.assertEqual(result.shard_id, "shard1")
        self.assertFalse(result.is_local)
        self.assertTrue(result.is_cross_shard)

    def test_global_sentinel_match_is_local(self):
        from evennia_shards import shard_aware_global_search

        target = self._make_remote(key="global_thing", shard="*")
        result = shard_aware_global_search(None, "global_thing")

        self.assertEqual(result.state, "found")
        self.assertIsNotNone(result.obj)
        self.assertEqual(result.shard_id, "*")
        self.assertTrue(result.is_local)
        self.assertFalse(result.is_cross_shard)

    # ── multi-match disambiguation ────────────────────────────────

    def test_multiple_matches_returns_candidates_list(self):
        from evennia_shards import shard_aware_global_search

        a = self._make_local(key="Tavern")
        b = self._make_remote(key="Tavern", shard="shard1")

        result = shard_aware_global_search(None, "Tavern")

        self.assertEqual(result.state, "multiple")
        self.assertIsNone(result.obj)
        pks_in_candidates = {c[0] for c in result.candidates}
        self.assertEqual(pks_in_candidates, {a.pk, b.pk})

    def test_multiple_matches_is_local_false(self):
        from evennia_shards import shard_aware_global_search

        self._make_local(key="x")
        self._make_remote(key="x", shard="shard1")

        result = shard_aware_global_search(None, "x")

        self.assertFalse(result.is_local)
        self.assertFalse(result.is_cross_shard)

    # ── tag filtering ─────────────────────────────────────────────

    def test_tag_filter_narrows_results(self):
        from evennia_shards import shard_aware_global_search

        scoped = self._make_local(key="Tavern")
        scoped.tags.add("millholm", category="zone")
        self._make_local(key="Tavern")

        self.assertEqual(
            shard_aware_global_search(None, "Tavern").state, "multiple"
        )
        result = shard_aware_global_search(
            None, "Tavern", tag="millholm",
        )
        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, scoped.pk)

    def test_tag_and_category_filter_narrows_further(self):
        from evennia_shards import shard_aware_global_search

        zone_tagged = self._make_local(key="Forge")
        zone_tagged.tags.add("millholm", category="zone")

        meta_tagged = self._make_local(key="Forge")
        meta_tagged.tags.add("millholm", category="meta")

        self.assertEqual(
            shard_aware_global_search(None, "Forge", tag="millholm").state,
            "multiple",
        )
        result = shard_aware_global_search(
            None, "Forge", tag="millholm", tag_category="zone",
        )
        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, zone_tagged.pk)

    def test_tag_filter_with_no_matching_tag_is_not_found(self):
        from evennia_shards import shard_aware_global_search

        room = self._make_local(key="Tavern")
        room.tags.add("millholm", category="zone")

        result = shard_aware_global_search(
            None, "Tavern", tag="cloverfen",
        )
        self.assertEqual(result.state, "not_found")

    # ── alias matching ────────────────────────────────────────────

    def test_alias_only_match(self):
        from evennia_shards import shard_aware_global_search

        obj = self._make_local(key="Excalibur")
        obj.aliases.add("sword")

        result = shard_aware_global_search(None, "sword")

        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, obj.pk)
        self.assertEqual(result.db_key, "Excalibur")

    def test_alias_match_is_case_insensitive(self):
        from evennia_shards import shard_aware_global_search

        obj = self._make_local(key="Excalibur")
        obj.aliases.add("Sword")

        for variant in ("sword", "SWORD", "SwOrD"):
            result = shard_aware_global_search(None, variant)
            self.assertEqual(result.state, "found", variant)
            self.assertEqual(result.pk, obj.pk, variant)

    def test_key_and_alias_both_match_returns_single_result(self):
        """One row whose key AND alias both match → 'found', not 'multiple'.

        ``.distinct()`` collapses the duplicate that the OR-over-m2m
        join would otherwise produce.
        """
        from evennia_shards import shard_aware_global_search

        obj = self._make_local(key="ball")
        obj.aliases.add("ball")

        result = shard_aware_global_search(None, "ball")

        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, obj.pk)

    def test_non_alias_tag_with_matching_key_does_not_match(self):
        """Tag with the right key but wrong tagtype must NOT match."""
        from evennia_shards import shard_aware_global_search

        obj = self._make_local(key="Excalibur")
        obj.tags.add("sword", category="zone")  # zone tag, not alias

        result = shard_aware_global_search(None, "sword")

        self.assertEqual(result.state, "not_found")

    def test_alias_match_composes_with_zone_tag_filter(self):
        from evennia_shards import shard_aware_global_search

        in_zone = self._make_local(key="Excalibur")
        in_zone.aliases.add("sword")
        in_zone.tags.add("millholm", category="zone")

        out_of_zone = self._make_local(key="Anduril")
        out_of_zone.aliases.add("sword")

        self.assertEqual(
            shard_aware_global_search(None, "sword").state, "multiple"
        )
        result = shard_aware_global_search(
            None, "sword", tag="millholm", tag_category="zone",
        )
        self.assertEqual(result.state, "found")
        self.assertEqual(result.pk, in_zone.pk)

    def test_alias_match_on_cross_shard_row_returns_metadata_only(self):
        from evennia_shards import shard_aware_global_search

        remote = self._make_remote(key="Excalibur", shard="shard1")
        remote.aliases.add("sword")

        result = shard_aware_global_search(None, "sword")

        self.assertEqual(result.state, "found")
        self.assertIsNone(result.obj)
        self.assertEqual(result.pk, remote.pk)
        self.assertEqual(result.shard_id, "shard1")
        self.assertTrue(result.is_cross_shard)

    # ── caller-relative specials (me / self / here) ───────────────

    def _stub_caller_with_location(self, location=None):
        """Tiny stand-in for caller — just the attributes the special-
        token branch reads. Avoids touching ObjectDB."""
        import types

        return types.SimpleNamespace(
            pk=42, shard_id="shard0", db_key="Bob", location=location,
        )

    def test_me_token_returns_caller(self):
        from evennia_shards import shard_aware_global_search

        caller = self._stub_caller_with_location()
        result = shard_aware_global_search(caller, "me")

        self.assertEqual(result.state, "found")
        self.assertIs(result.obj, caller)
        self.assertEqual(result.pk, 42)
        self.assertEqual(result.shard_id, "shard0")
        self.assertEqual(result.db_key, "Bob")

    def test_self_token_returns_caller(self):
        from evennia_shards import shard_aware_global_search

        caller = self._stub_caller_with_location()
        result = shard_aware_global_search(caller, "self")

        self.assertEqual(result.state, "found")
        self.assertIs(result.obj, caller)

    def test_me_token_is_case_insensitive(self):
        from evennia_shards import shard_aware_global_search

        caller = self._stub_caller_with_location()
        for variant in ("ME", "Me", "  me  "):
            result = shard_aware_global_search(caller, variant)
            self.assertEqual(result.state, "found", variant)
            self.assertIs(result.obj, caller, variant)

    def test_here_token_returns_caller_location(self):
        import types

        from evennia_shards import shard_aware_global_search

        location = types.SimpleNamespace(
            pk=99, shard_id="shard0", db_key="Tavern",
        )
        caller = self._stub_caller_with_location(location=location)
        result = shard_aware_global_search(caller, "here")

        self.assertEqual(result.state, "found")
        self.assertIs(result.obj, location)
        self.assertEqual(result.pk, 99)
        self.assertEqual(result.db_key, "Tavern")

    def test_here_token_with_no_location_is_not_found(self):
        from evennia_shards import shard_aware_global_search

        caller = self._stub_caller_with_location(location=None)
        result = shard_aware_global_search(caller, "here")

        self.assertEqual(result.state, "not_found")

    def test_me_does_not_match_db_object_named_me(self):
        from evennia_shards import shard_aware_global_search

        self._make_local(key="me")  # tries to compete
        caller = self._stub_caller_with_location()

        result = shard_aware_global_search(caller, "me")

        self.assertIs(result.obj, caller)
        self.assertEqual(result.pk, 42)


# ---------------------------------------------------------------------------
# Config accessors (get_role, get_shard_id, get_shard_url, get_router_url,
# get_router_shard_id, get_message_timeout). Pure setting reads — no DB,
# no multitenant interaction. Ported verbatim from the legacy suite.
# ---------------------------------------------------------------------------


class ConfigAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_role / get_shard_id accessors."""

    @override_settings(SHARDS_ROLE="router")
    def test_get_role_reflects_setting_router(self):
        from evennia_shards import get_role

        self.assertEqual(get_role(), "router")

    @override_settings(SHARDS_ROLE="shard")
    def test_get_role_reflects_setting_shard(self):
        from evennia_shards import get_role

        self.assertEqual(get_role(), "shard")

    @override_settings(SHARD_ID="some-shard")
    def test_get_shard_id_reflects_setting(self):
        from evennia_shards import get_shard_id

        self.assertEqual(get_shard_id(), "some-shard")


class ShardUrlAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_shard_url accessor."""

    @override_settings(SHARD_URLS={"shard0": "ws://localhost:4001/"})
    def test_returns_url_for_known_shard(self):
        from evennia_shards import get_shard_url

        self.assertEqual(get_shard_url("shard0"), "ws://localhost:4001/")

    @override_settings(SHARD_URLS={"shard0": "ws://localhost:4001/"})
    def test_raises_key_error_for_unknown_shard(self):
        from evennia_shards import get_shard_url

        with self.assertRaises(KeyError):
            get_shard_url("shard99")

    @override_settings(SHARD_URLS=None)
    def test_raises_value_error_when_not_configured(self):
        from evennia_shards import get_shard_url

        with self.assertRaises(ValueError):
            get_shard_url("shard0")

    @override_settings(
        SHARD_URLS={
            "overworld": "ws://overworld.example.com/",
            "dungeons": "ws://dungeons.example.com/",
            "pvp_arena": "ws://pvp.example.com/",
        }
    )
    def test_multiple_shards_flexible_names(self):
        from evennia_shards import get_shard_url

        self.assertEqual(get_shard_url("overworld"), "ws://overworld.example.com/")
        self.assertEqual(get_shard_url("dungeons"), "ws://dungeons.example.com/")
        self.assertEqual(get_shard_url("pvp_arena"), "ws://pvp.example.com/")


class RouterUrlAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_router_url accessor."""

    @override_settings(ROUTER_URL="ws://router.example.com/")
    def test_returns_configured_url(self):
        from evennia_shards import get_router_url

        self.assertEqual(get_router_url(), "ws://router.example.com/")

    @override_settings(ROUTER_URL=None)
    def test_raises_value_error_when_not_configured(self):
        from evennia_shards import get_router_url

        with self.assertRaises(ValueError):
            get_router_url()


class RouterShardIdAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_router_shard_id accessor."""

    def test_returns_router(self):
        from evennia_shards import get_router_shard_id

        self.assertEqual(get_router_shard_id(), "router")


class MessageTimeoutAccessorTests(BaseEvenniaTestCase):
    """Tests for the get_message_timeout accessor."""

    def test_default_is_10_seconds_when_no_settings(self):
        from evennia_shards import get_message_timeout

        self.assertEqual(get_message_timeout("anything"), 10)

    @override_settings(SHARDS_MESSAGE_TIMEOUT_DEFAULT=20)
    def test_global_default_is_overridden(self):
        from evennia_shards import get_message_timeout

        self.assertEqual(get_message_timeout("anything"), 20)

    @override_settings(SHARDS_MESSAGE_TIMEOUTS={"tell": 5, "character_handoff": 30})
    def test_per_kind_override_returns_specific_timeout(self):
        from evennia_shards import get_message_timeout

        self.assertEqual(get_message_timeout("tell"), 5)
        self.assertEqual(get_message_timeout("character_handoff"), 30)

    @override_settings(
        SHARDS_MESSAGE_TIMEOUT_DEFAULT=20,
        SHARDS_MESSAGE_TIMEOUTS={"tell": 5},
    )
    def test_unmapped_kind_falls_back_to_default(self):
        from evennia_shards import get_message_timeout

        self.assertEqual(get_message_timeout("tell"), 5)
        self.assertEqual(get_message_timeout("other_kind"), 20)


# ---------------------------------------------------------------------------
# Cross-shard message bus — Message model + send / poll / delete primitives.
#
# The Message model has its own table (not ObjectDB), so multitenant doesn't
# touch it. Tests port verbatim from the legacy suite — only imports change.
# ---------------------------------------------------------------------------


class MessageModelTests(BaseEvenniaTestCase):
    """The Message model is wired and the migration deploys."""

    def test_table_name_is_namespaced(self):
        from evennia_shards.models import Message

        self.assertEqual(Message._meta.db_table, "evennia_shards_message")

    def test_create_round_trips_payload(self):
        from evennia_shards.models import Message

        msg = Message.objects.create(
            to_shard="shard1",
            from_shard="shard0",
            kind="character_handoff",
            payload={"char_id": 42, "to_room": 7},
        )
        loaded = Message.objects.get(pk=msg.pk)
        self.assertEqual(loaded.to_shard, "shard1")
        self.assertEqual(loaded.from_shard, "shard0")
        self.assertEqual(loaded.kind, "character_handoff")
        self.assertEqual(loaded.payload, {"char_id": 42, "to_room": 7})
        self.assertIsNotNone(loaded.created_at)

    def test_payload_defaults_to_empty_dict(self):
        from evennia_shards.models import Message

        msg = Message.objects.create(to_shard="shard1", kind="ping")
        self.assertEqual(msg.payload, {})

    def test_from_shard_can_be_null(self):
        from evennia_shards.models import Message

        msg = Message.objects.create(to_shard="shard1", kind="ping")
        self.assertIsNone(msg.from_shard)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class SendMessageTests(BaseEvenniaTestCase):
    """send_message primitive: insert a message row."""

    def test_returns_created_message_instance(self):
        from evennia_shards import send_message
        from evennia_shards.models import Message

        msg = send_message(
            kind="ping",
            payload={"hello": "world"},
            to_shard="shard1",
        )
        self.assertIsInstance(msg, Message)
        self.assertIsNotNone(msg.pk)

    def test_explicit_from_shard_is_recorded(self):
        from evennia_shards import send_message

        msg = send_message(
            kind="ping",
            payload={},
            to_shard="shard1",
            from_shard="shard2",
        )
        self.assertEqual(msg.from_shard, "shard2")

    def test_default_from_shard_uses_current_setting(self):
        from evennia_shards import send_message

        msg = send_message(kind="ping", payload={}, to_shard="shard1")
        self.assertEqual(msg.from_shard, "shard0")

    def test_payload_is_persisted(self):
        from evennia_shards import send_message
        from evennia_shards.models import Message

        msg = send_message(
            kind="character_handoff",
            payload={"char_id": 42, "to_room": 7},
            to_shard="shard1",
        )
        loaded = Message.objects.get(pk=msg.pk)
        self.assertEqual(loaded.payload, {"char_id": 42, "to_room": 7})

    def test_explicit_same_shard_send_raises(self):
        from evennia_shards import MessageBusError, send_message

        with self.assertRaises(MessageBusError) as ctx:
            send_message(
                kind="ping",
                payload={},
                to_shard="shard0",
                from_shard="shard0",
            )
        self.assertIn("shard0", str(ctx.exception))

    def test_default_from_shard_same_as_to_shard_raises(self):
        from evennia_shards import MessageBusError, send_message

        # SHARD_ID is "shard0" via class @override_settings; no explicit
        # from_shard, so it defaults to "shard0", matching to_shard.
        with self.assertRaises(MessageBusError):
            send_message(kind="ping", payload={}, to_shard="shard0")

    def test_no_message_row_inserted_when_same_shard_send_raises(self):
        from evennia_shards import MessageBusError, send_message
        from evennia_shards.models import Message

        before = Message.objects.count()
        with self.assertRaises(MessageBusError):
            send_message(
                kind="ping", payload={}, to_shard="shard0", from_shard="shard0",
            )
        self.assertEqual(Message.objects.count(), before)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class PollMessagesTests(BaseEvenniaTestCase):
    """poll_messages primitive: read messages addressed to a shard."""

    def test_returns_only_messages_for_requested_shard(self):
        from evennia_shards import poll_messages, send_message

        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        send_message(kind="ping", payload={}, to_shard="shard2", from_shard="shard0")
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")

        result = list(poll_messages("shard1"))
        self.assertEqual(len(result), 2)
        for msg in result:
            self.assertEqual(msg.to_shard, "shard1")

    def test_returns_empty_when_no_matching_messages(self):
        from evennia_shards import poll_messages, send_message

        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        result = list(poll_messages("shard9"))
        self.assertEqual(result, [])

    def test_results_ordered_by_created_at_ascending(self):
        from evennia_shards import poll_messages, send_message

        first = send_message(
            kind="ping", payload={"n": 1}, to_shard="shard1", from_shard="shard0",
        )
        second = send_message(
            kind="ping", payload={"n": 2}, to_shard="shard1", from_shard="shard0",
        )
        third = send_message(
            kind="ping", payload={"n": 3}, to_shard="shard1", from_shard="shard0",
        )

        result = list(poll_messages("shard1"))
        self.assertEqual([msg.pk for msg in result], [first.pk, second.pk, third.pk])

    def test_default_shard_uses_current_setting(self):
        from evennia_shards import poll_messages, send_message

        send_message(kind="ping", payload={}, to_shard="shard0", from_shard="shard1")
        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")

        result = list(poll_messages())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].to_shard, "shard0")

    def test_returns_queryset_not_list(self):
        from evennia_shards import poll_messages, send_message

        send_message(kind="ping", payload={}, to_shard="shard1", from_shard="shard0")
        result = poll_messages("shard1")
        self.assertEqual(result.count(), 1)
        self.assertIsNotNone(result.first())


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class DeleteMessageTests(BaseEvenniaTestCase):
    """delete_message primitive: remove a processed message row."""

    def test_deletes_only_the_named_message(self):
        from evennia_shards import delete_message, send_message
        from evennia_shards.models import Message

        keep = send_message(
            kind="ping", payload={}, to_shard="shard1", from_shard="shard0",
        )
        drop = send_message(
            kind="ping", payload={}, to_shard="shard1", from_shard="shard0",
        )

        delete_message(drop)

        remaining_pks = list(Message.objects.values_list("pk", flat=True))
        self.assertIn(keep.pk, remaining_pks)
        self.assertNotIn(drop.pk, remaining_pks)

    def test_subsequent_poll_does_not_return_deleted_message(self):
        from evennia_shards import delete_message, poll_messages, send_message

        msg = send_message(
            kind="ping", payload={}, to_shard="shard1", from_shard="shard0",
        )
        self.assertEqual(poll_messages("shard1").count(), 1)

        delete_message(msg)

        self.assertEqual(poll_messages("shard1").count(), 0)


# ---------------------------------------------------------------------------
# MessageHandler — dispatch for library-shipped kinds (ping, obj_msg,
# account_msg, room_msg, flush_from_cache, undeliverable_reply). All ObjectDB
# rows the handler resolves are local to the current shard scope, so the
# multitenant auto-filter doesn't change visibility for these tests.
# ---------------------------------------------------------------------------


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class MessageHandlerTests(BaseEvenniaTestCase):
    """The base MessageHandler dispatches library-shipped kinds."""

    def test_unknown_kind_returns_false(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="unknown_kind", payload={},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertFalse(MessageHandler().handle(msg))

    def test_ping_returns_true_and_inserts_ping_received_reply(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        ping = Message.objects.create(
            kind="ping", payload={"text": "hello"},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(ping))

        replies = list(Message.objects.filter(kind="ping_received"))
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply.to_shard, "shard1")
        self.assertEqual(reply.from_shard, "shard0")
        self.assertEqual(
            reply.payload, {"original_pk": ping.pk, "echo": {"text": "hello"}},
        )

    def test_ping_with_no_from_shard_returns_true_and_no_reply(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        ping = Message.objects.create(
            kind="ping", payload={}, to_shard="shard0", from_shard=None,
        )
        self.assertTrue(MessageHandler().handle(ping))
        self.assertFalse(Message.objects.filter(kind="ping_received").exists())

    def test_ping_received_returns_true_and_inserts_nothing(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="ping_received",
            payload={"original_pk": 99, "echo": {}},
            to_shard="shard0", from_shard="shard1",
        )
        before_count = Message.objects.count()
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(Message.objects.count(), before_count)

    def test_obj_msg_calls_target_msg_with_kwargs(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        recorded_kwargs = {}
        # Shadow the typeclass-level msg via __dict__ (bypasses
        # Evennia's protective __setattr__).
        target.__dict__["msg"] = lambda **kwargs: recorded_kwargs.update(kwargs)

        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": {"text": "hello"}},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(recorded_kwargs, {"text": "hello"})

    def test_obj_msg_passes_oob_kwargs_intact(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        captured = []
        target.__dict__["msg"] = lambda **kwargs: captured.append(kwargs)

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1", "ticket": "abc"},
            "options": {"raw": True},
        }
        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": kwargs},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(captured, [kwargs])

    def test_obj_msg_target_gone_returns_true_and_inserts_nothing(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": 999_999, "kwargs": {"text": "hi"}},
            to_shard="shard0", from_shard="shard1",
        )
        before_count = Message.objects.count()
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(Message.objects.count(), before_count)

    def test_account_msg_calls_target_msg_with_kwargs(self):
        from evennia.accounts.models import AccountDB

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = AccountDB.objects.create(
            username="msg_target",
            db_typeclass_path="evennia.accounts.accounts.DefaultAccount",
        )
        recorded_kwargs = {}
        target.__dict__["msg"] = lambda **kwargs: recorded_kwargs.update(kwargs)

        msg = Message.objects.create(
            kind="account_msg",
            payload={"pk": target.pk, "kwargs": {"text": "ooc hi"}},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(recorded_kwargs, {"text": "ooc hi"})

    def test_account_msg_target_gone_returns_true_and_inserts_nothing(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="account_msg",
            payload={"pk": 999_999, "kwargs": {"text": "hi"}},
            to_shard="shard0", from_shard="shard1",
        )
        before_count = Message.objects.count()
        self.assertTrue(MessageHandler().handle(msg))
        self.assertEqual(Message.objects.count(), before_count)

    def test_subclass_super_handle_dispatches_obj_msg(self):
        """Consumer subclass calling super().handle() inherits obj_msg dispatch."""
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = ObjectDB.objects.create(
            db_key="char", db_typeclass_path=TYPECLASS,
        )
        captured = []
        target.__dict__["msg"] = lambda **kwargs: captured.append(kwargs)

        class ConsumerHandler(MessageHandler):
            def handle(self, message):
                if super().handle(message):
                    return True
                if message.kind == "consumer_kind":
                    return True
                return False

        msg = Message.objects.create(
            kind="obj_msg",
            payload={"pk": target.pk, "kwargs": {"text": "via super"}},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(ConsumerHandler().handle(msg))
        self.assertEqual(captured, [{"text": "via super"}])

    # ── flush_from_cache ──────────────────────────────────────────

    def test_flush_from_cache_returns_true(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="flush_from_cache", payload={"pks": []},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))

    def test_flush_from_cache_evicts_cached_pk(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = ObjectDB.objects.create(
            db_key="room", db_typeclass_path=TYPECLASS,
        )
        cache = ObjectDB.__dbclass__.__instance_cache__
        self.assertIn(target.pk, cache)

        msg = Message.objects.create(
            kind="flush_from_cache", payload={"pks": [target.pk]},
            to_shard="shard0", from_shard="shard1",
        )
        MessageHandler().handle(msg)

        self.assertNotIn(target.pk, cache)

    def test_flush_from_cache_uncached_pk_is_noop(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="flush_from_cache", payload={"pks": [12345]},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))

    def test_flush_from_cache_idempotent(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        target = ObjectDB.objects.create(
            db_key="room", db_typeclass_path=TYPECLASS,
        )
        cache = ObjectDB.__dbclass__.__instance_cache__

        msg = Message.objects.create(
            kind="flush_from_cache", payload={"pks": [target.pk]},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))
        self.assertNotIn(target.pk, cache)

        self.assertTrue(MessageHandler().handle(msg))

    def test_flush_from_cache_multiple_pks_each_evicted(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        a = ObjectDB.objects.create(db_key="a", db_typeclass_path=TYPECLASS)
        b = ObjectDB.objects.create(db_key="b", db_typeclass_path=TYPECLASS)
        cache = ObjectDB.__dbclass__.__instance_cache__
        self.assertIn(a.pk, cache)
        self.assertIn(b.pk, cache)

        msg = Message.objects.create(
            kind="flush_from_cache",
            payload={"pks": [a.pk, 999_999, b.pk]},
            to_shard="shard0", from_shard="shard1",
        )
        MessageHandler().handle(msg)

        self.assertNotIn(a.pk, cache)
        self.assertNotIn(b.pk, cache)

    def test_flush_from_cache_empty_pks_is_noop(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="flush_from_cache", payload={"pks": []},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))

    def test_flush_from_cache_missing_pks_key_treated_as_empty(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="flush_from_cache", payload={},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))

    # ── room_msg ──────────────────────────────────────────────────

    def _make_room(self, key="room"):
        return ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS,
        )

    def test_room_msg_calls_msg_contents_with_text(self):
        from unittest import mock

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        room = self._make_room()
        msg = Message.objects.create(
            kind="room_msg",
            payload={"room_pk": room.pk, "text": "ball arrives."},
            to_shard="shard0", from_shard="shard1",
        )

        msg_contents = mock.MagicMock()
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        with mock.patch.object(cached, "msg_contents", msg_contents):
            self.assertTrue(MessageHandler().handle(msg))

        msg_contents.assert_called_once()
        args, kwargs = msg_contents.call_args
        self.assertEqual(args[0], "ball arrives.")
        self.assertEqual(kwargs.get("exclude"), [])
        self.assertIsNone(kwargs.get("from_obj"))

    def test_room_msg_passes_exclude_pks_resolved_to_instances(self):
        from unittest import mock

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        room = self._make_room()
        excluded = ObjectDB.objects.create(
            db_key="ball", db_typeclass_path=TYPECLASS,
        )
        msg = Message.objects.create(
            kind="room_msg",
            payload={
                "room_pk": room.pk,
                "text": "ball arrives.",
                "exclude_pks": [excluded.pk],
            },
            to_shard="shard0", from_shard="shard1",
        )

        msg_contents = mock.MagicMock()
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        with mock.patch.object(cached, "msg_contents", msg_contents):
            self.assertTrue(MessageHandler().handle(msg))

        _, kwargs = msg_contents.call_args
        excluded_passed = kwargs.get("exclude")
        self.assertEqual(len(excluded_passed), 1)
        self.assertEqual(excluded_passed[0].pk, excluded.pk)

    def test_room_msg_skips_stale_exclude_pk_silently(self):
        from unittest import mock

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        room = self._make_room()
        msg = Message.objects.create(
            kind="room_msg",
            payload={
                "room_pk": room.pk,
                "text": "ball arrives.",
                "exclude_pks": [999_999],
            },
            to_shard="shard0", from_shard="shard1",
        )

        msg_contents = mock.MagicMock()
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        with mock.patch.object(cached, "msg_contents", msg_contents):
            self.assertTrue(MessageHandler().handle(msg))

        _, kwargs = msg_contents.call_args
        self.assertEqual(kwargs.get("exclude"), [])

    def test_room_msg_resolves_from_obj_pk(self):
        from unittest import mock

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        room = self._make_room()
        sender = ObjectDB.objects.create(
            db_key="alice", db_typeclass_path=TYPECLASS,
        )
        msg = Message.objects.create(
            kind="room_msg",
            payload={
                "room_pk": room.pk,
                "text": "alice says hi.",
                "from_obj_pk": sender.pk,
            },
            to_shard="shard0", from_shard="shard1",
        )

        msg_contents = mock.MagicMock()
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        with mock.patch.object(cached, "msg_contents", msg_contents):
            self.assertTrue(MessageHandler().handle(msg))

        _, kwargs = msg_contents.call_args
        from_obj_passed = kwargs.get("from_obj")
        self.assertIsNotNone(from_obj_passed)
        self.assertEqual(from_obj_passed.pk, sender.pk)

    def test_room_msg_stale_from_obj_pk_becomes_none(self):
        from unittest import mock

        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        room = self._make_room()
        msg = Message.objects.create(
            kind="room_msg",
            payload={
                "room_pk": room.pk,
                "text": "ball arrives.",
                "from_obj_pk": 999_999,
            },
            to_shard="shard0", from_shard="shard1",
        )

        msg_contents = mock.MagicMock()
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        with mock.patch.object(cached, "msg_contents", msg_contents):
            self.assertTrue(MessageHandler().handle(msg))

        _, kwargs = msg_contents.call_args
        self.assertIsNone(kwargs.get("from_obj"))

    def test_room_msg_missing_room_logs_and_consumes(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="room_msg",
            payload={"room_pk": 999_999, "text": "lost arrival."},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertTrue(MessageHandler().handle(msg))


# ---------------------------------------------------------------------------
# process_inbox + ProcessInboxTimeout — one polling cycle: poll, dispatch,
# delete on success; aged-out unhandled messages → undeliverable_reply.
# ---------------------------------------------------------------------------


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class ProcessInboxTests(BaseEvenniaTestCase):
    """process_inbox runs one polling cycle."""

    def test_handler_truthy_deletes_message(self):
        from evennia_shards import MessageHandler, process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        self.assertEqual(process_inbox(AlwaysHandle()), 1)
        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())

    def test_handler_falsy_leaves_message(self):
        from evennia_shards import MessageHandler, process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class NeverHandle(MessageHandler):
            def handle(self, message):
                return False

        self.assertEqual(process_inbox(NeverHandle()), 0)
        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())

    def test_handler_exception_leaves_message(self):
        from evennia_shards import MessageHandler, process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        class BrokenHandler(MessageHandler):
            def handle(self, message):
                raise RuntimeError("oops")

        self.assertEqual(process_inbox(BrokenHandler()), 0)
        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())

    def test_default_handler_processes_ping(self):
        from evennia_shards import process_inbox
        from evennia_shards.models import Message

        Message.objects.create(
            kind="ping", payload={"text": "hi"},
            to_shard="shard0", from_shard="shard1",
        )
        self.assertEqual(process_inbox(), 1)
        self.assertFalse(Message.objects.filter(kind="ping").exists())
        self.assertEqual(
            Message.objects.filter(
                kind="ping_received", to_shard="shard1",
            ).count(),
            1,
        )

    def test_skips_messages_for_other_shards(self):
        from evennia_shards import MessageHandler, process_inbox
        from evennia_shards.models import Message

        Message.objects.create(
            kind="custom", payload={}, to_shard="shard9", from_shard="shard1",
        )

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        self.assertEqual(process_inbox(AlwaysHandle()), 0)
        self.assertEqual(Message.objects.count(), 1)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class ProcessInboxTimeoutTests(BaseEvenniaTestCase):
    """Aged-out unhandled messages produce undeliverable_reply and are deleted."""

    def _age_message(self, msg, seconds):
        # auto_now_add doesn't honour save()-time mutations; force the
        # value via QuerySet.update.
        from datetime import timedelta

        from django.utils import timezone

        from evennia_shards.models import Message

        Message.objects.filter(pk=msg.pk).update(
            created_at=timezone.now() - timedelta(seconds=seconds),
        )

    def test_aged_out_message_with_valid_from_shard_inserts_undeliverable_reply(self):
        from evennia_shards import process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={"data": 1},
            to_shard="shard0", from_shard="shard1",
        )
        self._age_message(msg, seconds=100)

        process_inbox()

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        replies = list(Message.objects.filter(kind="undeliverable_reply"))
        self.assertEqual(len(replies), 1)
        reply = replies[0]
        self.assertEqual(reply.to_shard, "shard1")
        self.assertEqual(reply.from_shard, "shard0")
        self.assertEqual(reply.payload["original_kind"], "custom")
        self.assertEqual(reply.payload["original_payload"], {"data": 1})
        self.assertEqual(reply.payload["reason"], "timeout")

    def test_aged_out_message_without_from_shard_just_deletes(self):
        from evennia_shards import process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard=None,
        )
        self._age_message(msg, seconds=100)

        process_inbox()

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_non_aged_message_stays_in_queue(self):
        from evennia_shards import process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={}, to_shard="shard0", from_shard="shard1",
        )

        process_inbox()

        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_undeliverable_reply_kind_consumed_silently_by_base_handler(self):
        from evennia_shards import MessageHandler
        from evennia_shards.models import Message

        Message.objects.create(
            kind="undeliverable_reply",
            payload={"original_kind": "x", "original_payload": {}, "reason": "timeout"},
            to_shard="shard0", from_shard="shard1",
        )
        result = MessageHandler().handle(Message.objects.first())
        self.assertTrue(result)
        self.assertEqual(Message.objects.count(), 1)

    @override_settings(SHARDS_MESSAGE_TIMEOUTS={"custom": 60})
    def test_per_kind_lifespan_override_is_respected(self):
        from evennia_shards import process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={},
            to_shard="shard0", from_shard="shard1",
        )
        self._age_message(msg, seconds=30)

        process_inbox()

        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())

    def test_handler_truthy_short_circuits_timeout_check(self):
        from evennia_shards import MessageHandler, process_inbox
        from evennia_shards.models import Message

        msg = Message.objects.create(
            kind="custom", payload={},
            to_shard="shard0", from_shard="shard1",
        )
        self._age_message(msg, seconds=100)

        class AlwaysHandle(MessageHandler):
            def handle(self, message):
                return True

        process_inbox(AlwaysHandle())

        self.assertFalse(Message.objects.filter(pk=msg.pk).exists())
        self.assertFalse(Message.objects.filter(kind="undeliverable_reply").exists())


# ---------------------------------------------------------------------------
# Ticket model + create_ticket / get_ticket / delete_ticket primitives.
#
# Ticket has its own table (not ObjectDB), so the multitenant tenancy
# install doesn't affect these — tests port verbatim from the legacy suite.
# ---------------------------------------------------------------------------


class TicketModelTests(BaseEvenniaTestCase):
    """The Ticket model is wired and the migration deploys."""

    def test_table_name_is_namespaced(self):
        from evennia_shards.models import Ticket

        self.assertEqual(Ticket._meta.db_table, "evennia_shards_ticket")

    def test_token_is_primary_key(self):
        from evennia_shards.models import Ticket

        field = Ticket._meta.get_field("token")
        self.assertTrue(field.primary_key)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class CreateTicketTests(BaseEvenniaTestCase):
    """create_ticket inserts a Ticket row and returns a token."""

    def test_returns_token_string(self):
        from evennia_shards import create_ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 32)  # uuid4().hex is 32 chars

    def test_inserts_ticket_row(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        ticket = Ticket.objects.get(token=token)
        self.assertEqual(ticket.account_id, 1)
        self.assertEqual(ticket.character_id, 2)
        self.assertEqual(ticket.to_shard, "shard0")

    def test_each_call_produces_unique_token(self):
        from evennia_shards import create_ticket

        t1 = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        t2 = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        self.assertNotEqual(t1, t2)

    def test_client_ip_stored_when_provided(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="192.168.1.42",
        )
        ticket = Ticket.objects.get(token=token)
        self.assertEqual(ticket.client_ip, "192.168.1.42")

    def test_client_ip_defaults_to_none(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        ticket = Ticket.objects.get(token=token)
        self.assertIsNone(ticket.client_ip)


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class GetTicketTests(BaseEvenniaTestCase):
    """get_ticket looks up a ticket by token with shard check."""

    def test_valid_token_returns_true_and_data(self):
        from evennia_shards import create_ticket, get_ticket

        token = create_ticket(account_id=10, character_id=20, to_shard="shard0")
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertEqual(data["account_id"], 10)
        self.assertEqual(data["character_id"], 20)
        self.assertEqual(data["to_shard"], "shard0")

    def test_invalid_token_returns_false(self):
        from evennia_shards import get_ticket

        found, data = get_ticket("nonexistent")
        self.assertFalse(found)
        self.assertIsNone(data)

    def test_wrong_shard_returns_false(self):
        from evennia_shards import create_ticket, get_ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard1")
        found, data = get_ticket(token, shard_id="shard0")
        self.assertFalse(found)
        self.assertIsNone(data)

    def test_returns_client_ip(self):
        from evennia_shards import create_ticket, get_ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertEqual(data["client_ip"], "10.0.0.1")

    def test_returns_none_client_ip_when_not_set(self):
        from evennia_shards import create_ticket, get_ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        found, data = get_ticket(token)
        self.assertTrue(found)
        self.assertIsNone(data["client_ip"])

    def test_does_not_delete_ticket(self):
        from evennia_shards import create_ticket, get_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        get_ticket(token)
        self.assertTrue(Ticket.objects.filter(token=token).exists())


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class DeleteTicketTests(BaseEvenniaTestCase):
    """delete_ticket removes a ticket by token."""

    def test_deletes_existing_ticket(self):
        from evennia_shards import create_ticket, delete_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        delete_ticket(token)
        self.assertFalse(Ticket.objects.filter(token=token).exists())

    def test_silent_on_nonexistent_token(self):
        from evennia_shards import delete_ticket

        delete_ticket("nonexistent")  # Should not raise

    def test_second_get_after_delete_returns_false(self):
        from evennia_shards import create_ticket, delete_ticket, get_ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        delete_ticket(token)
        found, data = get_ticket(token)
        self.assertFalse(found)
        self.assertIsNone(data)


# ---------------------------------------------------------------------------
# Protocol-side ticket parsing/validation helpers
# (ShardWebSocketClient._extract_ticket_token, ._validate_ticket, ._get_client_address).
# Tests bind the unbound methods onto a _FakeProtocol to avoid standing
# up Twisted reactor + Autobahn.
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Minimal stand-in for a Twisted transport."""

    def __init__(self, client=None):
        self.client = client  # tuple: (ip, port) or None


class _FakeProtocol:
    """Stand-in for ShardWebSocketClient.

    Provides the attributes ``_extract_ticket_token``, ``_validate_ticket``,
    and ``_get_client_address`` rely on. The protocol methods themselves
    get bound onto the class below the protocol's own definition.
    """

    def __init__(self, uri=None, client_ip=None, http_headers=None):
        self.http_request_uri = uri
        self.transport = _FakeTransport(
            client=(client_ip, 0) if client_ip else None
        )
        self.http_headers = http_headers or {}
        self.sent_lines = []
        self.close_calls = []
        self.protocol_flags = {}

    def sendLine(self, data):
        self.sent_lines.append(data)

    def sendClose(self, code=None, reason=None):
        self.close_calls.append((code, reason))


# Bind the unbound methods onto _FakeProtocol so tests can call them
# without instantiating the real ShardWebSocketClient.
from evennia_shards.protocols import ShardWebSocketClient as _SWC

_FakeProtocol._extract_ticket_token = _SWC._extract_ticket_token
_FakeProtocol._validate_ticket = _SWC._validate_ticket
_FakeProtocol._get_client_address = _SWC._get_client_address
_FakeProtocol._send_text = _SWC._send_text
_FakeProtocol._mark_ooc_arrival_if_router = _SWC._mark_ooc_arrival_if_router


class ExtractTicketTokenTests(BaseEvenniaTestCase):
    """_extract_ticket_token parses ?ticket= from the WebSocket URL."""

    def test_extracts_token_from_query_string(self):
        proto = _FakeProtocol(uri="/websocket?ticket=abc123")
        self.assertEqual(proto._extract_ticket_token(), "abc123")

    def test_returns_none_when_no_ticket_param(self):
        proto = _FakeProtocol(uri="/websocket?csessid=xyz")
        self.assertIsNone(proto._extract_ticket_token())

    def test_returns_none_when_no_uri(self):
        proto = _FakeProtocol(uri=None)
        self.assertIsNone(proto._extract_ticket_token())

    def test_returns_first_token_when_multiple(self):
        proto = _FakeProtocol(uri="/websocket?ticket=first&ticket=second")
        self.assertEqual(proto._extract_ticket_token(), "first")

    def test_handles_full_url(self):
        proto = _FakeProtocol(
            uri="ws://shard0:4002/websocket?ticket=tok123&csessid=abc"
        )
        self.assertEqual(proto._extract_ticket_token(), "tok123")


class GetClientAddressTests(BaseEvenniaTestCase):
    """_get_client_address resolves the real client IP."""

    def test_direct_connection_returns_transport_ip(self):
        proto = _FakeProtocol(client_ip="192.168.1.10")
        self.assertEqual(proto._get_client_address(), "192.168.1.10")

    def test_no_transport_client_returns_none(self):
        proto = _FakeProtocol(client_ip=None)
        self.assertIsNone(proto._get_client_address())

    def test_proxy_returns_forwarded_ip(self):
        proto = _FakeProtocol(
            client_ip="127.0.0.1",
            http_headers={"x-forwarded-for": "10.0.0.5, 127.0.0.1"},
        )
        # _UPSTREAM_IPS is a module-level constant; patch in-place.
        import evennia_shards.protocols as proto_mod

        original = proto_mod._UPSTREAM_IPS
        proto_mod._UPSTREAM_IPS = ["127.0.0.1"]
        try:
            self.assertEqual(proto._get_client_address(), "10.0.0.5")
        finally:
            proto_mod._UPSTREAM_IPS = original

    def test_non_proxy_ip_ignores_forwarded_header(self):
        proto = _FakeProtocol(
            client_ip="10.0.0.1",
            http_headers={"x-forwarded-for": "evil.ip"},
        )
        self.assertEqual(proto._get_client_address(), "10.0.0.1")


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class ValidateTicketTests(BaseEvenniaTestCase):
    """_validate_ticket validates, IP-checks, and consumes the ticket."""

    def test_valid_ticket_returns_true_and_data(self):
        from evennia_shards import create_ticket

        token = create_ticket(account_id=10, character_id=20, to_shard="shard0")
        proto = _FakeProtocol()
        valid, data = proto._validate_ticket(token, "127.0.0.1")
        self.assertTrue(valid)
        self.assertEqual(data["account_id"], 10)
        self.assertEqual(data["character_id"], 20)

    def test_valid_ticket_is_consumed(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket(token, "127.0.0.1")
        self.assertFalse(Ticket.objects.filter(token=token).exists())

    def test_second_use_of_same_token_rejected(self):
        from evennia_shards import create_ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket(token, "127.0.0.1")
        valid, error = proto._validate_ticket(token, "127.0.0.1")
        self.assertFalse(valid)
        self.assertIn("not found", error)

    def test_invalid_token_returns_false_and_error(self):
        proto = _FakeProtocol()
        valid, error = proto._validate_ticket("nonexistent", "127.0.0.1")
        self.assertFalse(valid)
        self.assertIn("not found", error)

    def test_invalid_token_does_not_delete_anything(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(account_id=1, character_id=2, to_shard="shard0")
        proto = _FakeProtocol()
        proto._validate_ticket("wrong_token", "127.0.0.1")
        self.assertTrue(Ticket.objects.filter(token=token).exists())

    def test_ip_match_passes(self):
        from evennia_shards import create_ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        valid, _ = proto._validate_ticket(token, "10.0.0.1")
        self.assertTrue(valid)

    def test_ip_mismatch_rejected(self):
        from evennia_shards import create_ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        valid, error = proto._validate_ticket(token, "192.168.1.99")
        self.assertFalse(valid)
        self.assertIn("IP mismatch", error)

    def test_ip_mismatch_does_not_consume_ticket(self):
        from evennia_shards import create_ticket
        from evennia_shards.models import Ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
            client_ip="10.0.0.1",
        )
        proto = _FakeProtocol()
        proto._validate_ticket(token, "192.168.1.99")
        self.assertTrue(Ticket.objects.filter(token=token).exists())

    def test_no_ip_on_ticket_skips_ip_check(self):
        from evennia_shards import create_ticket

        token = create_ticket(
            account_id=1, character_id=2, to_shard="shard0",
        )
        proto = _FakeProtocol()
        valid, _ = proto._validate_ticket(token, "192.168.1.99")
        self.assertTrue(valid)


# ---------------------------------------------------------------------------
# Cross-shard messaging helpers (send_cross_shard_message,
# send_cross_shard_room_message). Sender-side wrappers on top of the obj_msg
# and room_msg bus primitives, with local-vs-remote dispatch.
#
# Under multitenant the helpers escape the auto-filter for the initial
# visibility lookup (same pattern as search.py) so foreign-shard targets
# are visible to the dispatch logic. The tests below exercise both the
# local fast-path (auto-filtered .get) and the remote bus path.
# ---------------------------------------------------------------------------


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class SendCrossShardMessageTests(BaseEvenniaTestCase):
    """Sender-side helper built on top of the ``obj_msg`` primitive."""

    def _make_target(self, shard_id="shard0", typeclass=TYPECLASS):
        target = ObjectDB.objects.create(
            db_key="target", db_typeclass_path=typeclass,
        )
        if shard_id != "shard0":
            _forge_db_shard(target.pk, shard_id)
        return target

    def test_local_target_calls_msg_directly_no_bus_row(self):
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        target = self._make_target(shard_id="shard0")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "local hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [{"text": "local hi"}])
        self.assertEqual(Message.objects.count(), 0)

    def test_global_star_target_treated_as_local(self):
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        target = self._make_target(shard_id="*")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "global hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [{"text": "global hi"}])
        self.assertEqual(Message.objects.count(), 0)

    def test_remote_target_inserts_obj_msg_bus_row(self):
        """Target on another shard → obj_msg bus row, no local .msg call.

        Validates the multitenant fix: the helper's visibility lookup
        wraps its values_list in ``shard_context(None)`` so the foreign
        row is visible to the dispatch decision.
        """
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        target = self._make_target(shard_id="shard1")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        result = send_cross_shard_message(
            target.pk, {"text": "remote hi"}, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [])
        msgs = list(Message.objects.all())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].kind, "obj_msg")
        self.assertEqual(msgs[0].to_shard, "shard1")
        self.assertEqual(msgs[0].from_shard, "shard0")
        self.assertEqual(
            msgs[0].payload, {"pk": target.pk, "kwargs": {"text": "remote hi"}}
        )

    def test_target_gone_returns_false_no_bus_row(self):
        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        result = send_cross_shard_message(999_999, {"text": "ghost"})

        self.assertFalse(result)
        self.assertEqual(Message.objects.count(), 0)

    def test_typeclass_mismatch_returns_false_no_bus_row(self):
        from evennia.objects.objects import DefaultCharacter

        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        # DefaultObject is NOT a subclass of DefaultCharacter (DC inherits
        # from DO, not the other way around). Filter rejects.
        target = self._make_target(
            shard_id="shard1",
            typeclass="evennia.objects.objects.DefaultObject",
        )

        result = send_cross_shard_message(
            target.pk, {"text": "hi"}, target_typeclass=DefaultCharacter,
        )

        self.assertFalse(result)
        self.assertEqual(Message.objects.count(), 0)

    @override_settings(
        SHARD_ID="shard0",
        SHARDS_ROLE="shard",
        BASE_CHARACTER_TYPECLASS="evennia.objects.objects.DefaultObject",
    )
    def test_default_typeclass_resolves_from_settings_at_call_time(self):
        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        target = self._make_target(shard_id="shard1")

        result = send_cross_shard_message(target.pk, {"text": "hi"})

        self.assertTrue(result)
        self.assertEqual(Message.objects.count(), 1)

    def test_kwargs_pass_through_to_remote_payload(self):
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message
        from evennia_shards.models import Message

        target = self._make_target(shard_id="shard1")

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1", "ticket": "abc"},
            "options": {"raw": True},
        }
        result = send_cross_shard_message(
            target.pk, kwargs, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        msg = Message.objects.first()
        self.assertEqual(msg.payload["kwargs"], kwargs)

    def test_kwargs_pass_through_to_local_msg(self):
        from evennia.objects.objects import DefaultObject

        from evennia_shards import send_cross_shard_message

        target = self._make_target(shard_id="shard0")
        captured = []
        target.__dict__["msg"] = lambda **kw: captured.append(kw)

        kwargs = {
            "text": "look",
            "shard_redirect": {"host": "shard1"},
            "options": {"raw": True},
        }
        result = send_cross_shard_message(
            target.pk, kwargs, target_typeclass=DefaultObject,
        )

        self.assertTrue(result)
        self.assertEqual(captured, [kwargs])


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class SendCrossShardRoomMessageTests(BaseEvenniaTestCase):
    """Sender-side helper built on top of the ``room_msg`` primitive."""

    def _make_room(self, shard_id="shard0", key="room"):
        target = ObjectDB.objects.create(
            db_key=key, db_typeclass_path=TYPECLASS,
        )
        if shard_id != "shard0":
            _forge_db_shard(target.pk, shard_id)
        return target

    def test_local_room_calls_msg_contents_directly_no_bus_row(self):
        from unittest import mock

        from evennia_shards.messaging import send_cross_shard_room_message
        from evennia_shards.models import Message

        room = self._make_room(shard_id="shard0")
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        spy = mock.MagicMock()

        with mock.patch.object(cached, "msg_contents", spy):
            result = send_cross_shard_room_message(room.pk, "ball arrives.")

        self.assertTrue(result)
        spy.assert_called_once()
        args, kwargs = spy.call_args
        self.assertEqual(args[0], "ball arrives.")
        self.assertEqual(kwargs.get("exclude"), [])
        self.assertIsNone(kwargs.get("from_obj"))
        self.assertEqual(Message.objects.count(), 0)

    def test_remote_room_inserts_bus_row_with_payload(self):
        """Room on another shard → room_msg bus row addressed to that shard.

        Validates the multitenant fix on the room helper.
        """
        from evennia_shards.messaging import send_cross_shard_room_message
        from evennia_shards.models import Message

        room = self._make_room(shard_id="shard1")
        excluded = ObjectDB.objects.create(
            db_key="ball", db_typeclass_path=TYPECLASS,
        )

        result = send_cross_shard_room_message(
            room.pk,
            "ball arrives.",
            exclude_pks=[excluded.pk],
            from_obj_pk=excluded.pk,
        )

        self.assertTrue(result)
        rows = list(Message.objects.filter(kind="room_msg"))
        self.assertEqual(len(rows), 1)
        msg = rows[0]
        self.assertEqual(msg.to_shard, "shard1")
        self.assertEqual(msg.payload["room_pk"], room.pk)
        self.assertEqual(msg.payload["text"], "ball arrives.")
        self.assertEqual(msg.payload["exclude_pks"], [excluded.pk])
        self.assertEqual(msg.payload["from_obj_pk"], excluded.pk)

    def test_remote_room_omits_optional_fields_when_unused(self):
        from evennia_shards.messaging import send_cross_shard_room_message
        from evennia_shards.models import Message

        room = self._make_room(shard_id="shard1")
        send_cross_shard_room_message(room.pk, "ball arrives.")

        msg = Message.objects.get(kind="room_msg")
        self.assertNotIn("exclude_pks", msg.payload)
        self.assertNotIn("from_obj_pk", msg.payload)

    def test_global_sentinel_room_is_local(self):
        from unittest import mock

        from evennia_shards.messaging import send_cross_shard_room_message
        from evennia_shards.models import Message

        room = self._make_room(shard_id="*")
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        spy = mock.MagicMock()

        with mock.patch.object(cached, "msg_contents", spy):
            self.assertTrue(send_cross_shard_room_message(room.pk, "hi."))

        spy.assert_called_once()
        self.assertEqual(Message.objects.count(), 0)

    def test_missing_room_logs_and_returns_false(self):
        from evennia_shards.messaging import send_cross_shard_room_message
        from evennia_shards.models import Message

        result = send_cross_shard_room_message(999_999, "ghost.")

        self.assertFalse(result)
        self.assertEqual(Message.objects.count(), 0)

    def test_local_path_resolves_optional_pks_to_instances(self):
        from unittest import mock

        from evennia_shards.messaging import send_cross_shard_room_message

        room = self._make_room(shard_id="shard0")
        excluded = ObjectDB.objects.create(
            db_key="ball", db_typeclass_path=TYPECLASS,
        )
        sender = ObjectDB.objects.create(
            db_key="alice", db_typeclass_path=TYPECLASS,
        )
        cached = ObjectDB.__dbclass__.__instance_cache__.get(room.pk)
        spy = mock.MagicMock()

        with mock.patch.object(cached, "msg_contents", spy):
            send_cross_shard_room_message(
                room.pk, "hi.",
                exclude_pks=[excluded.pk],
                from_obj_pk=sender.pk,
            )

        _, kwargs = spy.call_args
        ex_passed = kwargs.get("exclude")
        self.assertEqual(len(ex_passed), 1)
        self.assertEqual(ex_passed[0].pk, excluded.pk)
        from_obj_passed = kwargs.get("from_obj")
        self.assertEqual(from_obj_passed.pk, sender.pk)


# ---------------------------------------------------------------------------
# ShardAwareCmdTeleport — shard-aware @tel override
#
# Two test groups: Func dispatch (branch routing into /tonone /
# both-local-vanilla / cross-shard), and Parse (shard_aware_global_search
# result stashing). Most tests mock the search and the cross_shard_move
# primitive so dispatch logic is tested in isolation from the DB.
# ---------------------------------------------------------------------------


def _make_teleport_cmd(args="", switches=None, caller=None):
    """Build a ShardAwareCmdTeleport instance wired up for testing.

    Bypasses ``parse()`` setup — caller is responsible for setting the
    post-parse state slots (``obj_to_teleport``, ``destination``,
    ``obj_pk`` / ``obj_shard``, ``dest_pk`` / ``dest_shard`` /
    ``dest_key``) before calling ``func()``. The few tests that
    exercise ``parse()`` itself set ``self.args`` and let the real
    parse run.
    """
    from evennia_shards.teleport import ShardAwareCmdTeleport

    cmd = ShardAwareCmdTeleport()
    cmd.args = args
    cmd.raw_string = f"@tel {args}"
    cmd.cmdstring = "@tel"
    cmd.switches = switches or []
    cmd.lhs = ""
    cmd.rhs = None
    cmd.caller = caller if caller is not None else _FakeCaller()
    cmd._messages = []
    cmd.msg = lambda text=None, **kwargs: (
        cmd._messages.append(text) if text is not None else None
    )
    return cmd


def _vanilla_cmd_teleport_cls():
    """Resolve the vanilla CmdTeleport class behind the library swap.

    ``apps.py`` swaps ``evennia.commands.default.building.CmdTeleport``
    to our subclass, so patching the dotted path patches the subclass —
    not what ``super().func()`` reaches. The unswapped parent is the
    subclass's first base, accessible via ``__bases__[0]``.
    """
    from evennia_shards.teleport import ShardAwareCmdTeleport

    return ShardAwareCmdTeleport.__bases__[0]


@override_settings(
    SHARDS_ROLE="shard", SHARD_ID="shard0",
    SHARD_URLS={"shard0": "ws://localhost:4011/", "shard1": "ws://localhost:4021/"},
)
class ShardAwareCmdTeleportFuncDispatchTests(BaseEvenniaTestCase):
    """``ShardAwareCmdTeleport.func`` routes into three branches:
    /tonone, both-local-delegate-to-vanilla, cross-shard-route.
    """

    # ── Branch 1: /tonone ─────────────────────────────────────────

    def test_tonone_local_obj_delegates_to_vanilla(self):
        from unittest import mock

        cmd = _make_teleport_cmd(switches=["tonone"])
        cmd.obj_to_teleport = cmd.caller  # local
        cmd.lhs = "ball"

        with mock.patch.object(
            _vanilla_cmd_teleport_cls(), "func",
        ) as vanilla_func:
            cmd.func()

        vanilla_func.assert_called_once()
        self.assertEqual(cmd.caller.messages, [])

    def test_tonone_foreign_obj_refuses_with_cross_shard_pointer(self):
        from unittest import mock

        cmd = _make_teleport_cmd(switches=["tonone"])
        cmd.obj_to_teleport = None  # foreign
        cmd.obj_pk = 42
        cmd.obj_shard = "shard1"
        cmd.lhs = "ball"

        with mock.patch.object(
            _vanilla_cmd_teleport_cls(), "func",
        ) as vanilla_func:
            cmd.func()

        vanilla_func.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("shard1", msg)
        self.assertIn("@tel", msg)
        self.assertIn("not yet implemented", msg)

    # ── Branch 2: both-local delegate ─────────────────────────────

    def test_both_targets_local_delegates_to_vanilla(self):
        from unittest import mock

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = cmd.caller

        with mock.patch.object(
            _vanilla_cmd_teleport_cls(), "func",
        ) as vanilla_func:
            cmd.func()

        vanilla_func.assert_called_once()

    # ── Branch 3: cross-shard ─────────────────────────────────────

    def test_foreign_obj_refuses(self):
        from unittest import mock

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = None  # foreign
        cmd.obj_pk = 42
        cmd.obj_shard = "shard1"
        cmd.destination = cmd.caller
        cmd.lhs = "remote_obj"
        cmd.rhs = "anywhere"

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("shard1", msg)
        self.assertIn("@tel", msg)
        self.assertIn("not yet implemented", msg)

    def test_local_obj_foreign_dest_calls_cross_shard_move(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = None  # foreign
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=1, failures=[],
            ),
        ) as primitive:
            cmd.func()

        primitive.assert_called_once_with(cmd.caller, "shard1", 99)
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("newroom", msg)
        self.assertIn("shard1", msg)

    def test_already_at_cross_shard_destination_short_circuits(self):
        from unittest import mock

        class _StubObj:
            def __init__(self):
                self.db_location_id = 99
                self.pk = 5

            def __str__(self):
                return "ball"

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = _StubObj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("already at", msg)
        self.assertIn("newroom", msg)

    def test_already_at_falls_back_to_dbref_when_dest_key_missing(self):
        from unittest import mock

        class _StubObj:
            def __init__(self):
                self.db_location_id = 99
                self.pk = 5

            def __str__(self):
                return "ball"

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = _StubObj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = None

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("#99", msg)

    def test_teleport_lock_blocks_non_admin_caller(self):
        from unittest import mock

        caller = _FakeCaller(is_admin=False)

        class _LockedObj:
            db_location_id = None
            pk = 5

            def access(self, accessing_obj, access_type, default=False):
                return False

            def __str__(self):
                return "ball"

        cmd = _make_teleport_cmd(caller=caller)
        cmd.obj_to_teleport = _LockedObj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(caller.messages)
        self.assertIn("'teleport'-lock", msg)
        self.assertIn("ball", msg)

    def test_teleport_lock_bypassed_by_admin_caller(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        caller = _FakeCaller(is_admin=True)

        class _LockedObj:
            db_location_id = None
            pk = 5
            key = "ball"
            location = None

            def access(self, accessing_obj, access_type, default=False):
                return False  # lock denies, but Admin bypasses

            def __str__(self):
                return "ball"

        cmd = _make_teleport_cmd(caller=caller)
        cmd.obj_to_teleport = _LockedObj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ) as primitive:
            cmd.func()

        primitive.assert_called_once_with(cmd.obj_to_teleport, "shard1", 99)

    def test_teleport_lock_allows_non_admin_when_obj_grants_access(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        caller = _FakeCaller(is_admin=False)

        class _PermissiveObj:
            db_location_id = None
            pk = 5
            key = "ball"
            location = None

            def access(self, accessing_obj, access_type, default=False):
                return True

            def __str__(self):
                return "ball"

        cmd = _make_teleport_cmd(caller=caller)
        cmd.obj_to_teleport = _PermissiveObj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ) as primitive:
            cmd.func()

        primitive.assert_called_once()

    def test_local_obj_foreign_dest_failure_emits_error(self):
        from unittest import mock

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            side_effect=RuntimeError("simulated"),
        ):
            cmd.func()

        msg = "\n".join(cmd.caller.messages)
        self.assertIn("failed", msg.lower())
        self.assertIn("simulated", msg)

    def test_loc_with_cross_shard_dest_refuses(self):
        from unittest import mock

        cmd = _make_teleport_cmd(switches=["loc"])
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("/loc", msg)
        self.assertIn("not yet supported", msg)

    def test_intoexit_with_cross_shard_dest_refuses(self):
        from unittest import mock

        cmd = _make_teleport_cmd(switches=["intoexit"])
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"

        with mock.patch("evennia_shards.handoff.cross_shard_move") as primitive:
            cmd.func()

        primitive.assert_not_called()
        msg = "\n".join(cmd.caller.messages)
        self.assertIn("/intoexit", msg)
        self.assertIn("not yet supported", msg)

    def test_quiet_does_not_suppress_caller_confirmation(self):
        """/quiet must NOT silence the caller's own confirmation.

        Vanilla CmdTeleport.func emits "Teleported X -> Y."
        unconditionally; /quiet only suppresses the source/destination
        room announces.
        """
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        cmd = _make_teleport_cmd(switches=["quiet"])
        cmd.obj_to_teleport = cmd.caller
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=1, failures=[],
            ),
        ):
            cmd.func()

        msg = "\n".join(cmd.caller.messages)
        self.assertIn("Teleported", msg)
        self.assertIn("newroom", msg)

    # ── leave/arrive announces ────────────────────────────────────

    def test_source_announce_fires_before_cross_shard_move(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        source = mock.MagicMock()
        source.key = "Limbo"

        class _Obj:
            db_location_id = 2  # different from dest_pk
            pk = 5
            key = "ball"
            location = source

            def access(self, *a, **kw):
                return True

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = _Obj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ):
            with mock.patch(
                "evennia_shards.messaging.send_cross_shard_room_message"
            ):
                cmd.func()

        source.msg_contents.assert_called_once()
        args, kwargs = source.msg_contents.call_args
        text = args[0]
        self.assertIn("ball", text)
        self.assertIn("leaving", text)
        self.assertIn("Limbo", text)
        self.assertIn("newroom", text)
        self.assertEqual(kwargs["exclude"], [cmd.obj_to_teleport])

    def test_quiet_suppresses_source_announce(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        source = mock.MagicMock()
        source.key = "Limbo"

        class _Obj:
            db_location_id = 2
            pk = 5
            key = "ball"
            location = source

            def access(self, *a, **kw):
                return True

        cmd = _make_teleport_cmd(switches=["quiet"])
        cmd.obj_to_teleport = _Obj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ):
            with mock.patch(
                "evennia_shards.messaging.send_cross_shard_room_message"
            ):
                cmd.func()

        source.msg_contents.assert_not_called()

    def test_no_source_announce_when_obj_has_no_location(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        class _Obj:
            db_location_id = None
            pk = 5
            key = "ball"
            location = None

            def access(self, *a, **kw):
                return True

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = _Obj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ):
            with mock.patch(
                "evennia_shards.messaging.send_cross_shard_room_message"
            ) as send_room:
                cmd.func()

        send_room.assert_called_once()

    def test_destination_announce_routes_via_bus_helper(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        source = mock.MagicMock()
        source.key = "Limbo"

        class _Obj:
            db_location_id = 2
            pk = 5
            key = "ball"
            location = source

            def access(self, *a, **kw):
                return True

        cmd = _make_teleport_cmd()
        cmd.obj_to_teleport = _Obj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ):
            with mock.patch(
                "evennia_shards.messaging.send_cross_shard_room_message"
            ) as send_room:
                cmd.func()

        send_room.assert_called_once()
        args, kwargs = send_room.call_args
        self.assertEqual(args[0], 99)
        text = args[1]
        self.assertIn("ball", text)
        self.assertIn("arrives", text)
        self.assertIn("Limbo", text)
        self.assertEqual(kwargs.get("exclude_pks"), [5])

    def test_quiet_suppresses_destination_announce(self):
        from unittest import mock

        from evennia_shards.handoff import MoveResult

        class _Obj:
            db_location_id = 2
            pk = 5
            key = "ball"
            location = None

            def access(self, *a, **kw):
                return True

        cmd = _make_teleport_cmd(switches=["quiet"])
        cmd.obj_to_teleport = _Obj()
        cmd.destination = None
        cmd.dest_pk = 99
        cmd.dest_shard = "shard1"
        cmd.dest_key = "newroom"

        with mock.patch(
            "evennia_shards.handoff.cross_shard_move",
            return_value=MoveResult(
                objects_moved=1, sessions_redirected=0, failures=[],
            ),
        ):
            with mock.patch(
                "evennia_shards.messaging.send_cross_shard_room_message"
            ) as send_room:
                cmd.func()

        send_room.assert_not_called()


@override_settings(
    SHARDS_ROLE="shard", SHARD_ID="shard0",
    SHARD_URLS={"shard0": "ws://localhost:4011/", "shard1": "ws://localhost:4021/"},
)
class ShardAwareCmdTeleportParseTests(BaseEvenniaTestCase):
    """``ShardAwareCmdTeleport.parse`` stashes search-result state on self.

    Parse calls super(CmdTeleport, self).parse() to split
    args/lhs/rhs/switches, then uses shard_aware_global_search for each
    of vanilla's three call sites. Tests mock the search to control
    state without setting up real ObjectDB rows.
    """

    def _result(self, **kw):
        from evennia_shards.search import ShardSearchResult

        defaults = dict(state="not_found")
        defaults.update(kw)
        return ShardSearchResult(**defaults)

    def test_no_args_leaves_defaults(self):
        cmd = _make_teleport_cmd(args="")
        cmd.parse()

        self.assertIs(cmd.obj_to_teleport, cmd.caller)
        self.assertIsNone(cmd.destination)
        self.assertIsNone(cmd.dest_pk)
        self.assertIsNone(cmd.dest_shard)

    def test_lhs_only_search_populates_destination(self):
        from unittest import mock

        cmd = _make_teleport_cmd(args="newroom")

        fake_dest = object()
        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(
                state="found", obj=fake_dest, pk=99,
                shard_id="shard1", db_key="newroom",
            ),
        ) as search:
            cmd.parse()

        search.assert_called_once_with(cmd.caller, "newroom")
        self.assertIs(cmd.destination, fake_dest)
        self.assertEqual(cmd.dest_pk, 99)
        self.assertEqual(cmd.dest_shard, "shard1")
        self.assertEqual(cmd.dest_key, "newroom")
        self.assertIs(cmd.obj_to_teleport, cmd.caller)

    def test_rhs_present_searches_both_obj_and_dest(self):
        from unittest import mock

        cmd = _make_teleport_cmd(args="ball = newroom")

        fake_obj = object()
        fake_dest = object()

        def search_side_effect(caller, name):
            if name == "ball":
                return self._result(
                    state="found", obj=fake_obj, pk=5,
                    shard_id="shard0", db_key="ball",
                )
            if name == "newroom":
                return self._result(
                    state="found", obj=fake_dest, pk=99,
                    shard_id="shard1", db_key="newroom",
                )
            return self._result(state="not_found")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            side_effect=search_side_effect,
        ):
            cmd.parse()

        self.assertIs(cmd.obj_to_teleport, fake_obj)
        self.assertEqual(cmd.obj_pk, 5)
        self.assertEqual(cmd.obj_shard, "shard0")
        self.assertEqual(cmd.obj_key, "ball")
        self.assertIs(cmd.destination, fake_dest)
        self.assertEqual(cmd.dest_pk, 99)
        self.assertEqual(cmd.dest_shard, "shard1")
        self.assertEqual(cmd.dest_key, "newroom")

    def test_cross_shard_match_stashes_pk_shard_no_instance(self):
        from unittest import mock

        cmd = _make_teleport_cmd(args="newroom")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(
                state="found", obj=None, pk=99,
                shard_id="shard1", db_key="newroom",
            ),
        ):
            cmd.parse()

        self.assertIsNone(cmd.destination)
        self.assertEqual(cmd.dest_pk, 99)
        self.assertEqual(cmd.dest_shard, "shard1")
        self.assertEqual(cmd.dest_key, "newroom")

    def test_obj_not_found_raises_interrupt(self):
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="missing = somewhere")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(state="not_found"),
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Did not find object", msg)

    def test_obj_multiple_matches_raises_interrupt(self):
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="ambiguous = somewhere")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(
                state="multiple",
                candidates=[(1, "shard0", "ambiguous"), (2, "shard1", "ambiguous")],
            ),
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Multiple matches", msg)
        self.assertIn("dbref", msg.lower())
        self.assertIn("#1 (ambiguous, shard0)", msg)
        self.assertIn("#2 (ambiguous, shard1)", msg)

    def test_dest_multiple_matches_in_rhs_path_raises_interrupt(self):
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="ball = tavern")
        fake_obj = object()

        def search_side_effect(caller, name):
            if name == "ball":
                return self._result(
                    state="found", obj=fake_obj, pk=5,
                    shard_id="shard0", db_key="ball",
                )
            return self._result(
                state="multiple",
                candidates=[(7, "shard0", "Tavern"), (8, "shard1", "Tavern")],
            )

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            side_effect=search_side_effect,
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Multiple matches", msg)
        self.assertIn("#7 (Tavern, shard0)", msg)
        self.assertIn("#8 (Tavern, shard1)", msg)

    def test_dest_multiple_matches_in_lhs_only_path_raises_interrupt(self):
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="tavern")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(
                state="multiple",
                candidates=[(7, "shard0", "Tavern"), (8, "shard1", "Tavern")],
            ),
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Multiple matches", msg)
        self.assertIn("#7 (Tavern, shard0)", msg)
        self.assertIn("#8 (Tavern, shard1)", msg)

    def test_dest_not_found_in_rhs_path_raises_interrupt_with_message(self):
        """Destination not_found (with rhs given) → ``Destination not found.``

        Legacy behaviour: parse left ``destination=None`` and trusted
        vanilla ``func()`` to emit the message. Under the trial's
        func() dispatch that path bypasses vanilla whenever
        ``destination is None`` (because the branch routes into
        cross-shard handling), so parse now emits the message itself
        and raises ``InterruptCommand``. Regression cover for the
        bug surfaced during the demo-gamedir smoke test.
        """
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="ball = nowhere")
        fake_obj = object()

        def search_side_effect(caller, name):
            if name == "ball":
                return self._result(
                    state="found", obj=fake_obj, pk=5,
                    shard_id="shard0", db_key="ball",
                )
            return self._result(state="not_found")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            side_effect=search_side_effect,
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Destination not found", msg)

    def test_dest_not_found_in_lhs_only_path_raises_interrupt_with_message(self):
        """Lhs-only @tel <unknown> → ``Destination not found.`` regression.

        Same root cause as the rhs path: previously fell through with
        ``destination=None``, which routed into cross_shard_move with
        ``target_shard=None`` and surfaced as
        ``"target_shard None is not configured"``. Now parse emits
        the right message.
        """
        from unittest import mock

        from evennia.commands.cmdhandler import InterruptCommand

        cmd = _make_teleport_cmd(args="nowhere")

        with mock.patch(
            "evennia_shards.teleport.shard_aware_global_search",
            return_value=self._result(state="not_found"),
        ):
            with self.assertRaises(InterruptCommand):
                cmd.parse()

        msg = "\n".join(cmd._messages)
        self.assertIn("Destination not found", msg)


# ---------------------------------------------------------------------------
# AppConfig setup — shard_id field wired onto ObjectDB
# ---------------------------------------------------------------------------


class AppSetupTests(BaseEvenniaTestCase):
    """AppConfig.ready() → install_tenancy_on_objectdb() wires shard_id."""

    def test_shard_id_field_wired_on_objectdb(self):
        from django.db import models

        field = ObjectDB._meta.get_field("shard_id")
        self.assertIsInstance(field, models.CharField)
        self.assertEqual(field.max_length, 64)
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertTrue(field.db_index)


# ---------------------------------------------------------------------------
# Role gating + portal-side OOC-arrival marker
# ---------------------------------------------------------------------------


@override_settings(SHARD_ID="shard0", SHARDS_ROLE="shard")
class RoleGatingTests(BaseEvenniaTestCase):
    """onOpen Phase 1: role-based gating when no ticket is present.

    Tests the gating *logic* (not the full onOpen — that needs Twisted).
    Shard with no ticket → redirect to router; router → fall through.
    """

    @override_settings(ROUTER_URL="ws://localhost:4002/")
    def test_shard_no_token_redirects_to_router(self):
        import json as _json

        from evennia_shards import get_role, get_router_url

        proto = _FakeProtocol(uri="/websocket", client_ip="127.0.0.1")
        self.assertIsNone(proto._extract_ticket_token())

        self.assertEqual(get_role(), "shard")

        router_url = get_router_url()
        proto.sendLine(_json.dumps(["shard_redirect", [router_url], {}]))
        proto.sendClose(1000, "Redirecting to router for login")

        self.assertEqual(len(proto.sent_lines), 1)
        self.assertIn("shard_redirect", proto.sent_lines[0])
        self.assertIn(router_url, proto.sent_lines[0])
        self.assertEqual(len(proto.close_calls), 1)
        self.assertEqual(proto.close_calls[0][0], 1000)

    @override_settings(SHARDS_ROLE="router")
    def test_router_no_token_proceeds(self):
        from evennia_shards import get_role

        proto = _FakeProtocol(uri="/websocket", client_ip="127.0.0.1")
        self.assertIsNone(proto._extract_ticket_token())
        self.assertEqual(get_role(), "router")
        self.assertEqual(len(proto.sent_lines), 0)
        self.assertEqual(len(proto.close_calls), 0)


class MarkOocArrivalIfRouterTests(BaseEvenniaTestCase):
    """_mark_ooc_arrival_if_router sets a protocol_flag on routers only.

    Runs in the Portal process. Sets
    ``protocol_flags["SHARDS_TICKET_AUTHED"]=True``; Evennia AMP-syncs
    the flag to the Server, where ``shard_aware_at_post_login`` reads
    it. The Portal does NOT write to account.db — that would be a
    cross-process write the Server's idmappers wouldn't see.
    """

    @override_settings(SHARDS_ROLE="router", SHARD_ID="router")
    def test_router_sets_protocol_flag(self):
        proto = _FakeProtocol()
        proto._mark_ooc_arrival_if_router(account_id=1)

        self.assertTrue(proto.protocol_flags.get("SHARDS_TICKET_AUTHED"))

    @override_settings(SHARDS_ROLE="shard", SHARD_ID="shard0")
    def test_shard_does_not_set_protocol_flag(self):
        proto = _FakeProtocol()
        proto._mark_ooc_arrival_if_router(account_id=1)

        self.assertNotIn("SHARDS_TICKET_AUTHED", proto.protocol_flags)

    @override_settings(SHARDS_ROLE="router", SHARD_ID="router")
    def test_router_does_not_touch_account_db(self):
        """Cross-process safety guard — Portal must not write account.db."""
        from evennia.accounts.models import AccountDB

        account = AccountDB.objects.create(
            username="portal_no_account_write",
            db_typeclass_path="evennia.accounts.accounts.DefaultAccount",
        )
        account.db._shards_at_ooc_menu = "untouched-sentinel"
        proto = _FakeProtocol()
        proto._mark_ooc_arrival_if_router(account.pk)

        account.refresh_from_db()
        self.assertEqual(
            account.db._shards_at_ooc_menu, "untouched-sentinel",
        )


# ---------------------------------------------------------------------------
# _redirect_to_character_shard helper (shared by IC + at_post_login paths)
# ---------------------------------------------------------------------------


@override_settings(
    SHARDS_ROLE="router", SHARD_ID="router",
    SHARD_URLS={"shard0": "ws://localhost:4011/"},
)
class RedirectToCharacterShardHelperTests(BaseEvenniaTestCase):
    """Direct tests for ``_redirect_to_character_shard``.

    Shared mechanism between the IC command path and the
    at_post_login override path. Tests cover only the side-effects
    the helper itself performs; validation is the caller's job.
    """

    def test_redirect_creates_ticket_sets_last_puppet_and_sends_oob(self):
        from evennia_shards.handoff import _redirect_to_character_shard
        from evennia_shards.models import Ticket

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        session = _FakeSession(address="10.0.0.1")

        url = _redirect_to_character_shard(account, session, char)

        self.assertIs(account.db._last_puppet, char)

        tickets = list(Ticket.objects.all())
        self.assertEqual(len(tickets), 1)
        ticket = tickets[0]
        self.assertEqual(ticket.account_id, 7)
        self.assertEqual(ticket.character_id, 42)
        self.assertEqual(ticket.to_shard, "shard0")
        self.assertEqual(ticket.client_ip, "10.0.0.1")

        self.assertIn("shard_redirect", session.oob_messages)
        oob_url = session.oob_messages["shard_redirect"][0][0]
        self.assertIn("ws://localhost:4011/?ticket=", oob_url)
        self.assertIn(ticket.token, oob_url)

        self.assertEqual(url, oob_url)

    def test_redirect_does_not_touch_at_ooc_menu_flag(self):
        """The helper must NOT touch ``account.db._shards_at_ooc_menu``.

        That flag is owned by the router's Server process and is
        written there in two places only — the at_post_login override
        on fresh ticket auth (True), and CmdIC on @ic (False).
        Touching it from this helper would create a cross-process
        write the router wouldn't see.
        """
        from evennia_shards.handoff import _redirect_to_character_shard

        char = _FakeCharacter("Bob", pk=42, shard_id="shard0")
        account = _FakeAccount(pk=7)
        account.db._shards_at_ooc_menu = "untouched-sentinel"
        session = _FakeSession(address="10.0.0.1")

        _redirect_to_character_shard(account, session, char)

        self.assertEqual(account.db._shards_at_ooc_menu, "untouched-sentinel")


# ---------------------------------------------------------------------------
# Portal services plugin (start_plugin_services) — registers the
# webclient WebSocket independently when WEBSERVER_ENABLED=False.
# ---------------------------------------------------------------------------


class _FakePortalService:
    """Minimal stand-in for Evennia's PortalServerFactory."""

    def __init__(self):
        self.children = []
        self.info_dict = {}


class StartPluginServicesTests(BaseEvenniaTestCase):
    """``portal_services.start_plugin_services`` registers WS only when needed."""

    @override_settings(
        WEBSERVER_ENABLED=True,
        WEBSOCKET_CLIENT_ENABLED=True,
        WEBSOCKET_CLIENT_PORT=4002,
        WEBSOCKET_CLIENT_INTERFACE="0.0.0.0",
        LOCKDOWN_MODE=False,
    )
    def test_no_op_when_webserver_enabled(self):
        from evennia_shards.portal_services import start_plugin_services

        portal = _FakePortalService()
        start_plugin_services(portal)
        self.assertEqual(len(portal.children), 0)
        self.assertNotIn("webclient", portal.info_dict)

    @override_settings(
        WEBSERVER_ENABLED=False,
        WEBSOCKET_CLIENT_ENABLED=False,
        WEBSOCKET_CLIENT_PORT=4002,
        WEBSOCKET_CLIENT_INTERFACE="0.0.0.0",
        LOCKDOWN_MODE=False,
    )
    def test_no_op_when_websocket_client_disabled(self):
        from evennia_shards.portal_services import start_plugin_services

        portal = _FakePortalService()
        start_plugin_services(portal)
        self.assertEqual(len(portal.children), 0)

    @override_settings(
        WEBSERVER_ENABLED=False,
        WEBSOCKET_CLIENT_ENABLED=True,
        WEBSOCKET_CLIENT_PORT=None,
        WEBSOCKET_CLIENT_INTERFACE="0.0.0.0",
        LOCKDOWN_MODE=False,
    )
    def test_no_op_when_websocket_port_missing(self):
        from evennia_shards.portal_services import start_plugin_services

        portal = _FakePortalService()
        start_plugin_services(portal)
        self.assertEqual(len(portal.children), 0)

    @override_settings(
        WEBSERVER_ENABLED=False,
        WEBSOCKET_CLIENT_ENABLED=True,
        WEBSOCKET_CLIENT_PORT=4012,
        WEBSOCKET_CLIENT_INTERFACE="0.0.0.0",
        LOCKDOWN_MODE=False,
    )
    def test_registers_websocket_when_webserver_disabled(self):
        from twisted.application import internet

        from evennia_shards.portal_services import start_plugin_services

        registered = []
        original = internet.TCPServer.setServiceParent
        try:
            def _record(self, parent):
                registered.append((self, parent))

            internet.TCPServer.setServiceParent = _record
            portal = _FakePortalService()
            start_plugin_services(portal)
        finally:
            internet.TCPServer.setServiceParent = original

        self.assertEqual(len(registered), 1)
        ws_service, parent = registered[0]
        self.assertIs(parent, portal)
        self.assertIn("4012", ws_service.name)

        self.assertIn("webclient", portal.info_dict)
        self.assertEqual(len(portal.info_dict["webclient"]), 1)
        self.assertIn("4012", portal.info_dict["webclient"][0])

    @override_settings(
        WEBSERVER_ENABLED=False,
        WEBSOCKET_CLIENT_ENABLED=True,
        WEBSOCKET_CLIENT_PORT=4012,
        WEBSOCKET_CLIENT_INTERFACE="0.0.0.0",
        LOCKDOWN_MODE=True,
    )
    def test_lockdown_mode_forces_localhost_interface(self):
        """LOCKDOWN_MODE → bind to 127.0.0.1 regardless of configured interface."""
        from twisted.application import internet

        from evennia_shards.portal_services import start_plugin_services

        registered = []
        original = internet.TCPServer.setServiceParent
        try:
            def _record(self, parent):
                registered.append((self, parent))

            internet.TCPServer.setServiceParent = _record
            portal = _FakePortalService()
            start_plugin_services(portal)
        finally:
            internet.TCPServer.setServiceParent = original

        self.assertEqual(len(registered), 1)
        ws_service, _ = registered[0]
        self.assertIn("127.0.0.1", ws_service.name)
