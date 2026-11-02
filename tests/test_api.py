"""API tests — the seven acceptance criteria from the scope, as executable checks.

The worker is not started here; each test drives `Worker.process_one()` directly so
"a job finished" is a fact rather than a race.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from forge3d_studio import config, db, storage
from forge3d_studio.api import app
from forge3d_studio.worker import Worker


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("STUDIO_DEV_MODE", "true")
    monkeypatch.setenv("STUDIO_STARTING_CREDITS", "100")
    monkeypatch.setenv("STUDIO_WORKERS", "0")
    config._settings = None
    config.get_settings(refresh=True)
    db.init_db()

    app.state.start_worker = False          # tests drive the queue themselves
    app.state.worker = None
    with TestClient(app) as client:
        yield client
    config._settings = None


def sign_in(client) -> dict:
    start = client.post("/auth/request", json={"email": "maker@example.com"})
    assert start.status_code == 200, start.text
    token = start.json()["dev_token"]
    assert token, "dev mode must return the token"
    done = client.get(f"/auth/callback?token={token}")
    assert done.status_code == 200, done.text
    return client.get("/me").json()


def run_queue() -> int:
    """Drain the queue synchronously. Returns how many jobs ran."""
    worker = Worker()
    count = 0
    while worker.process_one():
        count += 1
        if count > 10:
            raise AssertionError("queue did not drain")
    return count


# --- 1. it is alive ------------------------------------------------------
def test_healthz(studio):
    body = studio.get("/healthz").json()
    assert body["ok"] is True
    assert body["service"] == "forge3d-studio"


def test_kinds_are_advertised_with_their_cost(studio):
    kinds = {k["kind"]: k for k in studio.get("/kinds").json()}
    assert kinds["shape"]["cost"] == 0
    assert kinds["provider"]["cost"] > 0, "a paid kind must declare its cost"


# --- 2. auth -------------------------------------------------------------
def test_a_magic_link_signs_you_in(studio):
    user = sign_in(studio)
    assert user["email"] == "maker@example.com"
    assert user["credits"] == 100


def test_endpoints_require_a_session(studio):
    assert studio.get("/me").status_code == 401
    assert studio.get("/jobs").status_code == 401
    assert studio.post("/jobs", json={"kind": "shape"}).status_code == 401


def test_a_login_token_is_single_use(studio):
    token = studio.post("/auth/request",
                        json={"email": "once@example.com"}).json()["dev_token"]
    assert studio.get(f"/auth/callback?token={token}").status_code == 200
    assert studio.get(f"/auth/callback?token={token}").status_code == 400


def test_logout_ends_the_session(studio):
    sign_in(studio)
    assert studio.get("/me").status_code == 200
    studio.post("/auth/logout")
    assert studio.get("/me").status_code == 401


# --- 3. the whole pipeline: submit -> worker -> asset --------------------
def test_a_shape_job_produces_a_downloadable_asset(studio):
    sign_in(studio)
    submitted = studio.post("/jobs", json={
        "kind": "shape", "params": {"shape": "torus_knot", "resolution": 48},
    })
    assert submitted.status_code == 201, submitted.text
    job = submitted.json()
    assert job["status"] == "queued"

    assert run_queue() == 1

    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "succeeded", finished
    assert finished["progress"] == 1.0
    assert finished["asset_id"]

    asset = studio.get(f"/assets/{finished['asset_id']}").json()
    assert asset["triangles"] > 0
    assert asset["has_glb"] is True
    assert asset["validation"], "the engine's validation report must be stored"

    glb = studio.get(f"/assets/{asset['id']}/file/glb")
    assert glb.status_code == 200
    assert glb.content[:4] == b"glTF", "not a binary glTF file"


def test_a_planet_job_runs_end_to_end(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={
        "kind": "planet", "params": {"radius": 30, "seed": 3, "subdivisions": 3,
                                     "texture_resolution": 0},
    }).json()
    run_queue()
    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "succeeded", finished
    assert studio.get(f"/assets/{finished['asset_id']}").json()["triangles"] > 0


def test_a_failing_job_is_recorded_not_raised(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={
        "kind": "shape", "params": {"shape": "no-such-shape"},
    }).json()
    run_queue()
    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "failed"
    assert finished["error"]


# --- 4. the cost guard fires before the money is spent -------------------
def test_a_paid_job_is_refused_before_it_is_submitted(studio):
    user = sign_in(studio)
    with db.connect() as conn:
        conn.execute("UPDATE users SET credits = 1 WHERE id = ?", (user["id"],))

    refused = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    assert refused.status_code == 402, refused.text
    assert "not enough credits" in refused.json()["detail"]

    # Nothing was queued, and nothing was charged.
    assert studio.get("/jobs").json() == []
    assert studio.get("/me").json()["credits"] == 1


def test_credits_are_reserved_when_a_paid_job_is_accepted(studio):
    user = sign_in(studio)
    accepted = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    assert accepted.status_code == 201
    assert studio.get("/me").json()["credits"] == user["credits"] - accepted.json()["cost"]


def test_a_failed_paid_job_is_refunded(studio):
    user = sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider",
                                     "params": {"prompt": "x"}}).json()
    charged = studio.get("/me").json()["credits"]
    assert charged == user["credits"] - job["cost"]

    run_queue()   # no provider key is configured, so this must fail

    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "failed"
    assert studio.get("/me").json()["credits"] == user["credits"], "not refunded"


# --- 5. idempotent submission -------------------------------------------
def test_the_same_idempotency_key_does_not_create_two_jobs(studio):
    sign_in(studio)
    payload = {"kind": "shape", "params": {"shape": "twisted_prism"},
               "idempotency_key": "abc-123"}
    first = studio.post("/jobs", json=payload).json()
    second = studio.post("/jobs", json=payload).json()
    assert first["id"] == second["id"]
    assert len(studio.get("/jobs").json()) == 1


# --- 6. cancellation ----------------------------------------------------
def test_a_queued_job_can_be_cancelled(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape",
                                     "params": {"shape": "spring"}}).json()
    cancelled = studio.post(f"/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    assert run_queue() == 0, "a cancelled job must not be picked up"


def test_cancelling_a_finished_job_is_refused(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape",
                                     "params": {"shape": "spring", "resolution": 32}}).json()
    run_queue()
    assert studio.post(f"/jobs/{job['id']}/cancel").status_code == 409


# --- 7. scenes and export ----------------------------------------------
def _make_asset(studio, shape: str) -> str:
    job = studio.post("/jobs", json={
        "kind": "shape", "params": {"shape": shape, "resolution": 40, "name": shape},
    }).json()
    run_queue()
    return studio.get(f"/jobs/{job['id']}").json()["asset_id"]


def test_a_scene_exports_to_one_html_page(studio):
    sign_in(studio)
    a = _make_asset(studio, "torus_knot")
    b = _make_asset(studio, "twisted_prism")

    scene = studio.post("/scenes", json={
        "name": "Two Bodies",
        "items": [{"asset_id": a, "position": [0, 0, 0]},
                  {"asset_id": b, "position": [12, 2, 0], "scale": 0.8}],
    })
    assert scene.status_code == 201, scene.text
    scene_id = scene.json()["id"]

    exported = studio.post(f"/scenes/{scene_id}/export")
    assert exported.status_code == 200, exported.text
    body = exported.json()
    assert body["ok"] is True, body
    assert body["size_bytes"] > 10_000

    page = studio.get(f"/assets/{body['asset_id']}/file/viewer")
    assert page.status_code == 200
    html = page.text
    assert html.count("data:model/gltf-binary") == 2, "both models must be embedded"
    assert "Two Bodies" in html


def test_a_scene_cannot_reference_someone_elses_asset(studio):
    sign_in(studio)
    mine = _make_asset(studio, "spring")

    other = TestClient(app)
    start = other.post("/auth/request", json={"email": "other@example.com"}).json()
    other.get(f"/auth/callback?token={start['dev_token']}")

    attempt = other.post("/scenes", json={
        "name": "theirs", "items": [{"asset_id": mine}],
    })
    assert attempt.status_code == 400
    assert "unknown asset ids" in attempt.json()["detail"]


# --- isolation ----------------------------------------------------------
def test_assets_are_not_visible_across_users(studio):
    sign_in(studio)
    mine = _make_asset(studio, "bumpy_sphere")

    other = TestClient(app)
    start = other.post("/auth/request", json={"email": "nosy@example.com"}).json()
    other.get(f"/auth/callback?token={start['dev_token']}")

    assert other.get(f"/assets/{mine}").status_code == 404
    assert other.get(f"/assets/{mine}/file/glb").status_code == 404


def test_stored_paths_cannot_escape_the_root(studio):
    with pytest.raises(ValueError):
        storage.resolve("../../etc/passwd")


def test_the_job_event_stream_reports_terminal_state(studio):
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape",
                                     "params": {"shape": "spring", "resolution": 32}}).json()
    run_queue()
    with studio.stream("GET", f"/jobs/{job['id']}/events") as response:
        payloads = [json.loads(line[len("data: "):])
                    for line in response.iter_lines() if line.startswith("data: ")]
    assert payloads, "the stream produced nothing"
    assert payloads[-1]["status"] == "succeeded"


# --- regressions found by using the product -----------------------------
def test_cancelling_a_queued_paid_job_refunds_it(studio):
    """The provider was never contacted, so the credits must come back."""
    user = sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider",
                                     "params": {"prompt": "x"}}).json()
    assert studio.get("/me").json()["credits"] == user["credits"] - job["cost"]

    assert studio.post(f"/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert studio.get("/me").json()["credits"] == user["credits"], "credits were not refunded"


def test_cancelling_twice_refunds_once(studio):
    user = sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider",
                                     "params": {"prompt": "x"}}).json()
    studio.post(f"/jobs/{job['id']}/cancel")
    assert studio.post(f"/jobs/{job['id']}/cancel").status_code == 409
    assert studio.get("/me").json()["credits"] == user["credits"], "double refund"


def test_the_api_works_before_the_lifespan_runs(tmp_path, monkeypatch):
    """A client used without `with`, or a script, must not hit 'no such table'."""
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "fresh"))
    monkeypatch.setenv("STUDIO_DEV_MODE", "true")
    config._settings = None
    config.get_settings(refresh=True)
    try:
        client = TestClient(app)          # deliberately not a context manager
        assert client.get("/healthz").status_code == 200
        assert client.post("/auth/request",
                           json={"email": "a@b.com"}).status_code == 200
    finally:
        config._settings = None


# --- refund policy on paid jobs (found in review) ------------------------
def _paid_job(studio) -> dict:
    job = studio.post("/jobs", json={"kind": "provider",
                                     "params": {"prompt": "a teapot"}}).json()
    assert job["cost"] > 0
    return job


def test_an_uncertain_submission_is_not_refunded(studio, monkeypatch):
    """The provider may have accepted and charged for it.

    Refunding here hands out provider work for free, which is why `uncertain` is
    its own status rather than a flavour of `failed`.
    """
    from forge3d_studio import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_require_provider", lambda: "meshy")
    monkeypatch.setattr(worker_mod, "_engine", lambda: type("E", (), {
        "create_model": staticmethod(
            lambda **kw: {"ok": False, "uncertain": True, "idempotency_key": "k-1"})})())

    user = sign_in(studio)
    job = _paid_job(studio)
    charged = studio.get("/me").json()["credits"]
    assert charged == user["credits"] - job["cost"]

    run_queue()

    done = studio.get(f"/jobs/{job['id']}").json()
    assert done["status"] == "uncertain"
    assert done["refunded"] is False
    assert done["needs_review"] is True
    assert studio.get("/me").json()["credits"] == charged, "an uncertain job was refunded"
    assert "provider" in done["error"].lower()


def test_a_rejected_submission_is_refunded(studio, monkeypatch):
    """A clean rejection means nothing was produced, so the credits go back."""
    from forge3d_studio import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_require_provider", lambda: "meshy")
    monkeypatch.setattr(worker_mod, "_engine", lambda: type("E", (), {
        "create_model": staticmethod(
            lambda **kw: {"ok": False, "error": "prompt rejected (400)"})})())

    user = sign_in(studio)
    job = _paid_job(studio)
    run_queue()

    done = studio.get(f"/jobs/{job['id']}").json()
    assert done["status"] == "failed"
    assert done["refunded"] is True
    assert studio.get("/me").json()["credits"] == user["credits"]


def test_an_unexpected_failure_keeps_the_credits_for_review(studio, monkeypatch):
    """An exception tells us nothing about whether the remote job exists."""
    from forge3d_studio import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_require_provider", lambda: "meshy")

    def explode(**kw):
        raise OSError("connection reset while uploading")

    monkeypatch.setattr(worker_mod, "_engine", lambda: type("E", (), {
        "create_model": staticmethod(explode)})())

    sign_in(studio)
    job = _paid_job(studio)
    charged = studio.get("/me").json()["credits"]
    run_queue()

    done = studio.get(f"/jobs/{job['id']}").json()
    assert done["status"] == "failed"
    assert done["refunded"] is False
    assert done["needs_review"] is True
    assert studio.get("/me").json()["credits"] == charged


def test_a_free_job_never_needs_review(studio, monkeypatch):
    from forge3d_studio import worker as worker_mod

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(worker_mod, "_engine", lambda: type("E", (), {
        "create_shape": staticmethod(explode)})())

    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape",
                                     "params": {"shape": "spring"}}).json()
    run_queue()
    done = studio.get(f"/jobs/{job['id']}").json()
    assert done["status"] == "failed"
    assert done["refunded"] is False
    assert done["needs_review"] is False, "a free job has no money to reconcile"


def test_the_reconciliation_queue_is_filterable(studio, monkeypatch):
    """An operator needs to find the jobs whose credits were kept."""
    from forge3d_studio import worker as worker_mod

    monkeypatch.setattr(worker_mod, "_require_provider", lambda: "meshy")
    monkeypatch.setattr(worker_mod, "_engine", lambda: type("E", (), {
        "create_model": staticmethod(
            lambda **kw: {"ok": False, "uncertain": True, "idempotency_key": "k-2"})})())

    sign_in(studio)
    job = _paid_job(studio)
    run_queue()

    flagged = studio.get("/jobs?needs_review=true").json()
    assert [j["id"] for j in flagged] == [job["id"]]
    assert studio.get("/jobs?needs_review=false").json()[0]["id"] == job["id"]
    assert studio.get("/jobs?status=uncertain").json()[0]["id"] == job["id"]


def test_an_existing_database_gains_the_new_columns(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does not add columns to a live database."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
            params TEXT NOT NULL, status TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'local', cost INTEGER NOT NULL DEFAULT 0,
            progress REAL NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT '',
            error TEXT, asset_id TEXT, idempotency_key TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, started_at REAL, finished_at REAL
        );
    """)
    conn.commit()
    conn.close()

    db.init_db(db_path)

    with db.connect(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert {"refunded", "needs_review"} <= columns
