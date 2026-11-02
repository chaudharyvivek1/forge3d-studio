# Changelog

## [0.2.2] — 2026-11-02

### Fixed — orphan recovery only ever saw the first 200 reservations

`reservations_for_app()` read one page and returned it as the whole set, so every
orphan past row 200 was invisible to the sweep — which is precisely the backlog
that needed recovering. It now follows the platform's cursor to exhaustion.

The client refuses two failure modes rather than papering over them: a cursor
that repeats (which would loop forever) and more than `max_pages` pages (which
would mean the sweep is not converging). Both raise, so a partial read is never
mistaken for a complete one.

### Fixed — the sweep could not stop cleanly

A platform outage during a release now ends the sweep instead of hammering a
dead service. Per-item rejections are counted as `failed` and the sweep carries
on. Reservations that are not in `reserved` state are skipped defensively, and a
reservation with any local job row is still never touched.

### Changed

- `tests/central_fake.py` asserts the installed platform package has the
  capabilities these tests need. An editable install left pointing at an old
  checkout used to make the suite pass against the wrong code; it now fails
  loudly with the path and version it found.
- Slow tests are marked `slow` and run in the `regression` workflow.
- CI is split three ways: `unit` (no credential), `integration` (credential
  required, fails rather than skips), `regression` (everything, nightly).

### Fixed — Windows CI failed on a bash conditional

`ci.yml` installed the private platform package behind `if [ -n "$TOKEN" ]`.
GitHub's Windows runners default to PowerShell, which rejects that syntax with
`ParserError: Missing '(' after 'if' in if statement`, so both Windows jobs
failed before running a single test.

Every workflow now sets `defaults.run.shell: bash`, so a shell script written
once behaves the same on Ubuntu, macOS and Windows. That fixes this instance and
the whole class of bug with it.

### Changed — one pipeline, not two

`ci.yml` duplicated the newer workflows and was the only place the failure could
happen. Its valuable part — the cross-platform matrix — moved to the maintained
workflows, and it was deleted:

| Workflow | Runs | Credentials |
| --- | --- | --- |
| `unit` | Ubuntu, macOS, Windows × Python 3.10, 3.12 | none |
| `integration` | Ubuntu, PRs and main | `BILLING_AUTH_TOKEN`, required |
| `regression` | Ubuntu, macOS, Windows, nightly and main | `BILLING_AUTH_TOKEN`, required |

`integration` fails rather than skipping when the credential is absent, and fork
pull requests get an explicit "nothing ran" notice instead of a false pass.

### Added — pagination input validation

`reservations_for_app` now rejects a page size outside 1–1000, a non-integer
page size, and a page budget below 1, instead of forwarding them and letting the
platform answer with a 422. A page budget that runs out raises rather than
returning a partial list as if it were complete.

- 18 client tests: invalid limits, an empty page that still has a cursor, a
  repeated cursor, the page-budget ceiling, cursor URL encoding, filters carried
  across pages, and a mid-pagination platform error.

### Note

Studio needed no schema change for this release; the pagination work is a query
concern. Existing jobs keep their reservation references untouched.


## [0.2.1] — 2026-11-02

Hardening found by reviewing v0.2.0 across both repositories.

### Fixed — studio could name its own price

`POST /jobs` sent a cost taken from studio's own `KINDS` table, and the platform
believed it. Studio now sends no price at all: the platform looks it up and the
job records what the platform charged. `GET /kinds` reads the platform's price
list and reports `authoritative: false` when it had to fall back.

### Fixed — reservations could be stranded

A reservation created just before the process died had no job attached, so no
sweep would ever find it and the credits stayed held forever.
`recover_orphan_reservations` runs at startup and releases reservations with no
local job, after `STUDIO_ORPHAN_GRACE_SECONDS` (default 300). A reservation with
any job row is left to normal reconciliation, and a reservation that might have
reached a provider is never released — a reservation with no job cannot have
reached one, because only a claimed job runs a handler.

### Fixed — a delivered job could stay uncommitted

A job that succeeded but whose commit failed was `succeeded` with
`needs_review`, which the old reconciliation skipped because it only looked at
`queued` and `running`. It now retries the idempotent commit on restart and
clears the flag. Credits are never released for an asset the user already has.

### Fixed — an outage looked like an empty wallet

`/me` caught any platform error and returned `credits: 0`. A real zero and an
unreachable billing service are opposite problems, so it now returns `503` when
the platform is unreachable and `502` when it answers badly.

### Changed

- `AUTH_BILLING_*` session cache window now comes from
  `STUDIO_SESSION_CACHE_TTL` instead of a hard-coded 15 seconds.
- `central.CentralClient` gained `prices()` and `reservations_for_app()`.

### Added

- `integration.yml`: a protected workflow that **fails** when the private
  platform credential is missing, and sets `REQUIRE_INTEGRATION=1` so a missing
  package is an error rather than a skip. `ci.yml` keeps running without it and
  says so.
- 14 tests: price authority, unpriced kinds, orphan recovery and its grace
  period, commit recovery for a delivered asset, the `/me` split, and a guard
  that fails if the cross-repository tests are silently skipped.


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
- 28 new tests for the integration, including the full failure matrix.

### Reliability (the v0.1.3 scope)

- **Worker leases and heartbeats.** A claim records `worker_id`, `heartbeat_at`
  and `lease_expires_at`; a heartbeat thread renews the lease on everything the
  worker is running. A worker that stops breathing loses its jobs.
- **Stale job reclamation.** Expired leases are swept by the pool. A paid job is
  moved to `uncertain` and its reservation flagged; a free job is marked
  `failed`. Nothing is ever requeued, because a retried paid submission is a
  second charge.
- **Local idempotency index.** A unique partial index on
  `(user_id, idempotency_key)` backs up the platform's own deduplication.
- **SSE reliability.** Frames are only sent when something changed, a heartbeat
  comment keeps idle connections alive through proxies, every frame carries an
  id, `Last-Event-ID` resumes a reconnecting client, and the stream asks the
  browser to retry with `retry:`. `x-accel-buffering: no` stops nginx eating it.
- **Uncertain in the frontend.** The job stream ends on `uncertain` and the page
  says "needs manual review" instead of waiting for a state that will not come.
- **Rate limits.** Magic-link requests are limited per client address (the one
  endpoint that makes the server send mail) and job submissions per account.
- **Payload limits.** Oversized bodies are refused before parsing; oversized
  generation parameters are refused before anything is reserved.
- 15 more tests: leases, heartbeats, reclamation, stream framing, resume,
  rate limits and payload guards.

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
