"""Client for the central authentication and billing platform.

Studio does not own identity or money any more. It owns generation jobs. This
module is the only place that talks to the platform, so the failure policy
lives in one file and can be read in one sitting.

The policy, in order of importance:

1. **Fail closed.** If the platform cannot confirm that a paid job is
   authorised, no job is created and no provider is called. A 503 to the user is
   always better than a generation nobody paid for.
2. **Never invent a state.** A reservation is only ever committed after a
   confirmed success, and only ever released when we know nothing was sent.
3. **Uncertain means uncertain.** If the platform cannot be reached to commit or
   release, the job is flagged for reconciliation. It is never silently retried,
   because a retried paid submission is a second charge.

Service credentials are read from the environment and never leave this process.
Nothing here is ever serialised into a response.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The platform's own maximum page size. Mirrored here so a caller gets a clear
#: local error instead of a 422 from the far side of a network boundary.
MAX_PAGE_SIZE = 1000

#: (method, url, body, headers) -> (status, decoded json)
Transport = Callable[[str, str, "dict | None", "dict[str, str]"], "tuple[int, dict]"]


class CentralError(Exception):
    """Base class for every platform failure."""


class CentralUnavailable(CentralError):
    """The platform could not be reached or answered 5xx. Fail closed."""


class CentralRejected(CentralError):
    """The platform answered, and said no."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"central platform refused the request ({status_code}): {detail}")


class NotEntitled(CentralRejected):
    """This account may not use this application, or this feature in it."""


class InsufficientCredits(CentralRejected):
    """The platform says the balance will not cover the reservation."""


class InvalidSession(CentralRejected):
    """The access token is unknown, expired or revoked."""


@dataclass(frozen=True)
class SessionView:
    access_token: str
    account_id: str
    email: str
    expires_at: float


@dataclass(frozen=True)
class ReservationView:
    id: str
    account_id: str
    app_id: str
    kind: str
    cost: int
    state: str
    created: bool
    balance_after: int


def urllib_transport(timeout: float) -> Transport:
    """A transport with no third-party dependency."""

    def send(method: str, url: str, body: dict | None, headers: dict[str, str]):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            request.add_header(key, value)
        if data is not None:
            request.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read() or b"{}"
                return response.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b"{}"
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"detail": raw.decode("utf-8", "replace")[:200]}
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CentralUnavailable(f"central platform unreachable: {type(exc).__name__}") from None

    return send


class SessionCache:
    """A very short-lived cache of session verifications.

    Every request would otherwise cost a round trip to the platform. The window
    is deliberately short: revocation is only as prompt as this TTL, which is
    the honest trade and is documented rather than hidden.
    """

    def __init__(self, ttl: float = 15.0, clock: Callable[[], float] = time.time) -> None:
        self.ttl = ttl
        self._clock = clock
        self._entries: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def get(self, token: str) -> tuple[bool, dict | None]:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return False, None
            expires_at, value = entry
            if expires_at <= self._clock():
                self._entries.pop(token, None)
                return False, None
            return True, value

    def put(self, token: str, value: dict | None) -> None:
        with self._lock:
            self._entries[token] = (self._clock() + self.ttl, value)

    def forget(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)


