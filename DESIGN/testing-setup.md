# Testing setup

How the library's unit tests are configured to run without a consumer gamedir.

## Goal

The library's tests must verify the library, not a consumer game. They should:

- Run from the library root with no dependency on `examples/demo_game/` (or any other gamedir).
- Not require Evennia's runtime services (Twisted reactor, Portal, Server, sessions, network listeners). Only Django's ORM is needed.
- Pass deterministically regardless of what the consumer's `settings.py` looks like.

## Structure

```
evennia-shards/
├── runtests.py                    # one-line test invocation
├── tests/
│   ├── __init__.py
│   ├── test_settings.py           # minimal Django test settings
│   └── urls.py                    # empty URL patterns
└── evennia_shards/
    └── tests.py                   # the actual test code (Evennia convention)
```

The split is intentional:

- **`tests/` at the repo root** holds *test infrastructure* (settings, runner config, URL stubs) — not test cases.
- **`evennia_shards/tests.py`** holds *test cases*, in the single-file shape Evennia uses across its own packages (~60 `tests.py` files in the Evennia source).

Mixing them would either pollute the library package with non-test infrastructure or scatter test code across two locations. Keeping infra outside the package and tests inside it matches both Django and Evennia conventions.

## Test settings

[`tests/test_settings.py`](../tests/test_settings.py) imports `evennia.settings_default` and overrides what's needed for a no-gamedir run:

- `INSTALLED_APPS` += `evennia_shards`
- `DATABASES` → in-memory sqlite (`":memory:"`)
- `SHARDS_ROLE = "shard"`, `SHARD_ID = "shard0"` — pinned so the tenancy install runs with a real tenant scope
- `ROOT_URLCONF = "tests.urls"` — overrides Evennia's default `"web.urls"`, which expects a gamedir's `web/` module
- `GAME_DIR` and `LOG_DIR` → `tempfile.gettempdir()` paths so the path-derived defaults in `settings_default` resolve

`SECRET_KEY` and `TEST_ENVIRONMENT` are set to satisfy Django/Evennia startup checks.

## Test base class: `BaseEvenniaTestCase`

All test classes inherit from `evennia.utils.test_resources.BaseEvenniaTestCase`, not the bare `django.test.TestCase`. This is the same base class Evennia's own internal tests use.

Why:

- `BaseEvenniaTestCase` carries `@override_settings(**DEFAULT_SETTINGS)` where `DEFAULT_SETTINGS` forces every gamedir-shaped setting (`CMDSET_*`, `BASE_*_TYPECLASS`, `CONNECTION_SCREEN_MODULE`, ...) to `evennia.game_template.*` fallbacks that ship inside the Evennia package itself. No gamedir needed.
- `tearDown` runs `flush_cache()` on Evennia's idmapper. Without this, instances cached by one test bleed into the next — the same idmapper-cache footgun observed in the live smoke test where a manual `UPDATE objects_objectdb SET shard_id=...` was shadowed by a stale in-memory instance.

## Running tests

From the library root:

```
python runtests.py
```

`runtests.py` sets `DJANGO_SETTINGS_MODULE=tests.test_settings`, calls `django.setup()`, and invokes Django's test runner against the `evennia_shards` package.

## Decoupled from `examples/demo_game/`

After this setup, the demo_game can be in any mode — monolith, shard, or deleted entirely — and the test suite is unaffected. The library's test database is sqlite in-memory and never touches the demo_game's database. The demo_game's `settings.py` is never read by the test runner.

The demo_game continues to serve a separate purpose: humans driving the library interactively (in-game `@py`, exercising real workflows against a running Evennia server). Test gamedir vs demo gamedir, two distinct concerns.

## Out of scope

Deliberately not addressed by the current setup; if any of these become useful, they are separate workstreams:

- pytest-django integration (Django's built-in test runner is sufficient).
- CI configuration.
- Coverage reporting.
