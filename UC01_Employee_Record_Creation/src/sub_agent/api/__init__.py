"""HTTP entry point for the UC-01 sub-agent.

`app` is the default FastAPI application, so the sub-agent is served as:

    uvicorn sub_agent.api:app

`create_app(container)` is re-exported for tests that want a fresh app
per test. `build_container` fails closed: outside APP_ENV=dev it refuses
to start on in-memory adapters, and when Supabase is configured it
refuses to start if the connection test fails.

The modules behind this package (`app`, `container`, `settings`,
`identity`, `schemas`, `error_handlers`) stay importable directly; this
only puts the three names a caller normally wants at the top.

Importing this package builds NOTHING and needs no credentials: `app` is
a LazyDefaultApp that builds the default container on first use - when a
server starts (ASGI lifespan) - and fails closed there if the
configuration is refused. The Supabase connection test then runs in the
real app's startup hook (see create_app).
"""

from .app import app, create_app
from .container import Container, build_container
from .settings import Settings

__all__ = ["Container", "Settings", "app", "build_container", "create_app"]