class CentralClient:
    def __init__(
        self,
        base_url: str,
        app_id: str,
        service_token: str,
        *,
        timeout: float = 5.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self._service_token = service_token
        self.timeout = timeout
        self._transport = transport or urllib_transport(timeout)
        self.sessions = SessionCache()

    # -- plumbing ---------------------------------------------------------
    def _service_headers(self) -> dict[str, str]:
        return {
            "x-service-token": self._service_token,
            "x-app-id": self.app_id,
            "accept": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        service: bool = True,
    ) -> dict:
        url = f"{self.base_url}{path}"
        headers = self._service_headers() if service else {"accept": "application/json"}
        try:
            status, payload = self._transport(method, url, body, headers)
        except CentralError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken transport still fails closed
            raise CentralUnavailable(
                f"central platform unreachable: {type(exc).__name__}"
            ) from None

        if 200 <= status < 300:
            return payload or {}

        detail = str((payload or {}).get("detail") or "request refused")
        if status == 401:
            raise InvalidSession(status, detail)
        if status == 402:
            raise InsufficientCredits(status, detail)
        if status == 403:
            raise NotEntitled(status, detail)
        if status >= 500:
            raise CentralUnavailable(detail)
        raise CentralRejected(status, detail)

    # -- authentication ---------------------------------------------------
    def request_login(self, email: str) -> dict:
        return self._call(
            "POST",
            "/v1/auth/login/request",
            {"email": email, "app_id": self.app_id},
            service=False,
        )

    def exchange(self, login_token: str) -> SessionView:
        payload = self._call(
            "POST", "/v1/service/sessions/exchange", {"login_token": login_token}
        )
        return SessionView(
            access_token=payload["access_token"],
            account_id=payload["account_id"],
            email=payload["email"],
            expires_at=float(payload.get("expires_at") or 0.0),
        )

    def verify(self, access_token: str) -> dict | None:
        """The account behind a token, or None. Cached for a few seconds."""
        hit, value = self.sessions.get(access_token)
        if hit:
            return value
        payload = self._call(
            "POST", "/v1/service/sessions/verify", {"access_token": access_token}
        )
        account = payload if payload.get("active") else None
        self.sessions.put(access_token, account)
        return account

    def revoke(self, access_token: str) -> None:
        self.sessions.forget(access_token)
        self._call("POST", "/v1/service/sessions/revoke", {"access_token": access_token})

    # -- entitlements and balance -----------------------------------------
    def balance(self, account_id: str) -> int:
        payload = self._call("GET", f"/v1/service/accounts/{account_id}/balance")
        return int(payload.get("credits") or 0)

    def entitlements(self, account_id: str) -> list[str]:
        payload = self._call(
            "GET", f"/v1/service/accounts/{account_id}/entitlements"
        )
        return list(payload.get("features") or [])

    def is_entitled(self, account_id: str, feature: str) -> bool:
        features = self.entitlements(account_id)
        return "*" in features or feature in features

    def prices(self) -> dict[str, int]:
        """The platform's prices for this application: kind -> cost."""
        payload = self._call("GET", f"/v1/service/apps/{self.app_id}/prices")
        return {row["kind"]: int(row["cost"]) for row in payload}

    # -- reservations -----------------------------------------------------
    @staticmethod
    def _reservation(payload: dict) -> ReservationView:
        return ReservationView(
            id=payload["id"],
            account_id=payload["account_id"],
            app_id=payload.get("app_id") or "",
            kind=payload.get("kind") or "",
            cost=int(payload.get("cost") or 0),
            state=payload.get("state") or "",
            created=bool(payload.get("created")),
            balance_after=int(payload.get("balance_after") or 0),
        )

    def create_reservation(
        self,
        *,
        account_id: str,
        kind: str,
        cost: int,
        idempotency_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> ReservationView:
        payload = self._call(
            "POST",
            "/v1/service/reservations",
            {
                "account_id": account_id,
                "kind": kind,
                "cost": cost,
                "idempotency_key": idempotency_key,
                "metadata": metadata or {},
            },
        )
        return self._reservation(payload)

    def get_reservation(self, reservation_id: str) -> ReservationView:
        return self._reservation(
            self._call("GET", f"/v1/service/reservations/{reservation_id}")
        )

    def pending_reservations(self, account_id: str, state: str = "needs_review") -> list[ReservationView]:
        payload = self._call(
            "GET",
            f"/v1/service/reservations?account_id={account_id}&state={state}",
        )
        return [self._reservation(row) for row in payload]

    def reservations_for_app(
        self,
        *,
        state: str | None = None,
        older_than: float | None = None,
        page_size: int = 200,
        max_pages: int = 500,
    ) -> list[ReservationView]:
        """Every reservation this application made, across every page.

        The old version read one page and called it the whole set, which meant
        everything past the first 200 rows was invisible to the orphan sweep —
        the reservations most in need of recovery were exactly the ones it could
        not see.

        Two failure modes are refused rather than papered over: a cursor that
        repeats (which would loop forever) and a page count beyond `max_pages`
        (which would mean the sweep is not converging). Both raise, so a caller
        never mistakes a partial read for a complete one.
        """
        # The platform caps a page at 1000. Asking for more is a caller bug, and
        # silently clamping it would hide the fact that the caller expected
        # something the API cannot do.
        if not isinstance(page_size, int) or isinstance(page_size, bool):
            raise ValueError("page_size must be an integer")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if not isinstance(max_pages, int) or isinstance(max_pages, bool):
            raise ValueError("max_pages must be an integer")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")

        collected: list[ReservationView] = []
        cursor: str | None = None
        seen: set[str] = set()

        for _ in range(max_pages):
            query = [f"limit={page_size}"]
            if state:
                query.append(f"state={state}")
            if older_than is not None:
                query.append(f"older_than={older_than}")
            if cursor:
                query.append(f"cursor={urllib.parse.quote(cursor, safe='')}")

            payload = self._call(
                "GET", f"/v1/service/reservations/page?{'&'.join(query)}"
            )
            collected.extend(self._reservation(row) for row in payload.get("items") or [])

            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                return collected
            if next_cursor in seen:
                raise CentralError(
                    "the platform returned a pagination cursor it had already "
                    "returned; refusing to loop"
                )
            seen.add(next_cursor)
            cursor = next_cursor

        raise CentralError(
            f"reservation listing did not finish within {max_pages} pages"
        )

    def _action(self, reservation_id: str, action: str, body: dict | None = None) -> tuple[str, bool]:
        payload = self._call(
            "POST", f"/v1/service/reservations/{reservation_id}/{action}", body or {}
        )
        return str(payload.get("state") or ""), bool(payload.get("changed"))

    def commit(self, reservation_id: str) -> tuple[str, bool]:
        return self._action(reservation_id, "commit")

    def release(self, reservation_id: str, reason: str = "release") -> tuple[str, bool]:
        return self._action(reservation_id, "release", {"reason": reason})

    def flag(self, reservation_id: str, reason: str = "uncertain") -> tuple[str, bool]:
        return self._action(reservation_id, "flag", {"reason": reason})


def client_from_settings(settings) -> CentralClient:
    client = CentralClient(
        settings.auth_billing_api_url,
        settings.auth_billing_app_id,
        settings.auth_billing_service_token,
        timeout=settings.auth_billing_timeout_seconds,
    )
    # The cache window is configuration, not a constant: an operator who wants
    # revocation to bite sooner should be able to say so.
    client.sessions = SessionCache(ttl=settings.session_cache_ttl_seconds)
    return client


__all__ = [
    "CentralClient",
    "CentralError",
    "CentralRejected",
    "CentralUnavailable",
    "InsufficientCredits",
    "InvalidSession",
    "NotEntitled",
    "MAX_PAGE_SIZE",
    "ReservationView",
    "SessionCache",
    "SessionView",
    "client_from_settings",
    "urllib_transport",
]
