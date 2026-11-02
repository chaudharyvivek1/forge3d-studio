"""The client's failure policy, with no server involved.

Every one of these is a "what happens when the platform misbehaves" case, which
is exactly where a client quietly gets it wrong: treating an outage as success,
or retrying something that may already have been paid for.
"""

from __future__ import annotations

import pytest

from forge3d_studio.central import (
    CentralClient,
    CentralRejected,
    CentralUnavailable,
    InsufficientCredits,
    InvalidSession,
    NotEntitled,
    SessionCache,
)


def client(transport) -> CentralClient:
    return CentralClient("http://central.test", "forge3d-studio", "tok", transport=transport)


def respond(status, payload=None):
    def transport(method, url, body, headers):
        return status, payload or {}

    return transport


def test_service_headers_carry_the_token_and_app_id():
    seen = {}

    def transport(method, url, body, headers):
        seen.update(headers)
        return 200, {"active": True, "account_id": "a", "email": "e"}

    client(transport).verify("t")
    assert seen["x-service-token"] == "tok"
    assert seen["x-app-id"] == "forge3d-studio"


def test_login_request_is_sent_without_service_credentials():
    seen = {}

    def transport(method, url, body, headers):
        seen.update(headers)
        return 200, {"sent": True}

    client(transport).request_login("a@b.com")
    assert "x-service-token" not in seen


def test_401_is_an_invalid_session():
    with pytest.raises(InvalidSession):
        client(respond(401, {"detail": "nope"})).verify("t")


def test_402_is_insufficient_credits():
    with pytest.raises(InsufficientCredits):
        client(respond(402, {"detail": "not enough"})).create_reservation(
            account_id="a", kind="provider", cost=25, idempotency_key="k"
        )


def test_403_is_not_entitled():
    with pytest.raises(NotEntitled):
        client(respond(403, {"detail": "not entitled"})).create_reservation(
            account_id="a", kind="provider", cost=25, idempotency_key="k"
        )


def test_5xx_is_unavailable_not_rejected():
    with pytest.raises(CentralUnavailable):
        client(respond(503, {"detail": "down"})).balance("a")


def test_connection_failure_is_unavailable():
    def transport(method, url, body, headers):
        raise ConnectionError("refused")

    with pytest.raises(CentralUnavailable):
        client(transport).balance("a")


def test_4xx_that_is_not_special_is_a_plain_rejection():
    with pytest.raises(CentralRejected) as caught:
        client(respond(404, {"detail": "no such reservation"})).get_reservation("x")
    assert caught.value.status_code == 404


def test_verify_caches_but_revoke_forgets():
    calls = {"n": 0}

    def transport(method, url, body, headers):
        calls["n"] += 1
        return 200, {"active": True, "account_id": "a", "email": "e"}

    c = client(transport)
    c.verify("token")
    c.verify("token")
    assert calls["n"] == 1, "the second verify should come from the cache"

    c.revoke("token")
    c.verify("token")
    assert calls["n"] == 3, "revoke must forget the cached session"


def test_an_inactive_session_is_cached_as_none():
    calls = {"n": 0}

    def transport(method, url, body, headers):
        calls["n"] += 1
        return 200, {"active": False}

    c = client(transport)
    assert c.verify("t") is None
    assert c.verify("t") is None
    assert calls["n"] == 1


def test_session_cache_expires(clock):
    cache = SessionCache(ttl=10, clock=clock)
    cache.put("t", {"account_id": "a"})
    assert cache.get("t") == (True, {"account_id": "a"})
    clock.advance(11)
    assert cache.get("t") == (False, None)


def test_entitlement_wildcard():
    def transport(method, url, body, headers):
        return 200, {"features": ["*"]}

    assert client(transport).is_entitled("a", "provider") is True


def test_entitlement_must_match():
    def transport(method, url, body, headers):
        return 200, {"features": ["shape"]}

    c = client(transport)
    assert c.is_entitled("a", "shape") is True
    assert c.is_entitled("a", "provider") is False
