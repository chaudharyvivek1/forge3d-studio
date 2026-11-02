"""Orphan recovery across every page.

The v0.2.1 sweep read one page of 200 and treated it as the whole set, so the
reservations most in need of recovery — everything past row 200 — were invisible.
"""

from __future__ import annotations

import pytest
from central_fake import APP_ID, BASE_URL, SERVICE_TOKEN, FakeCentral
from fastapi.testclient import TestClient

from forge3d_studio import config, db
from forge3d_studio.api import app
from forge3d_studio.central import CentralClient
from forge3d_studio.reconcile import recover_orphan_reservations

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


def sign_in(client) -> str:
    token = client.post("/auth/request", json={"email": EMAIL}).json()["dev_token"]
    assert client.get(f"/auth/callback?token={token}").status_code == 200
    return client.central.account_for_email(EMAIL)


def seed_reservations(client, count: int, *, cost: int = 0) -> None:
    """Create reservations straight in the platform's database.

    Going through HTTP a thousand times would test the transport, not the sweep.
    The read path under test is still the real paginated endpoint.
    """
    from billing_auth.billing import reserve_operation
    from billing_auth.db import connect, transaction

    account = client.central.account_for_email(EMAIL)
    if cost:
        # Enough balance that the seeding itself never runs dry: the point of
        # these tests is recovery, not the credit check.
        client.central.set_balance(account, count * cost + 100)
    conn = connect(client.central.db_path)
    try:
        for index in range(count):
            with transaction(conn):
                reserve_operation(
                    conn,
                    account_id=account,
                    cost=cost,
                    kind="shape",
                    app_id=APP_ID,
                    idempotency_key=f"orphan-{index}",
                )
    finally:
        conn.close()


@pytest.mark.parametrize("count", [1, 199, 200, 201])
def test_every_orphan_is_released(studio, count):
    sign_in(studio)
    seed_reservations(studio, count)

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["candidates"] == count
    assert counts["released"] == count
    assert counts["failed"] == 0

    remaining = studio.central_client.reservations_for_app(state="reserved")
    assert remaining == []


@pytest.mark.slow
def test_more_than_a_thousand_orphans_are_all_handled(studio):
    account = sign_in(studio)
    seed_reservations(studio, 1001, cost=1)   # charged, so releasing is visible
    assert studio.central.balance(account) == 100, "1001 credits are held"

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["candidates"] == 1001
    assert counts["released"] == 1001
    assert studio.central.balance(account) == 1101, "every credit came back"


@pytest.mark.slow
def test_a_job_linked_reservation_is_never_released(studio):
    account = sign_in(studio)
    job = studio.post("/jobs", json={"kind": "provider", "params": {"prompt": "x"}}).json()
    seed_reservations(studio, 250)          # plenty of orphans around it

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 250
    assert counts["skipped"] == 1
    linked = studio.central_client.get_reservation(job["reservation_id"])
    assert linked.state == "reserved", "a job's reservation must survive the sweep"
    assert studio.central.balance(account) == 75


def test_a_reservation_inside_the_grace_period_is_left_alone(studio, monkeypatch):
    sign_in(studio)
    seed_reservations(studio, 300)
    monkeypatch.setenv("STUDIO_ORPHAN_GRACE_SECONDS", "3600")
    config._settings = None
    settings = config.get_settings(refresh=True)

    counts = recover_orphan_reservations(studio.central_client, settings)

    assert counts["candidates"] == 0
    assert len(studio.central_client.reservations_for_app(state="reserved")) == 300


def test_a_failure_while_reading_pages_releases_nothing(studio):
    account = sign_in(studio)
    seed_reservations(studio, 300, cost=1)
    studio.central.fail_paths("/reservations/page")

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 0
    assert studio.central.balance(account) == 100, "nothing was touched"


@pytest.mark.slow
def test_a_per_item_release_failure_is_counted_and_does_not_lie(studio):
    """A rejected release is counted and the sweep carries on."""
    account = sign_in(studio)
    seed_reservations(studio, 250, cost=1)
    studio.central.fail_paths("/release", status=404)

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 0
    assert counts["failed"] == 250
    assert studio.central.balance(account) == 100, "no failed release refunded anything"


def test_the_sweep_stops_when_the_platform_goes_away_midway(studio):
    """A 5xx is not a per-item failure: stop, and let a later run finish."""
    account = sign_in(studio)
    seed_reservations(studio, 30, cost=1)
    studio.central.fail_paths("/release", status=503)

    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 0
    assert counts["failed"] == 1, "it stopped after the first outage, not after 30"
    assert studio.central.balance(account) == 100
    assert len(studio.central_client.reservations_for_app(state="reserved")) == 30


@pytest.mark.slow
def test_repeated_runs_are_idempotent(studio):
    account = sign_in(studio)
    seed_reservations(studio, 450, cost=1)

    first = recover_orphan_reservations(studio.central_client, config.get_settings())
    second = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert first["released"] == 450
    assert second["candidates"] == 0, "the second run has nothing left to find"
    assert studio.central.balance(account) == 550, "450 released, not 900"


@pytest.mark.slow
def test_no_reservation_is_processed_twice_across_pages(studio):
    """Each released id must appear exactly once in the sweep's decisions."""
    sign_in(studio)
    seed_reservations(studio, 401)
    seen: list[str] = []

    real_release = studio.central_client.release

    def spy(reservation_id, reason="release"):
        seen.append(reservation_id)
        return real_release(reservation_id, reason=reason)

    studio.central_client.release = spy  # type: ignore[method-assign]
    counts = recover_orphan_reservations(studio.central_client, config.get_settings())

    assert counts["released"] == 401
    assert len(seen) == 401
    assert len(set(seen)) == 401, "a reservation was released twice"


def test_a_repeated_cursor_is_refused_rather_than_looping(studio):
    from forge3d_studio.central import CentralError

    client = studio.central_client
    original = client._call

    def stuck(method, path, body=None, service=True):
        if "/reservations/page" in path:
            return {"items": [], "next_cursor": "same-cursor"}
        return original(method, path, body, service=service)

    client._call = stuck  # type: ignore[method-assign]

    with pytest.raises(CentralError, match="already returned"):
        client.reservations_for_app(state="reserved")
