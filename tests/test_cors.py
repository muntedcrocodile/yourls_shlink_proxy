"""PrivateBin (>= 1.3.1, incl. the deployed 2.1.0) calls the shortener with a
cross-origin fetch() from the paste page. Without CORS headers the fetch fails
and PrivateBin falls back to opening the raw proxy response in a new tab."""
from urllib.parse import quote

from conftest import SHLINK_ENDPOINT, shlink_short_url

PASTE = "https://bin.example/?abc#key"
ORIGIN = "https://bin.example"


def test_default_allows_any_origin(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    resp = client.get("/?link=" + quote(PASTE, safe=""), headers={"Origin": ORIGIN})
    assert resp.status_code == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Credentials" not in resp.headers


def test_error_responses_carry_cors_headers_too(client):
    # Otherwise the browser hides the status and PrivateBin can't report it.
    resp = client.get("/?link=", headers={"Origin": ORIGIN})
    assert resp.status_code == 400
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


def test_origin_allow_list(make_client, mocked):
    client = make_client(CORS_ALLOW_ORIGINS="https://bin.example, https://other.example/")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    ok = client.get("/?link=" + quote(PASTE, safe=""), headers={"Origin": ORIGIN})
    assert ok.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Origin" in ok.headers["Vary"]
    bad = client.get("/?link=" + quote(PASTE, safe=""), headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in bad.headers


def test_cors_can_be_disabled(make_client):
    client = make_client(CORS_ALLOW_ORIGINS="")
    resp = client.get("/?link=", headers={"Origin": ORIGIN})
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_preflight(client):
    resp = client.options("/", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert resp.status_code in (200, 204)
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
