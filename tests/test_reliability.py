"""The v0.1.3 reliability work: leases, heartbeats, limits and stream liveness.

Each test here maps to a failure that only shows up in production — a worker
that died, a proxy that dropped a quiet connection, a loop that submits a
thousand jobs.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import suppress

import pytest
from central_fake import APP_ID, BASE_URL, SERVICE_TOKEN, FakeCentral
from fastapi.testclient import TestClient

from forge3d_studio import config, db
from forge3d_studio.api import app
from forge3d_studio.central import CentralClient
from forge3d_studio.reconcile import reclaim_stale_jobs
from forge3d_studio.worker import Worker

EMAIL = "maker@example.com"


@pytest.fixture
def central(tmp_path):
    with FakeCentral(tmp_path) as fake:
        yield fake


def _studio(tmp_path, monkeypatch, central, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("STUDIO_DEV_MODE", "true")
    monkeypatch.setenv("STUDIO_WORKERS", "0")
    monkeypatch.setenv("AUTH_BILLING_API_URL", BASE_URL)
    monkeypatch.setenv("AUTH_BILLING_APP_ID", APP_ID)
    monkeypatch.setenv("AUTH_BILLING_SERVICE_TOKEN", SERVICE_TOKEN)
    config._settings = None
    config.get_settings(refresh=True)
    db.init_db()

    app.state.central = CentralClient(BASE_URL, APP_ID, SERVICE_TOKEN, transport=central.dispatch)
    app.state.start_worker = False
    app.state.worker = None
    return app


@pytest.fixture
def studio(tmp_path, monkeypatch, central):
    _studio(tmp_path, monkeypatch, central)
    with TestClient(app) as client:
        client.central = central
        client.central_client = app.state.central
        yield client
    app.state.central = None
    config._settings = None


@pytest.fixture
def studio_factory(tmp_path, monkeypatch, central):
    opened: list[TestClient] = []

    def make(**env) -> TestClient:
        _studio(tmp_path, monkeypatch, central, **env)
        client = TestClient(app)
        client.__enter__()
        client.central = central
        client.central_client = app.state.central
        opened.append(client)
        return client

    yield make
    for client in opened:
        with suppress(Exception):
            client.__exit__(None, None, None)
    app.state.central = None
    config._settings = None


def sign_in(client) -> None:
    token = client.post("/auth/request", json={"email": EMAIL}).json()["dev_token"]
    assert client.get(f"/auth/callback?token={token}").status_code == 200


def account_id(client) -> str:
    return client.central.account_for_email(EMAIL)


def run_queue(client) -> int:
    worker = Worker(central=client.central_client)
    count = 0
    while worker.process_one():
        count += 1
        if count > 10:
            raise AssertionError("queue did not drain")
    return count


# ------------------------------------------------------------- idempotency
def test_the_database_refuses_a_duplicate_idempotency_key(studio):
    """The local index is the backstop if the platform ever lets one through."""
    sign_in(studio)
    studio.post("/jobs", json={"kind": "shape", "params": {}, "idempotency_key": "k1"})

    with db.connect() as conn:
        user_id = conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO jobs (id, user_id, kind, params, status, provider, cost, "
                "progress, stage, idempotency_key, created_at) "
                "VALUES ('manual', ?, 'shape', '{}', 'queued', 'local', 0, 0, 'q', 'k1', 0)",
                (user_id,),
            )


def test_null_keys_do_not_collide(studio):
    sign_in(studio)
    for _ in range(3):
        assert studio.post("/jobs", json={"kind": "shape", "params": {}}).status_code == 201
    assert len(studio.get("/jobs").json()) == 3


# -------------------------------------------------------------- leases
def test_a_claim_records_the_worker_and_an_expiry(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()

    with db.connect() as conn:
        claimed = db.claim_next_job(conn, worker_id="worker-a", lease_seconds=60)

    assert claimed["id"] == job["id"]
    assert claimed["worker_id"] == "worker-a"
    assert claimed["heartbeat_at"] > 0
    assert claimed["lease_expires_at"] > time.time() + 30


def test_a_heartbeat_extends_the_lease(studio):
    sign_in(studio)
    studio.post("/jobs", json={"kind": "shape", "params": {}})
    with db.connect() as conn:
        db.claim_next_job(conn, worker_id="worker-a", lease_seconds=5)
        first = conn.execute("SELECT lease_expires_at FROM jobs").fetchone()[0]

    worker = Worker(central=studio.central_client)
    worker.worker_id = "worker-a"
    time.sleep(0.01)
    assert worker.heartbeat() == 1

    with db.connect() as conn:
        second = conn.execute("SELECT lease_expires_at FROM jobs").fetchone()[0]
    assert second > first


def test_a_live_lease_is_not_reclaimed(studio):
    sign_in(studio)
    studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    with db.connect() as conn:
        db.claim_next_job(conn, worker_id="worker-a", lease_seconds=600)

    counts = reclaim_stale_jobs(studio.central_client, config.get_settings())
    assert counts == {"stale": 0, "flagged": 0, "failed": 0}


def test_a_dead_workers_paid_job_becomes_uncertain_and_is_not_retried(studio):
    sign_in(studio)
    account = account_id(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()
    with db.connect() as conn:
        db.claim_next_job(conn, worker_id="worker-dead", lease_seconds=-1)

    counts = reclaim_stale_jobs(studio.central_client, config.get_settings())

    assert counts["stale"] == 1
    assert counts["flagged"] == 1
    reclaimed = studio.get(f"/jobs/{job['id']}").json()
    assert reclaimed["status"] == "uncertain"
    assert reclaimed["needs_review"] is True
    assert "not retried" in reclaimed["error"]
    assert studio.central.reservation_states(account) == ["needs_review"]
    assert studio.central.balance(account) == 75, "an uncertain job keeps its reservation"

    # And it must not be claimable again.
    with db.connect() as conn:
        assert db.claim_next_job(conn, worker_id="worker-b") is None


def test_a_dead_workers_free_job_is_marked_failed(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()
    with db.connect() as conn:
        db.claim_next_job(conn, worker_id="worker-dead", lease_seconds=-1)

    counts = reclaim_stale_jobs(studio.central_client, config.get_settings())

    assert counts["failed"] == 1
    reclaimed = studio.get(f"/jobs/{job['id']}").json()
    assert reclaimed["status"] == "failed"
    assert reclaimed["needs_review"] is False


# ------------------------------------------------------------------- SSE
def _local_user_id() -> str:
    with db.connect() as conn:
        return conn.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]


def _read_stream(client, job_id, *, headers=None, limit=12):
    """Read a bounded slice of the stream; the generator never ends on its own."""
    lines: list[str] = []
    with (
        suppress(Exception),
        client.stream("GET", f"/jobs/{job_id}/events", headers=headers or {}) as response,
    ):
        assert response.status_code == 200
        for line in response.iter_lines():
            lines.append(line)
            if len(lines) >= limit or ": heartbeat" in line:
                break
    return lines


def test_the_stream_sends_a_heartbeat_while_idle(studio_factory):
    """A job that is not moving still has to keep the connection alive.

    Read straight from the frame generator: holding a socket open on an endless
    stream is exactly what a test must not do.
    """
    import itertools
    from types import SimpleNamespace

    from forge3d_studio.api import event_frames

    studio = studio_factory()
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()

    settings = SimpleNamespace(sse_poll_seconds=0.01, sse_heartbeat_seconds=0.05)
    frames = event_frames(job["id"], _local_user_id(), settings)
    collected = list(itertools.islice(frames, 8))
    frames.close()

    # Each yielded chunk is a whole SSE frame, so match inside rather than at
    # the start.
    assert collected[0].startswith("retry: "), collected[:1]
    assert any("data: " in frame for frame in collected), collected
    assert any(frame.startswith(": heartbeat") for frame in collected), collected


def test_the_stream_emits_ids_and_resumes_from_last_event_id(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()
    run_queue(studio)

    lines = _read_stream(studio, job["id"])
    ids = [line for line in lines if line.startswith("id: ")]
    assert ids, lines

    resumed = _read_stream(studio, job["id"], headers={"Last-Event-ID": "7"})
    first_id = next(line for line in resumed if line.startswith("id: "))
    assert first_id == "id: 8", resumed


def test_the_stream_asks_the_browser_to_retry(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()
    run_queue(studio)
    lines = _read_stream(studio, job["id"])
    assert lines[0].startswith("retry: "), lines[:2]


def test_another_users_stream_looks_like_a_missing_job(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()
    run_queue(studio)

    other = studio.post("/auth/request", json={"email": "someone@example.com"}).json()
    studio.cookies.clear()
    studio.get(f"/auth/callback?token={other['dev_token']}")

    lines = _read_stream(studio, job["id"])
    assert any("no such job" in line for line in lines), lines


# ---------------------------------------------------------------- limits
def test_login_requests_are_rate_limited(studio_factory):
    studio = studio_factory(STUDIO_LOGIN_RATE_LIMIT=2)
    for _ in range(2):
        assert studio.post("/auth/request", json={"email": EMAIL}).status_code == 200
    blocked = studio.post("/auth/request", json={"email": EMAIL})
    assert blocked.status_code == 429
    assert "retry-after" in {k.lower() for k in blocked.headers}


def test_job_submissions_are_rate_limited(studio_factory):
    studio = studio_factory(STUDIO_JOB_RATE_LIMIT=2)
    sign_in(studio)
    for _ in range(2):
        assert studio.post("/jobs", json={"kind": "shape", "params": {}}).status_code == 201
    assert studio.post("/jobs", json={"kind": "shape", "params": {}}).status_code == 429


def test_an_oversized_body_is_refused_before_it_is_parsed(studio_factory):
    studio = studio_factory(STUDIO_MAX_PAYLOAD_BYTES=256)
    response = studio.post(
        "/auth/request",
        content=b'{"email": "' + b"a" * 1000 + b'@example.com"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_oversized_generation_parameters_are_refused(studio_factory):
    studio = studio_factory(STUDIO_MAX_PARAMS_BYTES=64)
    sign_in(studio)
    response = studio.post(
        "/jobs", json={"kind": "shape", "params": {"shape": "x" * 500}}
    )
    assert response.status_code == 413
    assert "parameters" in response.json()["detail"]
    assert studio.get("/jobs").json() == []
