"""Offline checks for the web API's character routes and error mapping.

Needs the dev dependency httpx (FastAPI's TestClient): `uv sync` installs it.
Run: uv run python test_app.py

The client functions app.py imports are swapped for fakes, so nothing touches
the network - these check routing, status codes and response shapes only.
"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

import app
from aion2_api import Aion2ApiError, CharacterLookupError

client = TestClient(app.app)

S6X = {"character_id": "a=", "server_id": "1101", "region": "nae"}
HITS = [
    {"character_id": "a=", "name": "s6x", "level": 45, "server_id": "1101", "server_name": "Siel", "region": "nae"},
    {"character_id": "c=", "name": "s6x", "level": 24, "server_id": "1505", "server_name": "Yustiel", "region": "as"},
]
BOARDS = [{"id": 81, "name": "Nezekan", "openPercent": 49}, {"id": 82, "name": "Zikel", "openPercent": 43}]


@contextmanager
def fakes(**replacements):
    """Swap names in app's namespace (where the routes look them up) for a test."""
    real = {name: getattr(app, name) for name in replacements}
    for name, fake in replacements.items():
        setattr(app, name, fake)
    try:
        yield
    finally:
        for name, value in real.items():
            setattr(app, name, value)


def resolves_to(ids):
    return lambda name, server=None: dict(ids)


def raises(exc):
    def fake(*args, **kwargs):
        raise exc

    return fake


def info_with_boards(endpoint, character_id, server_id, region=None, timeout=25.0, **extra):
    assert endpoint == "info"
    return {"daevanion": {"boardList": BOARDS}}, region


def test_search_returns_hits_and_validates_region():
    seen = []

    def fake_search(name, region=None):
        seen.append((name, region))
        return HITS

    with fakes(search_characters=fake_search):
        r = client.get("/characters/search", params={"name": "s6x", "region": "nae"})
        assert r.status_code == 200, r.text
        assert [h["server_name"] for h in r.json()] == ["Siel", "Yustiel"]
        assert seen == [("s6x", "nae")]

        assert client.get("/characters/search", params={"name": "s6x", "region": "xx"}).status_code == 422
        assert client.get("/characters/search").status_code == 422, "name is required"


def test_character_passes_server_through():
    seen = []

    def fake_resolve(name, server=None):
        seen.append((name, server))
        return dict(S6X)

    with fakes(resolve_character=fake_resolve, get_character=lambda cid, sid, region, item_details: {"profile": {"characterName": "s6x"}}):
        r = client.get("/characters/s6x", params={"server": "Siel"})
        assert r.status_code == 200, r.text
        assert r.json() == {"profile": {"characterName": "s6x"}}
        assert seen == [("s6x", "Siel")]


def test_character_item_details_is_opt_in():
    asked = []

    def fake_get_character(cid, sid, region, item_details):
        asked.append(item_details)
        return {"profile": {}}

    with fakes(resolve_character=lambda name, server=None: dict(S6X), get_character=fake_get_character):
        assert client.get("/characters/s6x").status_code == 200
        assert client.get("/characters/s6x", params={"item_details": "true"}).status_code == 200
        assert client.get("/characters/s6x", params={"item_details": "false"}).status_code == 200
        assert asked == [False, True, False], asked
        assert client.get("/characters/s6x", params={"item_details": "maybe"}).status_code == 422


ITEM_QUERY = {
    "characterId": "rKx=", "serverId": "1501", "id": "110130050",
    "slotPos": "1", "enchantLevel": "10", "exceedLevel": "0",
}
DETAIL = {"id": 110130050, "soulBindRate": "50.4", "subStats": [{"id": "Critical", "value": "56"}], "godStoneStat": []}
no_call = raises(AssertionError("must not be called"))


def test_equipment_item_forwards_one_item_without_lookups():
    seen = []

    def fake_item(character_id, server_id, item, region):
        seen.append((character_id, server_id, item, region))
        return dict(DETAIL)

    with fakes(
        get_equipment_item=fake_item,
        resolve_server_region=lambda s: "as" if s == "1501" else None,
        resolve_character=no_call,
        get_character=no_call,
        character_endpoint=no_call,
    ):
        r = client.get("/characters/equipment/item", params=ITEM_QUERY)
        assert r.status_code == 200, r.text
        assert r.json() == DETAIL, "passed through unchanged"
        assert seen == [("rKx=", "1501", {"id": 110130050, "slotPos": 1, "enchantLevel": 10, "exceedLevel": 0}, "as")]

        seen.clear()  # an explicit region is used as given
        assert client.get("/characters/equipment/item", params={**ITEM_QUERY, "region": "nae"}).status_code == 200
        assert seen[0][3] == "nae"


