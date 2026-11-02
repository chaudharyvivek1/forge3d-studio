# Changelog

## [0.2.0] — 2026-11-02

### Changed — authentication and billing move to the central platform

Studio no longer owns identity or credits. Both live in the private
`forge3d-billing-auth` service, which is shared with other applications on the
platform. Studio reaches it over HTTP with a service token.

- `POST /auth/request` and `GET /auth/callback` now proxy to the platform. The
  magic link lands on studio's own registered callback and is exchanged
  server-side for an access token, which studio keeps in its own cookie.
- `current_user` verifies the token against the platform. Verification is cached
  for `STUDIO_SESSION_CACHE_TTL` seconds, so revocation is only as prompt as that
  window — stated here rather than discovered later.
- `GET /me` reads the balance and entitlements from the platform. The `apps` and
  `entitlements` fields are new.
- `POST /jobs` checks entitlement, creates a reservation with an idempotency key,
  and only then inserts the job. `jobs.reservation_id` records it.
- The worker commits on success, releases when nothing was sent, and flags
  uncertain submissions. It no longer touches a local credit balance.
- Cancelling a queued job releases its reservation; cancelling a running job only
  flags it, because the provider may already have charged.

### Added

- `forge3d_studio.central` — the platform client, with the failure policy in one
  place: fail closed, never invent a state, never retry uncertain work.
- `forge3d_studio.reconcile` — startup classification of jobs still holding a
  reservation. A job found `running` after a restart is flagged, never requeued.
- `users.central_account_id` and `jobs.reservation_id`, both additive migrations.
  An existing v0.1.2 profile is adopted by email on first sign-in, so generation
  history stays attached.
- `uncertain` is now a terminal state on the job event stream, so a client stops
  waiting instead of hanging.
- 28 new tests, including the full Phase 6 failure matrix.

### Not changed

Generation, assets, scenes and export work exactly as before; the 29 tests that
covered them still pass unchanged against the real platform.

### Migration note

Existing `users.credits` values are preserved but ignored. They are **not**
converted automatically — export them and grant them on the platform
deliberately. See `docs/central-integration.md`.


## [0.1.2] — 2026-11-01

### Fixed — an uncertain submission was refunded

If a provider accepted a job and we lost the response, the engine reported
`uncertain`; the worker turned that into an exception and `_fail()` refunded the
credits. The provider had probably already been charged, so that is a way to get
paid generation for free.

`uncertain` is now its own job status, not a flavour of `failed`, and it is never
refunded automatically. The job keeps its credits, is flagged `needs_review`, and
carries the idempotency key so an operator can reconcile it with the provider.

The refund rule is now explicit rather than incidental:

| outcome | refund | why |
|---|---|---|
| cancelled while queued | yes | the provider was never contacted |
| cancelled while running | no | it may already have been charged |
| not configured | yes | nothing was sent |
| provider rejected it | yes | nothing was produced |
| submission uncertain | **no** | it may exist and have been billed |
| any other failure, paid | **no** | we cannot prove nothing was charged |

Two supporting changes make that honest:

- **A pre-flight configuration check.** `_require_provider()` runs *before* the
  network call, so "not configured" is a fact rather than an inference from an
  error string — and it is the one case where a refund is certainly safe.
- **Two new job columns**, `refunded` and `needs_review`, exposed on every job in
  the API. They are added by a migration, because `CREATE TABLE IF NOT EXISTS`
  does not touch a database that already exists.

### Tests

28 passing (was 23). New: uncertain is not refunded and is flagged for review, a
rejected submission is refunded, an unexpected exception keeps the credits, a free
job is never flagged, and an existing database gains the new columns.

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
