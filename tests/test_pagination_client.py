"""Input validation and loop safety in the paginated reservation reader.

These are the ways a paging loop goes wrong quietly: an impossible page size, a
budget that silently truncates, a cursor that repeats, a page that is empty but
promises more.
"""

from __future__ import annotations

import pytest

from forge3d_studio.central import MAX_PAGE_SIZE, CentralClient, CentralError, CentralUnavailable


def client(transport) -> CentralClient:
    return CentralClient("http://central.test", "forge3d-studio", "tok", transport=transport)


def page(items, next_cursor=None):
    return 200, {"items": items, "next_cursor": next_cursor}


def row(rid: str) -> dict:
    return {
        "id": rid,
        "account_id": "acc",
        "app_id": "forge3d-studio",
        "kind": "shape",
        "cost": 0,
        "state": "reserved",
        "created": False,
        "balance_after": 0,
        "idempotency_key": None,
    }


# ------------------------------------------------------------ validation
@pytest.mark.parametrize("page_size", [0, -1, MAX_PAGE_SIZE + 1, 10_000])
def test_an_impossible_page_size_is_refused(page_size):
    c = client(lambda *a: page([]))
    with pytest.raises(ValueError, match="page_size"):
        c.reservations_for_app(page_size=page_size)


@pytest.mark.parametrize("page_size", [1.5, "200", None, True])
def test_a_non_integer_page_size_is_refused(page_size):
    c = client(lambda *a: page([]))
    with pytest.raises(ValueError, match="page_size"):
        c.reservations_for_app(page_size=page_size)


@pytest.mark.parametrize("max_pages", [0, -5])
def test_a_non_positive_page_budget_is_refused(max_pages):
    c = client(lambda *a: page([]))
    with pytest.raises(ValueError, match="max_pages"):
        c.reservations_for_app(max_pages=max_pages)


def test_the_maximum_page_size_is_accepted():
    calls = []

    def transport(method, url, body, headers):
        calls.append(url)
        return page([row("r1")])

    assert len(client(transport).reservations_for_app(page_size=MAX_PAGE_SIZE)) == 1
    assert f"limit={MAX_PAGE_SIZE}" in calls[0]


# ------------------------------------------------------------- loop safety
def test_a_repeated_cursor_is_rejected():
    c = client(lambda *a: page([row("r1")], next_cursor="same"))
    with pytest.raises(CentralError, match="already returned"):
        c.reservations_for_app()


def test_the_page_budget_raises_rather_than_truncating():
    """A partial read must never come back looking complete."""
    counter = {"n": 0}

    def transport(method, url, body, headers):
        counter["n"] += 1
        return page([row(f"r{counter['n']}")], next_cursor=f"c{counter['n']}")

    c = client(transport)
    with pytest.raises(CentralError, match="did not finish"):
        c.reservations_for_app(max_pages=3)
    assert counter["n"] == 3, "it stopped at the budget, not at the end of the data"


def test_an_empty_page_with_a_cursor_keeps_going():
    """An empty page is not the end of the list; the cursor is."""
    pages = iter([
        page([], next_cursor="c1"),
        page([row("r1")], next_cursor="c2"),
        page([row("r2")], next_cursor=None),
    ])

    def transport(method, url, body, headers):
        return next(pages)

    result = client(transport).reservations_for_app()
    assert [r.id for r in result] == ["r1", "r2"]


def test_the_cursor_is_url_encoded():
    seen: list[str] = []

    def transport(method, url, body, headers):
        seen.append(url)
        if len(seen) == 1:
            return page([row("r1")], next_cursor="a+b/c=d")
        return page([row("r2")])

    client(transport).reservations_for_app()
    assert "cursor=a%2Bb%2Fc%3Dd" in seen[1], seen[1]


def test_a_central_error_mid_pagination_raises():
    counter = {"n": 0}

    def transport(method, url, body, headers):
        counter["n"] += 1
        if counter["n"] == 1:
            return page([row("r1")], next_cursor="c1")
        return 503, {"detail": "down"}

    with pytest.raises(CentralUnavailable):
        client(transport).reservations_for_app()


def test_filters_are_carried_on_every_page():
    seen: list[str] = []

    def transport(method, url, body, headers):
        seen.append(url)
        if len(seen) == 1:
            return page([row("r1")], next_cursor="c1")
        return page([row("r2")])

    client(transport).reservations_for_app(state="reserved", older_than=123.0)
    for url in seen:
        assert "state=reserved" in url
        assert "older_than=123.0" in url


def test_one_page_is_enough_when_the_cursor_is_null():
    calls = {"n": 0}

    def transport(method, url, body, headers):
        calls["n"] += 1
        return page([row("r1")])

    assert len(client(transport).reservations_for_app()) == 1
    assert calls["n"] == 1
