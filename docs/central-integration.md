# Central authentication and billing — how studio integrates

Studio no longer authenticates users or holds credits. Both live in the private
platform at `chaudharyvivek1/forge3d-billing-auth`, which is shared with other
applications. This document is studio's half of that contract.

The wire contract itself is in that repository, at `docs/central-api.md`.

## What studio sends

| Variable | Meaning |
| --- | --- |
| `AUTH_BILLING_API_URL` | base URL of the platform |
| `AUTH_BILLING_APP_ID` | `forge3d-studio` — must be registered on the platform |
| `AUTH_BILLING_SERVICE_TOKEN` | server-side credential, bound to that app id |
| `AUTH_BILLING_TIMEOUT_SECONDS` | how long to wait before failing closed |

The service token never reaches a browser, is never logged, and is never echoed
in an error body. `tests/test_integration_central.py` asserts that last one.

## Who owns what

| Studio | Platform |
| --- | --- |
| job records and status | credit balances |
| provider status and errors | credit transactions and ledger |
| output assets and metadata | payment records |
| local profile (display data) | subscriptions and entitlements |
| scene documents | reservations and final usage accounting |
| | user identity and sessions |

A *reservation* is the seam. Studio holds a reservation id on each job; the
money is the platform's problem.

## The submission flow

1. Authenticate the caller — the platform verifies the access token.
2. Check entitlement for the requested kind (`GET .../entitlements`).
3. Read the cost from `KINDS` in `worker.py`.
4. `POST /v1/service/reservations` with a unique idempotency key.
5. Store `reservation_id` on the job row.
6. Run the generation.
7. `commit` on confirmed success.
8. `release` on a failure where no provider charge occurred.
9. `flag` for an uncertain provider submission — credits stay held.
10. Reconcile pending reservations at startup.

Steps 1–5 happen in `api.submit_job`; 7–9 in `worker._commit` and
`worker._settle_money`; 10 in `reconcile.reconcile_pending`.

## Failure policy

**Fail closed.** If the platform cannot confirm that a paid job is authorised,
no job is created and no provider is called. The user gets a `503`, which is
always better than a generation nobody paid for.

| Situation | Studio's answer |
| --- | --- |
| Platform unreachable during login | `503`, no session |
| Platform unreachable at submit | `503`, nothing queued, nothing reserved |
| Insufficient credits (`402`) | `402`, nothing queued |
| Not entitled (`403`) | `403`, nothing queued |
| Duplicate idempotency key | the original job, `201`-shaped, charged once |
| Job insert fails after reserving | reservation released, no stranded credits |
| Commit fails after success | asset kept, job flagged `needs_review`, credits stay held |
| Release fails | job flagged `needs_review`, never retried automatically |

The last two are deliberate: money left held is a support ticket, money handed
back twice is a loss.

## Startup reconciliation

`reconcile.reconcile_pending` runs in the lifespan before the worker starts, and
classifies every job that still holds a reservation:

| Job state | Reservation state | Action |
| --- | --- | --- |
| `queued` | `reserved` | leave it; nothing was sent |
| `running` | `reserved` | **flag for review, mark `uncertain`** — never retry |
| any | `refunded` | mark the job `failed`, `refunded=1` |
| any | `settled` | mark `uncertain` — credits spent, no local result |
| any | `needs_review` | mark `needs_review` |

If the platform is unreachable the pass stops and changes nothing; the next boot
tries again. Nothing is ever requeued on the strength of a guess.

## Migrating an existing studio database

The schema migration is additive and runs on boot:

* `jobs.reservation_id TEXT` — the platform's reservation for this job.
* `users.central_account_id TEXT` — the local profile is keyed by central account.

`db.profile_for_central_account` adopts an existing v0.1.2 profile by email the
first time someone signs in through the platform, so generation history stays
attached to its owner.

**Existing local credit balances are not silently converted.** They stay in
`users.credits` and are ignored — the platform's balance is the only one that
counts. Before switching a live deployment over, export the old balances and
grant them on the platform deliberately:

```sql
SELECT email, credits FROM users WHERE credits > 0;
```

Then grant each one on the platform. Doing it explicitly is the point: a
migration that quietly moves money is a migration nobody can audit.

## Local development with a mocked platform

Studio's tests run the **real** platform app in-process — no socket, no network,
no real money. `tests/central_fake.py` builds the service with FastAPI's
`TestClient` and points studio's client at it by swapping the transport, so the
tests exercise the actual status codes and state machine rather than a stub.

To run them you need the private package installed:

```bash
pip install -e ../forge3d-billing-auth
pytest
```

Without it those tests are skipped, not failed — `pytest.importorskip` in
`tests/central_fake.py`. That is a real gap in CI when the credential is absent,
and is called out in the workflow file.
