"""SQLite schema and access helpers.

SQLite, not Postgres, for the MVP — and the reason is written down rather than
assumed: one API process, one writer at a time, a few jobs per minute. WAL mode
gives concurrent readers. The moment a second API process writes, this has to
become Postgres, which is a `DATABASE_URL` swap and a schema migration, not a
rewrite.

Every write that must not race (claiming a job, spending credits) is a single
conditional UPDATE with a `WHERE` clause and a rowcount check. That is the same
guarantee a `SELECT ... FOR UPDATE` gives, without holding a transaction open.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    credits       INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS login_tokens (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    params            TEXT NOT NULL,
    status            TEXT NOT NULL,
    provider          TEXT NOT NULL DEFAULT 'local',
    cost              INTEGER NOT NULL DEFAULT 0,
    progress          REAL NOT NULL DEFAULT 0,
    stage             TEXT NOT NULL DEFAULT '',
    error             TEXT,
    asset_id          TEXT,
    idempotency_key   TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    refunded          INTEGER NOT NULL DEFAULT 0,
    needs_review      INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    started_at        REAL,
    finished_at       REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS assets (
    id           TEXT PRIMARY KEY,
    job_id       TEXT,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    glb_path     TEXT,
    stl_path     TEXT,
    html_path    TEXT,
    triangles    INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    validation   TEXT NOT NULL DEFAULT '{}',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_user ON assets(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scenes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    doc         TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS scenes_user ON scenes(user_id, updated_at DESC);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


#: Databases whose schema this process has already created. Checking a set is
#: cheaper than `CREATE TABLE IF NOT EXISTS` on every connection, and keying it by
#: path means a settings change (a test, a second workdir) still gets a schema.
_INITIALISED: set[str] = set()


@contextmanager
def connect(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """A connection with row access by name, schema guaranteed.

    The schema is created on first use of each database file, not only at startup:
    otherwise anything that touches the API before the lifespan runs — a test
    client used without `with`, an ASGI bridge, a one-off script — fails with a
    raw "no such table" instead of working.
    """
    db_path = Path(path or get_settings().db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        key = str(db_path.resolve())
        if key not in _INITIALISED:
            conn.executescript(SCHEMA)
            _migrate(conn)
            _INITIALISED.add(key)
        yield conn
    finally:
        conn.close()


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not
#: add them to a database that already exists, so they are applied explicitly.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "jobs": [
        ("refunded", "INTEGER NOT NULL DEFAULT 0"),
        ("needs_review", "INTEGER NOT NULL DEFAULT 0"),
        #: The central platform's reservation. Money is tracked there now; this
        #: column is how a job and its reservation find each other again.
        ("reservation_id", "TEXT"),
    ],
    "users": [
        #: Studio's local profile is keyed by the central account id, so a
        #: profile survives the platform's own changes and never duplicates a
        #: person who logs in twice.
        ("central_account_id", "TEXT"),
    ],
}

#: Indexes added after the first release. `IF NOT EXISTS` is safe to repeat, but
#: they are listed here rather than in SCHEMA so an existing database gets them.
INDEX_MIGRATIONS: list[str] = [
    "CREATE UNIQUE INDEX IF NOT EXISTS users_central "
    "ON users(central_account_id) WHERE central_account_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS jobs_reservation ON jobs(reservation_id)",
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    for statement in INDEX_MIGRATIONS:
        conn.execute(statement)


def init_db(path: Path | str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# ---------------------------------------------------------------------------
# users and sessions
# ---------------------------------------------------------------------------
def ensure_user(conn: sqlite3.Connection, email: str, starting_credits: int) -> sqlite3.Row:
    email = email.strip().lower()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return row
    conn.execute(
        "INSERT INTO users (id, email, credits, created_at) VALUES (?, ?, ?, ?)",
        (new_id(), email, starting_credits, time.time()),
    )
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def profile_for_central_account(
    conn: sqlite3.Connection, central_account_id: str, email: str
) -> sqlite3.Row:
    """Find or create studio's own profile for a central account.

    The central platform owns the person; studio owns the profile hanging off
    them (display preferences later, ownership of assets and scenes now). A
    profile that already exists is reused, which is what keeps generation
    history attached when the platform's user id is all we start from.
    """
    email = email.strip().lower()
    row = conn.execute(
        "SELECT * FROM users WHERE central_account_id = ?", (central_account_id,)
    ).fetchone()
    if row is not None:
        return row

    # A v0.1.2 profile with the same email is adopted rather than duplicated.
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE users SET central_account_id = ? WHERE id = ?",
            (central_account_id, row["id"]),
        )
        return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()

    user_id = new_id()
    conn.execute(
        "INSERT INTO users (id, email, credits, created_at, central_account_id) "
        "VALUES (?, ?, 0, ?, ?)",
        (user_id, email, time.time(), central_account_id),
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_session(conn: sqlite3.Connection, user_id: str, ttl: int) -> str:
    import secrets

    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, now, now + ttl),
    )
    return token


def user_for_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    row = conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token = ? AND s.expires_at > ?""",
        (token, time.time()),
    ).fetchone()
    return row


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# login tokens
# ---------------------------------------------------------------------------
def create_login_token(conn: sqlite3.Connection, email: str, ttl: int) -> str:
    import secrets

    token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        """INSERT INTO login_tokens (token, email, created_at, expires_at, used)
           VALUES (?, ?, ?, ?, 0)""",
        (token, email.strip().lower(), now, now + ttl),
    )
    return token


