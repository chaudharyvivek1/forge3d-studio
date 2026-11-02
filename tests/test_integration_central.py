"""Studio and the central platform, together.

These are the Phase 6 and Phase 7 scenarios: what happens when the platform is
unhappy, when the process dies mid-job, and when two applications share a
platform. The platform here is the real service app running in-process, so the
status codes and state transitions are the actual contract, not a stub of it.
"""

from __future__ import annotations

import json

import pytest
from central_fake import APP_ID, BASE_URL, SERVICE_TOKEN, FakeCentral
from fastapi.testclient import TestClient

from forge3d_studio import config, db
from forge3d_studio.api import app
from forge3d_studio.central import CentralClient
from forge3d_studio.reconcile import reconcile_pending
from forge3d_studio.worker import Worker

EMAIL = "maker@example.com"


@pytest.fixture
def central(tmp_path):
    with FakeCentral(tmp_path) as fake:
        yield fake


@pytest.fixture
def studio(tmp_path, monkeypatch, central):
    monkeypatch.setenv("STUDIO_DATA_DIR", str(tmp_path / "studio"))
    monkeypatch.setenv("STUDIO_DEV_MODE", "true")
    monkeypatch.setenv("STUDIO_WORKERS", "0")
    monkeypatch.setenv("AUTH_BILLING_API_URL", BASE_URL)
    monkeypatch.setenv("AUTH_BILLING_APP_ID", APP_ID)
    monkeypatch.setenv("AUTH_BILLING_SERVICE_TOKEN", SERVICE_TOKEN)
    config._settings = None
    config.get_settings(refresh=True)
    db.init_db()

    client = CentralClient(BASE_URL, APP_ID, SERVICE_TOKEN, transport=central.dispatch)
    app.state.central = client
    app.state.start_worker = False
    app.state.worker = None
    with TestClient(app) as test_client:
        test_client.central = central
        test_client.central_client = client
        yield test_client
    app.state.central = None
    config._settings = None


def sign_in(client) -> dict:
    token = client.post("/auth/request", json={"email": EMAIL}).json()["dev_token"]
    assert client.get(f"/auth/callback?token={token}").status_code == 200
    return client.get("/me").json()


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


# ------------------------------------------------------- application access
def test_a_job_without_an_entitlement_is_refused(studio):
    sign_in(studio)
    studio.central.revoke(account_id(studio))

    refused = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    assert refused.status_code == 403
    assert "not entitled" in refused.json()["detail"]

    assert studio.get("/jobs").json() == []
    assert studio.central.reservations(account_id(studio)) == []


def test_entitlement_is_per_application(studio):
    """Being allowed in another application says nothing about studio."""
    sign_in(studio)
    account = account_id(studio)
    studio.central.revoke(account)
    studio.central.grant(account, "*", app_id="other-app")

    refused = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    assert refused.status_code == 403


def test_studio_cannot_read_another_applications_reservation(studio):
    """A reservation belongs to the application that made it."""
    from central_fake import OTHER_TOKEN

    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()
    reservation_id = job["reservation_id"]
    assert reservation_id

    as_studio = studio.central.client.get(
        f"/v1/service/reservations/{reservation_id}",
        headers={"x-service-token": SERVICE_TOKEN},
    )
    assert as_studio.status_code == 200

    as_other = studio.central.client.get(
        f"/v1/service/reservations/{reservation_id}",
        headers={"x-service-token": OTHER_TOKEN},
    )
    assert as_other.status_code == 404, "another application must not see this reservation"


