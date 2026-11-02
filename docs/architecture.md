# Architecture

## The shape of it

```
browser ──► FastAPI (api.py) ──► SQLite ──► worker threads ──► forge3d engine
                  │                              │
                  └── storage.py (local FS) ◄────┘
```

One process. One database file. One directory of assets. That is deliberate: every
piece of infrastructure you add before you need it is a piece you have to debug
without knowing whether you need it.

## Why each choice

### The engine is a library, not a service

The worker imports `forge3d` and calls its public functions. It does not shell out
to the MCP server, and it does not reimplement geometry. `config.apply_to_engine()`
points the engine at studio's own work directory before the first call, so a
studio install cannot corrupt a developer's local `./.forge3d` state.

Consequence: a studio deploy is pinned to an engine version
(`forge3d>=0.6.5,<0.7`). Upgrading the engine is a deliberate act with a release
note, not a side effect of `pip install -U`.

### SQLite, with a written-down exit

| Constraint | Today | When it breaks |
|---|---|---|
| one API process | true | the second replica |
| one writer at a time | true (WAL allows concurrent readers) | a background job per request |
| a few jobs/minute | true | a hundred |

The switch is Postgres behind the same `db.py` functions — the SQL is already
parameterised and contains no SQLite-specific syntax beyond `PRAGMA`. What must
change at that point:

1. `connect()` returns a `psycopg` connection with `dict_row` instead of
   `sqlite3.Row`.
2. `claim_next_job` becomes `SELECT ... FOR UPDATE SKIP LOCKED` inside a
   transaction, which is the Postgres spelling of the same guarantee.
3. `REAL` timestamps become `timestamptz`.

Nothing in the API layer changes.

### The queue is the table

`claim_next_job()` is:

```sql
UPDATE jobs SET status='running', started_at=?
WHERE id=? AND status='queued'
```

with a rowcount check. Two workers racing produce one winner and one `0 rows`;
the loser tries the next job. This is why no broker is needed yet: the guarantee a
queue gives you is "exactly one worker takes a job", and a conditional UPDATE
provides exactly that.

A restart resumes: `status='queued'` rows are still there. A crash mid-job leaves
`running`, which `jobs.status` alone cannot distinguish from "still working" —
see *Known gaps* below.

### Credits are reserved, not charged

`spend_credits()` is one conditional UPDATE:

```sql
UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?
```

If it returns 0 rows, the submit is refused with `402` **before** a provider is
contacted. Charging after the call means a user at zero can spend money by racing
the check. A failed paid job refunds, because charging for a failure the user
cannot diagnose is a support ticket rather than a business.

### Storage paths are relative

`assets.glb_path` is `"<user_id>/<asset_id>/model.glb"`, resolved against
`STUDIO_DATA_DIR`. Absolute paths in a database are how you get an install you
cannot move. `storage.resolve()` rejects anything that escapes the root.

## Known gaps

Honest list, in the order I would fix them:

1. **A crashed worker leaves a job `running` forever.** The engine has a
   heartbeat for its own jobs; studio's queue does not yet. Fix: a
   `heartbeat_at` column, touched by the worker, plus a reaper that re-queues
   `running` jobs older than N minutes.
2. **No live provider adapter.** `kind: "provider"` is wired through the queue and
   the cost guard, but a run needs `FORGE3D_PROVIDER` and a key. Without them it
   fails cleanly and refunds — which is what the tests assert.
3. **Dev mode returns login tokens in the response.** Fine locally; set
   `STUDIO_DEV_MODE=false` anywhere else.
4. **No email delivery.** Magic links are generated and stored; sending them is a
   provider integration that does not exist yet.
5. **The scene editor is an API, not a UI.** Compose is `POST /scenes`; the page
   shows assets and viewers. Dragging is milestone S4.
6. **No rate limiting.** One authenticated user can queue jobs as fast as they can
   POST. The cost guard bounds *spend*, not *load*.

## What is deliberately absent

Billing, teams, permissions beyond "your own rows", a marketplace, GPU inference,
webhooks, and a second service. Each of those is a decision with a cost, and none
of them is needed to answer the question this repo exists to answer: can someone
sign in, generate a model, and get a link they can send to a client?

## Worker leases

A job is claimed by a conditional `UPDATE` that also stamps `worker_id`,
`heartbeat_at` and `lease_expires_at`. That row is a lease: it says *this worker
is running this job until this moment*.

A heartbeat thread renews the lease on everything the worker owns, on a timer
rather than only when a handler reports progress, because a provider call can
take minutes without calling back. A worker that dies stops renewing, and its
leases expire.

The sweeper then reclaims expired jobs. It never re-queues them: for a paid kind
the provider may already have been called, so the job becomes `uncertain` and its
reservation is flagged for review. A free kind failed safely and is marked
`failed`. This is the same rule the startup reconciliation applies, and it exists
because the alternative — retrying — is how a product pays twice for one job.

## Event stream

Three properties, each fixing a specific production failure:

- **only on change** — an idle job costs one database read per poll, not a stream
  of identical frames;
- **heartbeat comments** — proxies drop connections that go quiet, and a browser
  that reconnects forever is worse than a slow one;
- **ids and `Last-Event-ID`** — a reconnecting client resumes instead of
  restarting its display.

`uncertain` is terminal. The client stops, the page says "needs manual review",
and a human settles it.

## Limits

Rate limiting is an in-process sliding window, not Redis. That is honest about
what it does: it stops one client hammering one endpoint, and it does not pretend
to stop a distributed attack. The switch point is the same one that moves SQLite
to Postgres — the moment a second process writes.

`X-Forwarded-For` is deliberately not trusted for the login limit. A spoofable
header is worse than no limit, because it looks like one.