def consume_login_token(conn: sqlite3.Connection, token: str) -> str | None:
    """Single use: the UPDATE only succeeds once, so a token cannot be replayed."""
    now = time.time()
    row = conn.execute(
        "SELECT email FROM login_tokens WHERE token = ? AND used = 0 AND expires_at > ?",
        (token, now),
    ).fetchone()
    if not row:
        return None
    updated = conn.execute(
        "UPDATE login_tokens SET used = 1 WHERE token = ? AND used = 0", (token,)
    ).rowcount
    return row["email"] if updated == 1 else None


# ---------------------------------------------------------------------------
# credits
# ---------------------------------------------------------------------------
def spend_credits(conn: sqlite3.Connection, user_id: str, cost: int) -> bool:
    """Reserve credits atomically. False means "not enough" — never a partial charge.

    Called *before* a paid provider is contacted. Charging after the fact means a
    user at zero can still spend money by racing the check.
    """
    if cost <= 0:
        return True
    updated = conn.execute(
        "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?",
        (cost, user_id, cost),
    ).rowcount
    return updated == 1


def refund_credits(conn: sqlite3.Connection, user_id: str, cost: int) -> None:
    if cost > 0:
        conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (cost, user_id))


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def create_job(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    kind: str,
    params: dict,
    provider: str = "local",
    cost: int = 0,
    idempotency_key: str | None = None,
    reservation_id: str | None = None,
) -> sqlite3.Row:
    job_id = new_id()
    conn.execute(
        """INSERT INTO jobs (id, user_id, kind, params, status, provider, cost,
                             progress, stage, idempotency_key, reservation_id,
                             created_at)
           VALUES (?, ?, ?, ?, 'queued', ?, ?, 0, 'queued', ?, ?, ?)""",
        (job_id, user_id, kind, json.dumps(params), provider, cost,
         idempotency_key, reservation_id, time.time()),
    )
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def job_for_reservation(conn: sqlite3.Connection, reservation_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM jobs WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()


def jobs_with_pending_reservations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Jobs that hold a reservation and have not reached a terminal state.

    These are what a restart has to reconcile: the money is reserved on the
    platform, and studio has to find out what actually happened.
    """
    return conn.execute(
        "SELECT * FROM jobs WHERE reservation_id IS NOT NULL "
        "AND status IN ('queued', 'running') ORDER BY created_at"
    ).fetchall()


def claim_next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Take one queued job, atomically.

    `UPDATE ... WHERE status = 'queued'` with a rowcount check is what makes two
    workers safe: whoever's UPDATE matches wins, the other sees 0 rows.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        claimed = conn.execute(
            """UPDATE jobs SET status = 'running', started_at = ?, stage = 'starting'
               WHERE id = ? AND status = 'queued'""",
            (time.time(), candidate["id"]),
        ).rowcount
        if claimed == 1:
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (candidate["id"],)).fetchone()
        # Lost the race: another worker took it. Try the next one.


def update_job(conn: sqlite3.Connection, job_id: str, **fields) -> None:
    allowed = {"status", "progress", "stage", "error", "asset_id",
               "finished_at", "cancel_requested", "refunded", "needs_review"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"cannot update job fields: {sorted(unknown)}")
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?",
                 (*fields.values(), job_id))


def job_is_cancelled(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def request_cancel(conn: sqlite3.Connection, job_id: str, user_id: str) -> bool:
    """Mark for cancellation.

    A **queued** job is cancelled outright and its credits are refunded: the
    provider was never contacted, so charging for it is simply taking money for
    nothing. The conditional UPDATE is what makes the refund safe — only the
    caller whose UPDATE matched gets to refund, so a double cancel cannot refund
    twice.

    A **running** job is only flagged. Credits are not refunded automatically
    because the provider may already have been charged for work it performed;
    the worker refunds if the job fails without producing anything.
    """
    row = conn.execute(
        "SELECT status, cost FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)
    ).fetchone()
    if row is None:
        return False
    if row["status"] == "queued":
        claimed = conn.execute(
            """UPDATE jobs SET status = 'cancelled', finished_at = ?, stage = 'cancelled'
               WHERE id = ? AND status = 'queued'""",
            (time.time(), job_id),
        ).rowcount
        if claimed == 1 and row["cost"]:
            refund_credits(conn, user_id, int(row["cost"]))
        return claimed == 1
    if row["status"] == "running":
        update_job(conn, job_id, cancel_requested=1)
        return True
    return False


# ---------------------------------------------------------------------------
# assets and scenes
# ---------------------------------------------------------------------------
def create_asset(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    job_id: str | None,
    name: str,
    glb_path: str | None,
    stl_path: str | None,
    html_path: str | None,
    triangles: int,
    size_bytes: int,
    validation: dict,
) -> sqlite3.Row:
    asset_id = new_id()
    conn.execute(
        """INSERT INTO assets (id, job_id, user_id, name, glb_path, stl_path, html_path,
                               triangles, size_bytes, validation, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asset_id, job_id, user_id, name, glb_path, stl_path, html_path,
         triangles, size_bytes, json.dumps(validation), time.time()),
    )
    return conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()


def create_scene(conn: sqlite3.Connection, *, user_id: str, name: str, doc: dict) -> sqlite3.Row:
    scene_id = new_id()
    now = time.time()
    conn.execute(
        """INSERT INTO scenes (id, user_id, name, doc, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (scene_id, user_id, name, json.dumps(doc), now, now),
    )
    return conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
