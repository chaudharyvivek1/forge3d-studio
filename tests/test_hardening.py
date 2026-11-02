"""v0.2.1: pricing authority, orphan recovery and commit recovery.

Each of these is a way to lose or strand money that the v0.2.0 review found.
"""

from __future__ import annotations

import central_fake
import pytest
from central_fake import APP_ID, BASE_URL, SERVICE_TOKEN, FakeCentral
from fastapi.testclient import TestClient

from forge3d_studio import config, db
from forge3d_studio.api import app
from forge3d_studio.central import CentralClient, client_from_settings
from forge3d_studio.reconcile import reconcile_pending, recover_orphan_reservations
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
    monkeypatch.setenv("STUDIO_ORPHAN_GRACE_SECONDS", "0")
    config._settings = None
    config.get_settings(refresh=True)
    db.init_db()

    app.state.central = CentralClient(BASE_URL, APP_ID, SERVICE_TOKEN, transport=central.dispatch)
    app.state.start_worker = False
    app.state.worker = None
    with TestClient(app) as client:
        client.central = central
        client.central_client = app.state.central
        yield client
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


# ------------------------------------------------------- pricing authority
def test_the_platforms_price_wins_over_studios_figure(studio):
    """Studio's KINDS says 25; the platform says 40; the user pays 40."""
    sign_in(studio)
    account = account_id(studio)
    with central_fake.connect(studio.central.db_path) as conn:
        central_fake.tenancy.set_price(conn, app_id=APP_ID, kind="provider", cost=40)

    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()

    assert job["cost"] == 40
    assert studio.central.balance(account) == 60


def test_kinds_advertise_the_platform_price(studio):
    sign_in(studio)
    with central_fake.connect(studio.central.db_path) as conn:
        central_fake.tenancy.set_price(conn, app_id=APP_ID, kind="provider", cost=40)

    kinds = {row["kind"]: row for row in studio.get("/kinds").json()}
    assert kinds["provider"]["cost"] == 40
    assert kinds["provider"]["authoritative"] is True


def test_kinds_fall_back_when_the_platform_is_unreachable(studio):
    studio.central.fail_paths("/prices")
    kinds = {row["kind"]: row for row in studio.get("/kinds").json()}
    assert kinds["provider"]["cost"] == 25
    assert kinds["provider"]["authoritative"] is False


def test_an_unpriced_kind_never_reaches_a_provider(studio):
    sign_in(studio)
    account = account_id(studio)
    with central_fake.connect(studio.central.db_path) as conn:
        central_fake.tenancy.delete_price(conn, app_id=APP_ID, kind="provider")

    response = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}})

    assert response.status_code >= 400
    assert studio.get("/jobs").json() == []
    assert studio.central.balance(account) == 100


# ------------------------------------------------------ orphan reservations
def _orphan_reservation(client, key="orphan-1"):
    return client.central_client.create_reservation(
        account_id=account_id(client),
        kind="provider",
        cost=25,
        idempotency_key=key,
    )


def test_a_reservation_with_no_job_is_released(studio):
    sign_in(studio)
    account = account_id(studio)
    reservation = _orphan_reservation(studio)
    assert studio.central.balance(account) == 75

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 1
    assert studio.central.reservation_states(account) == ["refunded"]
    assert studio.central.balance(account) == 100
    assert reservation.id  # the id was the correlation key


def test_a_reservation_with_a_job_is_left_to_reconciliation(studio):
    sign_in(studio)
    account = account_id(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 0
    assert counts["skipped"] == 1
    assert studio.central.reservation_states(account) == ["reserved"]
    assert studio.get(f"/jobs/{job['id']}").json()["status"] == "queued"


def test_the_grace_period_protects_a_submit_still_in_flight(studio, monkeypatch):
    sign_in(studio)
    account = account_id(studio)
    _orphan_reservation(studio)

    monkeypatch.setenv("STUDIO_ORPHAN_GRACE_SECONDS", "3600")
    config._settings = None
    settings = config.get_settings(refresh=True)

    counts = recover_orphan_reservations(studio.central_client, settings)

    assert counts["candidates"] == 0
    assert studio.central.balance(account) == 75


def test_the_orphan_sweep_does_nothing_when_the_platform_is_down(studio):
    sign_in(studio)
    account = account_id(studio)
    _orphan_reservation(studio)
    studio.central.go_down()

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 0
    assert studio.central.balance(account) == 75


# ------------------------------------------------------- commit recovery
def test_a_delivered_job_whose_commit_failed_is_committed_on_restart(studio):
    """Commit fails after the asset is stored; the restart finishes the job."""
    sign_in(studio)
    job = studio.post("/jobs", json={"kind": "shape", "params": {}}).json()

    studio.central.fail_paths("/commit")
    run_queue(studio)
    studio.central.restore()

    delivered = studio.get(f"/jobs/{job['id']}").json()
    assert delivered["status"] == "succeeded"
    assert delivered["asset_id"], "the asset must survive a failed commit"
    assert delivered["needs_review"] is True

    counts = reconcile_pending(studio.central_client, config.get_settings())

    assert counts["committed"] == 1
    assert studio.get(f"/jobs/{job['id']}").json()["needs_review"] is False


def test_a_paid_delivered_job_is_never_released_by_recovery(studio, monkeypatch):
    """A succeeded job with an asset keeps its credits, whatever the commit did."""
    from forge3d_studio import worker as worker_module

    def fake_provider(job, params, progress):
        return {"card": {"triangles": 1}, "validation": {}, "glb": None}

    monkeypatch.setitem(worker_module.HANDLERS, "provider", fake_provider)
    sign_in(studio)
    account = account_id(studio)

    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()
    studio.central.fail_paths("/commit")
    run_queue(studio)
    studio.central.restore()

    after_failure = studio.get(f"/jobs/{job['id']}").json()
    assert after_failure["status"] == "succeeded"
    assert after_failure["needs_review"] is True
    assert studio.central.balance(account) == 75, "credits stay spent for delivered work"

    reconcile_pending(studio.central_client, config.get_settings())

    assert studio.central.reservation_states(account) == ["settled"]
    assert studio.central.balance(account) == 75
    assert studio.get(f"/jobs/{job['id']}").json()["needs_review"] is False


# ---------------------------------------------------------- error handling
def test_me_reports_an_outage_rather_than_a_zero_balance(studio):
    sign_in(studio)
    studio.central.fail_paths("/balance")

    response = studio.get("/me")
    assert response.status_code == 503
    assert "credits" not in response.text or response.status_code != 200


def test_me_still_reports_a_real_zero_balance(studio):
    sign_in(studio)
    account = account_id(studio)
    studio.central.set_balance(account, 0)

    body = studio.get("/me").json()
    assert body["credits"] == 0


def test_the_session_cache_ttl_comes_from_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_SESSION_CACHE_TTL", "42")
    config._settings = None
    settings = config.get_settings(refresh=True)
    assert client_from_settings(settings).sessions.ttl == 42


def test_integration_tests_are_actually_running():
    """Guard against the whole file silently skipping.

    If the private package were missing, every test above would be skipped and
    CI would go green while testing nothing.
    """
    assert central_fake.INTEGRATION_AVAILABLE is True