# ---------------------------------------------------------------- the happy path
def test_a_successful_paid_job_settles_the_reservation(studio):
    sign_in(studio)
    account = account_id(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()

    assert studio.central.reservation_states(account) == ["reserved"]
    assert studio.central.balance(account) == 75

    run_queue(studio)   # no provider key, so this fails safely

    assert studio.central.reservation_states(account) == ["refunded"]
    assert studio.central.balance(account) == 100
    assert studio.get(f"/jobs/{job['id']}").json()["refunded"] is True


def test_an_uncertain_submission_keeps_the_reservation(studio, monkeypatch):
    from forge3d_studio import worker as worker_module

    def uncertain(job, params, progress):
        progress(0.5, "submitting")
        raise worker_module.UncertainSubmission("job-1", "provider may have accepted")

    monkeypatch.setitem(worker_module.HANDLERS, "provider", uncertain)
    sign_in(studio)
    account = account_id(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()

    run_queue(studio)

    assert studio.central.reservation_states(account) == ["needs_review"]
    assert studio.central.balance(account) == 75, "an uncertain job must not be refunded"

    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "uncertain"
    assert finished["needs_review"] is True
    assert finished["refunded"] is False


def test_an_uncertain_job_ends_its_event_stream(studio, monkeypatch):
    from forge3d_studio import worker as worker_module

    def uncertain(job, params, progress):
        raise worker_module.UncertainSubmission(None, "provider may have accepted")

    monkeypatch.setitem(worker_module.HANDLERS, "provider", uncertain)
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()
    run_queue(studio)

    with studio.stream("GET", f"/jobs/{job['id']}/events") as response:
        payloads = [
            json.loads(line[len("data: "):])
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]
    assert payloads
    assert payloads[-1]["status"] == "uncertain"
    assert payloads[-1]["needs_review"] is True


# --------------------------------------------------------------- outages
def test_billing_outage_refuses_the_job(studio):
    sign_in(studio)
    studio.central.fail_paths("/reservations")

    refused = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})
    assert refused.status_code == 503
    assert studio.get("/jobs").json() == [], "no job may be queued without a reservation"


def test_auth_outage_refuses_everything(studio):
    sign_in(studio)
    studio.central.go_down()

    assert studio.get("/me").status_code == 503
    assert studio.post("/jobs", json={"kind": "shape", "params": {}}).status_code == 503


def test_no_service_credential_is_ever_returned(studio):
    sign_in(studio)
    bodies = [
        studio.get("/healthz").text,
        studio.get("/me").text,
        studio.get("/jobs").text,
        studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).text,
    ]
    studio.central.fail_paths("/reservations")
    bodies.append(studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).text)

    for body in bodies:
        assert SERVICE_TOKEN not in body


# ------------------------------------------------------- failed job insert
def test_a_failed_job_insert_releases_the_reservation(studio, monkeypatch):
    sign_in(studio)
    account = account_id(studio)

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "create_job", boom)

    with pytest.raises(RuntimeError):
        studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})

    assert studio.central.reservation_states(account) == ["refunded"]
    assert studio.central.balance(account) == 100


# ------------------------------------------------------------ reconciliation
def _queued_paid_job(client) -> dict:
    sign_in(client)
    return client.post(
        "/jobs", json={"kind": "provider", "params": {"prompt": "x"}}
    ).json()


def _mark_running(job_id: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = 1 WHERE id = ?", (job_id,)
        )


def test_a_job_running_at_shutdown_is_flagged_not_retried(studio):
    job = _queued_paid_job(studio)
    account = account_id(studio)
    _mark_running(job["id"])

    counts = reconcile_pending(studio.central_client, config.get_settings())

    assert counts["flagged"] == 1
    assert studio.central.reservation_states(account) == ["needs_review"]
    assert studio.central.balance(account) == 75, "an uncertain job keeps its reservation"

    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "uncertain"
    assert finished["needs_review"] is True
    assert "not retried" in finished["error"]


def test_a_queued_job_is_left_for_the_worker(studio):
    job = _queued_paid_job(studio)
    account = account_id(studio)

    counts = reconcile_pending(studio.central_client, config.get_settings())

    assert counts["queued"] == 1
    assert studio.central.reservation_states(account) == ["reserved"]
    assert studio.get(f"/jobs/{job['id']}").json()["status"] == "queued"


def test_reconciliation_records_a_reservation_released_elsewhere(studio):
    job = _queued_paid_job(studio)
    account = account_id(studio)
    _mark_running(job["id"])
    studio.central.client.post(
        f"/v1/service/reservations/{job['reservation_id']}/release",
        json={"reason": "operator"},
        headers={"x-service-token": SERVICE_TOKEN},
    )

    counts = reconcile_pending(studio.central_client, config.get_settings())

    assert counts["released"] == 1
    finished = studio.get(f"/jobs/{job['id']}").json()
    assert finished["status"] == "failed"
    assert finished["refunded"] is True
    assert studio.central.balance(account) == 100


def test_reconciliation_is_skipped_safely_when_the_platform_is_down(studio):
    job = _queued_paid_job(studio)
    _mark_running(job["id"])
    studio.central.go_down()

    counts = reconcile_pending(studio.central_client, config.get_settings())

    assert counts["unreachable"] == 1
    assert studio.get(f"/jobs/{job['id']}").json()["status"] == "running", (
        "a platform outage must not change local state"
    )


def test_restart_reconciliation_runs_at_startup(studio):
    """The lifespan reconciles before the worker starts."""
    assert "checked" in (app.state.reconciled or {}), app.state.reconciled
