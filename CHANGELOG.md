# Changelog

## [0.1.1] — 2026-11-01

Two defects found by using the product rather than reading it.

### Fixed — cancelling a queued paid job burned the credits

Submit a `provider` job (25 credits reserved), cancel it before a worker picks it
up, and the credits were gone — even though no provider was ever contacted. That
is taking money for nothing.

`request_cancel` now refunds when it cancels a queued job. The refund sits behind
the same conditional `UPDATE ... WHERE status='queued'` that performs the
cancellation, so only the caller that actually cancelled refunds; a second cancel
returns 409 and refunds nothing.

Cancelling a **running** job still does not auto-refund: the provider may already
have been charged for work it performed. The worker refunds if the job fails
without producing an asset.

### Fixed — the API raised `no such table` before the lifespan ran

The schema was created only in the FastAPI lifespan, so anything that touched the
API without one — a `TestClient` used without `with`, an ASGI bridge, a one-off
script — failed with a raw `sqlite3.OperationalError` 500. The schema is now
created on first connection to each database file, keyed by path so a settings
change also gets one.

### Tests

23 passing (was 20). New: cancel refunds, a double cancel refunds once, and the
API answering before the lifespan runs.

Verified additionally by a scripted user journey — sign in, generate a shape and a
planet, download both, compose and export a scene, cancel a paid job, simulate a
restart, and check a second user sees nothing — run three times with no failures.
