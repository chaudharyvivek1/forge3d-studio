# forge3d-studio

**The product surface around [forge3d](https://github.com/chaudharyvivek1/forge3d).**
Accounts, a job queue, an asset library and shareable scene pages — on top of an
engine that already generates and validates 3D models.

```bash
pip install -e ".[dev]"
forge3d-studio            # http://127.0.0.1:8000
```

Sign in with any email (dev mode returns the token instead of sending it), pick a
shape, press generate, and the viewer opens when the job finishes.

---

## What this repo is, and what it is not

| | [forge3d](https://github.com/chaudharyvivek1/forge3d) | **forge3d-studio** |
|---|---|---|
| Users | AI agents, developers, scripts | people, in a browser |
| Interface | MCP tools, CLI, Python | HTTP API + a web page |
| Runtime | a local process, no accounts | multi-user, hosted |
| Owns | geometry, validation, providers | accounts, jobs, storage, scenes |
| Failure mode | "the agent got a bad mesh" | "the site is down" |

**Studio owns no geometry.** Every mesh comes from the pinned `forge3d` package,
and every validation report shown in the UI is the engine's own. If studio needs a
capability the engine lacks, it goes into the engine, with a test, and a version
bump — not a copy here.

That boundary is the whole design. Merging the two would mean every engine change
ships through a web deploy, and every UI experiment risks the toolchain an agent
depends on.

## What works today

- **Magic-link auth** — single-use tokens, httpOnly session cookie, per-user data
  isolation on every query.
- **A real job queue** — jobs live in SQLite; a worker claims one with a
  conditional `UPDATE` and runs it. Restart-safe: queued work resumes.
- **Cost guard** — credits are reserved with an atomic conditional update *before*
  a paid provider is contacted, and refunded if the job fails. A user at zero
  cannot reach a paid API by racing the check.
- **Progress** — `GET /jobs/{id}/events` streams server-sent events until the job
  is terminal.
- **Cancellation** — queued jobs are cancelled outright; running jobs are flagged
  and stop at the next checkpoint.
- **Assets** — GLB, STL and a single-file viewer page per asset, downloadable.
- **Scenes** — compose assets with positions, then export one self-contained HTML
  page through the engine's scene viewer.
- **A web page** at `/` that does all of the above without curl.

## API

```
GET    /healthz                     service state, queue depth
GET    /kinds                       job kinds, their credit cost and provider
POST   /auth/request                {email} -> magic-link token (dev: in response)
GET    /auth/callback?token=...     sets the session cookie
POST   /auth/logout
GET    /me                          current user and credits

POST   /jobs                        {kind, params, idempotency_key?} -> job
GET    /jobs                        recent jobs for the user
GET    /jobs/{id}
POST   /jobs/{id}/cancel
GET    /jobs/{id}/events            server-sent progress

GET    /assets                      the library
GET    /assets/{id}
GET    /assets/{id}/file/{glb|stl|viewer}

POST   /scenes                      {name, items:[{asset_id, position, scale}]}
GET    /scenes/{id}
POST   /scenes/{id}/export          -> one HTML page
```

## Design decisions worth knowing

**SQLite, not Postgres.** One API process, one writer, a few jobs per minute. WAL
mode gives concurrent readers. The switch point is documented in
[docs/architecture.md](docs/architecture.md): the first time a second API process
writes, this becomes Postgres — a `DATABASE_URL` swap and a migration, not a
rewrite.

**A thread pool, not Celery.** The queue is the `jobs` table, so a crash loses
nothing and a restart resumes. A broker solves a problem this deployment does not
have yet, and a queue you cannot debug is worse than a thread pool you can.

**Atomic claim, not a lock.** `UPDATE jobs SET status='running' WHERE id=? AND
status='queued'` with a rowcount check is the same guarantee as `SELECT ... FOR
UPDATE`, without holding a transaction open across a multi-minute generation.

**Relative storage paths.** Absolute paths in a database are how you get an
unmigratable install. Paths are relative to `STUDIO_DATA_DIR`, and
`storage.resolve()` refuses anything that escapes the root.

**Dev mode is a documented hole.** With `STUDIO_DEV_MODE=true` the magic-link
token is returned in the API response, because there is no mail provider. That is
fine locally and unacceptable in public — set it to `false` before deploying, and
note that it also turns on `Secure` cookies.

## Authentication and billing

Neither lives here. Both belong to the central platform in the private
repository `chaudharyvivek1/forge3d-billing-auth`, which is shared with other
applications on the same site. Studio talks to it over HTTP and holds only:

* the job record, and the platform's reservation id on it;
* a local profile keyed by central account id, so generation history survives.

Studio never sees a password, never stores a credit balance, and never decides
what something costs the user. `docs/central-integration.md` has the full
contract and the failure matrix; the platform's own wire spec is
`docs/central-api.md` in that repository.

## Configuration

Copy `.env.example`. Everything is env-driven:

| variable | default | meaning |
|---|---|---|
| `AUTH_BILLING_API_URL` | `http://127.0.0.1:8080` | the central platform |
| `AUTH_BILLING_APP_ID` | `forge3d-studio` | which application studio is |
| `AUTH_BILLING_SERVICE_TOKEN` | — | server-side credential; required |
| `AUTH_BILLING_TIMEOUT_SECONDS` | `5` | fail closed after this long |
| `STUDIO_REQUIRE_ENTITLEMENT` | `true` | refuse kinds the account lacks |
| `STUDIO_SESSION_CACHE_TTL` | `15` | how long a verified session is trusted |
| `STUDIO_DATA_DIR` | `./.studio` | everything studio writes |
| `STUDIO_WORKERS` | `2` | worker threads in the API process |
| `STUDIO_SESSION_TTL` | `604800` | session cookie lifetime, seconds |

Paid jobs (`kind: "provider"`) additionally need the engine's provider config:
`FORGE3D_PROVIDER` plus that provider's key. Without them the job fails cleanly
and the reservation is released — studio cannot refund anything itself, because
it holds no credits.

`STUDIO_STARTING_CREDITS` and `STUDIO_SECRET` are still read so an old database
and an old `.env` keep working, but neither has any effect now.

## Orphan reservations

A reservation created just before a crash has no job attached, and nothing else
would ever find it. `recover_orphan_reservations` walks **every page** of the
application's reservations at startup and releases those with no local job, after
`STUDIO_ORPHAN_GRACE_SECONDS`. A reservation with any job row is left alone, and
a platform outage ends the sweep rather than hammering a dead service.

## Reliability behaviour

A job is claimed with a lease. The worker renews it on a timer, and a worker
that stops renewing loses its jobs to the sweeper: a paid job becomes
`uncertain` with its reservation held, a free job becomes `failed`. Nothing is
re-queued automatically — a retried paid submission is a second charge.

The event stream only sends a frame when something changed, emits a heartbeat
comment while idle, carries ids, honours `Last-Event-ID`, and ends on
`uncertain` so the page can say "needs manual review" instead of waiting.

Limits are in-process and deliberately simple: `STUDIO_LOGIN_RATE_LIMIT` per
client address, `STUDIO_JOB_RATE_LIMIT` per account, `STUDIO_MAX_PAYLOAD_BYTES`
on the request and `STUDIO_MAX_PARAMS_BYTES` on generation parameters.

## Tests

```bash
pytest -q
```

72 tests, no network, no provider keys, no GPU, no real money. They cover
sign-up → generate → download in one flow; a paid job refused *before* the call;
refunds on failure; idempotent submission; cancellation; cross-user isolation;
and a scene exporting to a page with both models embedded.

The central platform's tests run the **real** service app in-process — FastAPI's
`TestClient` behind studio's HTTP client — so the integration tests exercise the
actual status codes and reservation state machine rather than a stub. Install the
private package to run them:

```bash
pip install -e ../forge3d-billing-auth
```

Without it those tests are skipped, not failed.

The suite takes around 45 seconds, most of it real geometry in the generation
tests. `pytest -q tests/test_reliability.py` alone is under ten.

The worker is driven synchronously in tests (`Worker.process_one()`) so "the job
finished" is a fact rather than a race.

## Roadmap

This is milestone **S1–S2** of the scope: skeleton, auth, and a working pipeline
with the local engines. Next, in order:

1. **S3** — the first paid provider adapter wired through the job queue with live
   cost accounting.
2. **S4** — a scene editor (drag, position, save) instead of an API-only compose.
3. **S5** — export optimisation: LOD selection, Draco/KTX2, a stated size budget.

Deliberately not here: billing, teams, a marketplace, self-hosted GPU inference.

## Licence

MIT.