def test_equipment_item_rejects_missing_or_invalid_parameters():
    bad = [{k: v for k, v in ITEM_QUERY.items() if k != missing} for missing in ITEM_QUERY]
    bad += [
        {**ITEM_QUERY, **change}
        for change in (
            {"characterId": ""}, {"serverId": "Siel"}, {"serverId": "15"}, {"id": "0"}, {"id": "abc"},
            {"slotPos": "0"}, {"enchantLevel": "-1"}, {"enchantLevel": "1.5"}, {"exceedLevel": "x"},
            {"region": "global"},
        )
    ]
    with fakes(get_equipment_item=no_call, resolve_server_region=no_call):
        for params in bad:
            r = client.get("/characters/equipment/item", params=params)
            assert r.status_code == 422, (params, r.status_code, r.text)


def test_equipment_item_maps_upstream_failures():
    for upstream, expected in ((404, 404), (500, 502), (429, 503), (0, 502)):
        with fakes(get_equipment_item=raises(Aion2ApiError(upstream, "https://x", "boom")), resolve_server_region=lambda s: "as"):
            r = client.get("/characters/equipment/item", params=ITEM_QUERY)
            assert r.status_code == expected, (upstream, r.status_code, r.text)


def test_equipment_item_makes_exactly_one_upstream_request():
    """End to end through the real client: one equipment/item URL, nothing else."""
    import aion2_api

    urls = []

    def fake_get_json(url, headers, timeout, params=None):
        urls.append((url, params))
        return dict(DETAIL)

    real = aion2_api.get_json
    aion2_api.get_json = fake_get_json
    try:
        with fakes(resolve_server_region=lambda s: "as"):
            assert client.get("/characters/equipment/item", params=ITEM_QUERY).json() == DETAIL
        assert [u for u, _ in urls] == ["https://aion2.plaync.com/api/character/equipment/item"], urls
        assert urls[0][1] == {
            "region": "as", "lang": "en-US", "characterId": "rKx=", "serverId": "1501",
            "id": 110130050, "slotPos": 1, "enchantLevel": 10, "exceedLevel": 0,
        }, urls[0][1]
    finally:
        aion2_api.get_json = real


def test_ambiguous_name_is_409_with_candidates():
    exc = CharacterLookupError("2 characters named 's6x': ...; pick a server", HITS)
    with fakes(resolve_character=raises(exc)):
        r = client.get("/characters/s6x")
        assert r.status_code == 409, r.text
        body = r.json()
        assert "pick a server" in body["detail"]
        assert [c["server_id"] for c in body["candidates"]] == ["1101", "1505"]


def test_unknown_name_is_404():
    with fakes(resolve_character=raises(CharacterLookupError("0 characters named 'nobody'", []))):
        for path in ("/characters/nobody", "/characters/nobody/daevanion", "/characters/nobody/daevanion/81"):
            r = client.get(path)
            assert r.status_code == 404, (path, r.text)
            assert "candidates" not in r.json()


def test_daevanion_board_list_and_one_board():
    fetched = []

    def fake_board(cid, sid, board_id, region=None):
        fetched.append(board_id)
        return {"nodeList": [{"row": 8, "col": 8, "type": "Start"}]}

    with fakes(
        resolve_character=resolves_to(S6X),
        character_endpoint=info_with_boards,
        get_daevanion_board=fake_board,
    ):
        r = client.get("/characters/s6x/daevanion")
        assert r.status_code == 200 and [b["name"] for b in r.json()] == ["Nezekan", "Zikel"], r.text

        for board in ("Nezekan", "nezekan", "81"):  # by name, any case, or by id
            r = client.get(f"/characters/s6x/daevanion/{board}")
            assert r.status_code == 200, (board, r.text)
            assert r.json()["board"]["id"] == 81
            assert r.json()["nodeList"][0]["type"] == "Start"
        assert fetched == [81, 81, 81]

        r = client.get("/characters/s6x/daevanion/nope")
        assert r.status_code == 404 and "Nezekan, Zikel" in r.json()["detail"], r.text
        assert fetched == [81, 81, 81], "an unknown board must not be fetched"


def test_upstream_errors_map_to_http_statuses():
    cases = [(404, 404), (403, 503), (429, 503), (500, 502), (0, 502)]
    for upstream, expected in cases:
        with fakes(resolve_character=resolves_to(S6X), get_character=raises(Aion2ApiError(upstream, "https://x", "boom"))):
            r = client.get("/characters/s6x")
            assert r.status_code == expected, (upstream, r.status_code, r.text)


def test_servers_with_missing_population_is_200_not_502():
    """A page that omits the population columns degrades to nulls, not a 502."""

    class FakeServers:
        def servers(self, name=None, region=None):
            return [
                {
                    "name": "Israphel",
                    "status": "Online",
                    "faction": "Asmodian",
                    "region": "Europe",
                    "population_percent": None,
                    "current": None,
                    "max": None,
                    "queue": None,
                    "badges": [],
                    "note": None,
                }
            ]

    with fakes(Aion2Servers=FakeServers):
        r = client.get("/servers")
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert row["name"] == "Israphel" and row["region"] == "Europe"
    assert row["population_percent"] is None
    assert row["current"] is None and row["max"] is None and row["queue"] is None


