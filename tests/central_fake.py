"""The central platform, running in-process for tests.

This is not a hand-written stub of the contract. It is the real service app,
driven through FastAPI's TestClient, with the studio client pointed at it by
swapping the transport. So these tests exercise the actual wire contract —
status codes, entitlement rules, idempotency, the lot — with no socket, no
network and no real money.

If the contract changes, these tests break. That is the point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

billing_auth = pytest.importorskip(
    "billing_auth",
    reason="studio's integration tests need the private central platform package",
)

from billing_auth import Settings as CoreSettings  # noqa: E402
from billing_auth import connect, initialise, register_app, tenancy  # noqa: E402
from billing_auth.service import ServiceSettings, create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

APP_ID = "forge3d-studio"
SERVICE_TOKEN = "test-service-token"
OTHER_TOKEN = "other-app-token"
BASE_URL = "http://central.test"
CALLBACK = "http://studio.test/auth/callback"


class FakeCentral:
    def __init__(
        self,
        tmp_path: Path,
        *,
        starting_credits: int = 100,
        auto_entitle: str | None = "*",
    ) -> None:
        self.db_path = Path(tmp_path) / "central.db"
        self.core = CoreSettings(
            db_path=self.db_path,
            dev_mode=True,
            starting_credits=starting_credits,
            app_base_url="http://central.test",
            session_cookie="central_session",
            login_token_ttl_seconds=900,
            session_ttl_seconds=3600,
            smtp_host="smtp.example",
        )
        self.settings = ServiceSettings(
            core=self.core,
            service_tokens={APP_ID: SERVICE_TOKEN, "other-app": OTHER_TOKEN},
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.auto_entitle = auto_entitle
        self.calls: list[tuple[str, str, int]] = []

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> FakeCentral:
        self.client.__enter__()
        conn = connect(self.db_path)
        initialise(conn)
        register_app(conn, APP_ID, "Forge3D Studio", callback_url=CALLBACK)
        register_app(conn, "other-app", "Other App", callback_url="http://other.test/cb")
        conn.close()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    # -- the transport the studio client uses -----------------------------
    def transport(self, method: str, url: str, body: dict | None, headers: dict):
        assert url.startswith(BASE_URL), f"unexpected host: {url}"
        path = url[len(BASE_URL):]
        response = self.client.request(method, path, json=body, headers=headers)
        self.calls.append((method, path, response.status_code))

        payload = {}
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {}

        # A signed-in account is entitled to everything in studio unless a test
        # says otherwise; entitlement itself is tested explicitly elsewhere.
        if (
            self.auto_entitle
            and path == "/v1/service/sessions/exchange"
            and response.status_code == 200
        ):
            self.grant(payload["account_id"], self.auto_entitle)
        return response.status_code, payload

    # -- test helpers -----------------------------------------------------
    def grant(self, account_id: str, feature: str = "*", app_id: str = APP_ID) -> None:
        conn = connect(self.db_path)
        try:
            tenancy.grant_entitlement(
                conn, account_id=account_id, app_id=app_id, feature=feature
            )
        finally:
            conn.close()

    def revoke(self, account_id: str, feature: str = "*", app_id: str = APP_ID) -> None:
        conn = connect(self.db_path)
        try:
            tenancy.revoke_entitlement(
                conn, account_id=account_id, app_id=app_id, feature=feature
            )
        finally:
            conn.close()

    def account_for_email(self, email: str) -> str:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM auth_accounts WHERE email = ?", (email.lower(),)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, f"no central account for {email}"
        return row["id"]

    def set_balance(self, account_id: str, credits: int) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                "UPDATE auth_accounts SET credits = ? WHERE id = ?",
                (credits, account_id),
            )
        finally:
            conn.close()

    def balance(self, account_id: str) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute(
                "SELECT credits FROM auth_accounts WHERE id = ?", (account_id,)
            ).fetchone()["credits"]
        finally:
            conn.close()

    def reservations(self, account_id: str) -> list[dict]:
        response = self.client.get(
            f"/v1/service/reservations?account_id={account_id}",
            headers={"x-service-token": SERVICE_TOKEN},
        )
        return response.json()

    def reservation_states(self, account_id: str) -> list[str]:
        return [row["state"] for row in self.reservations(account_id)]

    def dispatch(self, method: str, url: str, body: dict | None, headers: dict):
        """A stable callable that always uses the *current* transport.

        The studio client captures its transport at construction, so tests that
        simulate an outage reassign `self.transport` and rely on this indirection
        to take effect.
        """
        return self.transport(method, url, body, headers)

    def fail_paths(self, *needles: str, status: int = 503) -> None:
        """Break only the endpoints whose path contains one of `needles`."""
        original = self.transport

        def wrapped(method, url, body, headers):
            if any(needle in url for needle in needles):
                return status, {"detail": "upstream error"}
            return original(method, url, body, headers)

        self.transport = wrapped  # type: ignore[assignment]

    def go_down(self) -> None:
        """Make every call fail the way an outage does."""

        def down(method, url, body, headers):
            raise ConnectionError("central platform is down")

        self.transport = down  # type: ignore[assignment]

    def slow(self, status: int = 503) -> None:
        """Answer everything with a server error."""

        def failing(method, url, body, headers):
            return status, {"detail": "upstream error"}

        self.transport = failing  # type: ignore[assignment]