def test_servers_roster_is_identity_only():
    ROSTER = [
        {"server_id": "1101", "name": "Siel", "short_name": "SIE", "faction": "Elyos", "region": "nae"},
        {"server_id": "1102", "name": "Israphel", "short_name": "ISR", "faction": "Asmodian", "region": "nae"},
    ]
    with fakes(server_list=lambda timeout=25.0: ROSTER):
        r = client.get("/servers/roster")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [x["server_id"] for x in rows] == ["1101", "1102"]
    assert rows[0]["faction"] == "Elyos" and rows[1]["faction"] == "Asmodian"
    # first-party means identity only: NCSOFT publishes no population or status.
    assert "population_percent" not in rows[0]
    assert "status" not in rows[0]


def test_servers_roster_upstream_failure_is_502():
    with fakes(server_list=raises(Aion2ApiError(0, "https://x", "server list down"))):
        r = client.get("/servers/roster")
    assert r.status_code == 502, r.text


def test_pinned_notices_respect_limit():
    class FakeBoard:
        def pinned(self):
            return [{"id": str(i)} for i in range(5)]

        def normalize(self, item):
            return {"id": item["id"], "title": f"Notice {item['id']}", "url": "https://x"}

    app.app.dependency_overrides[app.get_board] = lambda: FakeBoard()
    try:
        r = client.get("/notices", params={"pinned": "true", "limit": 2})
        assert r.status_code == 200, r.text
        assert [n["id"] for n in r.json()] == ["0", "1"]
        assert len(client.get("/notices", params={"pinned": "true"}).json()) == 5, "default limit keeps all 5"
    finally:
        app.app.dependency_overrides.clear()


def test_app_reports_the_shared_version():
    assert app.app.version == app.APP_VERSION
    assert client.get("/openapi.json").json()["info"]["version"] == app.APP_VERSION


def test_root_lists_character_routes():
    r = client.get("/")
    assert r.status_code == 200
    assert any("/characters/" in v for v in r.json()["how_to_use"].values())


# --- internal key (AION2_API_INTERNAL_KEY) -----------------------------------
# A made-up value for tests only; the real one lives in the deployment's env.
TEST_KEY = "test-only-not-a-real-key"
ROSTER_ROW = [{"server_id": "1101", "name": "Siel", "short_name": "SIE", "faction": "Elyos", "region": "nae"}]
PROTECTED = ["/servers/roster", "/notices", "/characters/s6x", "/openapi.json", "/docs"]


def test_internal_key_root_stays_open():
    with fakes(INTERNAL_KEY=TEST_KEY):
        r = client.get("/")
    assert r.status_code == 200, r.text
    assert "how_to_use" in r.json()


def test_internal_key_missing_header_is_401():
    with fakes(INTERNAL_KEY=TEST_KEY, server_list=raises(AssertionError("upstream must not be called"))):
        for path in PROTECTED:
            r = client.get(path)
            assert r.status_code == 401, (path, r.status_code)
            assert r.json() == {"detail": "Missing or invalid internal key"}
            assert TEST_KEY not in r.text


def test_internal_key_wrong_header_is_401():
    with fakes(INTERNAL_KEY=TEST_KEY, server_list=raises(AssertionError("upstream must not be called"))):
        # raw non-ASCII bytes too: the server decodes them as latin-1, and the
        # comparison must reject them rather than raise a 500
        for wrong in ["nope", TEST_KEY + "x", TEST_KEY[:-1], "", "ключ".encode("utf-8")]:
            r = client.get("/servers/roster", headers={"X-Internal-Key": wrong})
            assert r.status_code == 401, (wrong, r.status_code)
            assert TEST_KEY not in r.text


def test_internal_key_correct_header_reaches_the_route():
    with fakes(INTERNAL_KEY=TEST_KEY, server_list=lambda timeout=25.0: ROSTER_ROW):
        r = client.get("/servers/roster", headers={"X-Internal-Key": TEST_KEY})
        assert r.status_code == 200, r.text
        assert r.json()[0]["server_id"] == "1101"
        # the header name is case-insensitive, as HTTP headers are
        assert client.get("/openapi.json", headers={"x-internal-key": TEST_KEY}).status_code == 200
    # errors keep their usual statuses and shapes behind the key
    with fakes(INTERNAL_KEY=TEST_KEY, server_list=raises(Aion2ApiError(0, "https://x", "down"))):
        r = client.get("/servers/roster", headers={"X-Internal-Key": TEST_KEY})
        assert r.status_code == 502 and "detail" in r.json()


def test_internal_key_unset_keeps_api_open():
    # "" is what an unset or blank AION2_API_INTERNAL_KEY becomes
    with fakes(INTERNAL_KEY="", server_list=lambda timeout=25.0: ROSTER_ROW):
        assert client.get("/servers/roster").status_code == 200
        # a stray header is ignored rather than rejected
        assert client.get("/servers/roster", headers={"X-Internal-Key": "anything"}).status_code == 200
        assert client.get("/openapi.json").status_code == 200


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"\n{len(tests)} route checks passed (offline, no network)")


if __name__ == "__main__":
    main()
