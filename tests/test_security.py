"""Authentication (YOURLS signature) and URL allow-listing."""
import hashlib
import time
from urllib.parse import quote, urlencode

import pytest

from conftest import SHLINK_BASE, SHLINK_ENDPOINT, shlink_short_url

SECRET = "s3cret-signature-token"
PASTE = "https://bin.example/?abc123#Decrypt10nKey"


def api(client, **params):
    return client.get("/yourls-api.php?" + urlencode(params))


@pytest.fixture
def signed(make_client):
    return make_client(YOURLS_SIGNATURE=SECRET)


def test_open_by_default_but_warns(make_client, caplog):
    make_client()
    assert "anyone who can reach this proxy" in caplog.text


@pytest.mark.parametrize("params", [{}, {"signature": ""}, {"signature": "wrong-signature-xx"},
                                    {"signature": SECRET[:-1]}, {"signature": SECRET + "x"}])
def test_api_rejects_missing_or_bad_signature(signed, mocked, params):
    resp = api(signed, action="shorturl", format="json", url=PASTE, **params)
    assert resp.status_code == 403
    assert resp.get_json()["message"] == "Please log in"
    assert len(mocked.calls) == 0


def test_username_password_not_supported(signed, mocked):
    resp = api(signed, action="shorturl", format="json", url=PASTE, username="u", password="p")
    assert resp.status_code == 403
    assert resp.get_json()["message"] == "Invalid username or password"


def test_auth_applies_to_every_action(signed, mocked):
    for action in ("version", "expand", "url-stats", "stats", "db-stats", "bogus"):
        assert api(signed, action=action, format="json").status_code == 403


def test_plain_signature(signed, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    resp = api(signed, action="shorturl", format="json", url=PASTE, signature=SECRET)
    assert resp.status_code == 200


def test_signature_in_post_body(signed, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    resp = signed.post("/yourls-api.php", data={"action": "shorturl", "format": "json",
                                                "url": PASTE, "signature": SECRET})
    assert resp.status_code == 200


@pytest.mark.parametrize("algo, order", [("sha256", "ts+secret"), ("sha256", "secret+ts"),
                                         ("sha512", "ts+secret"), ("md5", "ts+secret")])
def test_timestamped_signature(signed, mocked, algo, order):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    ts = str(int(time.time()))
    msg = ts + SECRET if order == "ts+secret" else SECRET + ts
    sig = hashlib.new(algo, msg.encode()).hexdigest()
    params = {"timestamp": ts, "signature": sig}
    if algo != "md5":           # legacy YOURLS clients send md5 without `hash`
        params["hash"] = algo
    resp = api(signed, action="shorturl", format="json", url=PASTE, **params)
    assert resp.status_code == 200


def test_expired_timestamp_rejected(signed, mocked):
    ts = str(int(time.time()) - 43200 - 5)
    sig = hashlib.sha256((ts + SECRET).encode()).hexdigest()
    resp = api(signed, action="version", format="json", timestamp=ts, signature=sig)
    assert resp.status_code == 403


@pytest.mark.parametrize("extra", [{"hash": "crc32"}, {"hash": "sha256", "timestamp": "abc"}])
def test_bad_hash_or_timestamp_rejected(signed, extra):
    ts = extra.get("timestamp", str(int(time.time())))
    sig = hashlib.sha256((ts + SECRET).encode()).hexdigest()
    resp = api(signed, action="version", format="json", signature=sig, **{**extra, "timestamp": ts})
    assert resp.status_code == 403


def test_signature_hash_is_not_accepted_as_token(signed):
    # A timestamped hash must not work without its timestamp (and vice versa).
    sig = hashlib.sha256((str(int(time.time())) + SECRET).encode()).hexdigest()
    assert api(signed, action="version", format="json", signature=sig).status_code == 403


def test_secret_not_logged(signed, mocked, caplog):
    api(signed, action="version", format="json", signature="wrong-signature-xx")
    api(signed, action="version", format="json", signature=SECRET)
    assert SECRET not in caplog.text and "wrong-signature-xx" not in caplog.text


def test_legacy_link_open_unless_configured(signed, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    assert signed.get("/?link=" + quote(PASTE, safe="")).status_code == 200


def test_legacy_link_can_require_signature(make_client, mocked):
    client = make_client(YOURLS_SIGNATURE=SECRET, LINK_REQUIRES_SIGNATURE="1")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    assert client.get("/?link=" + quote(PASTE, safe="")).status_code == 403
    assert client.get(f"/?signature={SECRET}&link=" + quote(PASTE, safe="")).status_code == 200


@pytest.mark.parametrize("env", [{"YOURLS_SIGNATURE": "short"},
                                 {"LINK_REQUIRES_SIGNATURE": "1"},
                                 {"ALLOWED_URL_PREFIXES": "bin.example"}])
def test_auth_config_validation(make_client, env):
    from app import ConfigError
    with pytest.raises(ConfigError):
        make_client(**env)


# ---- stats / db-stats ---------------------------------------------------------

def test_stats_disabled_without_signature(client, mocked):
    # They would list every stored long URL, including PrivateBin keys.
    for action in ("stats", "db-stats"):
        resp = api(client, action=action, format="json")
        assert resp.status_code == 403
    assert len(mocked.calls) == 0


def _mock_totals(mocked):
    mocked.get(SHLINK_BASE + "/rest/v3/short-urls", json={"shortUrls": {
        "data": [shlink_short_url("a"), shlink_short_url("b")],
        "pagination": {"totalItems": 42}}})
    mocked.get(SHLINK_BASE + "/rest/v3/visits", json={"visits": {
        "nonOrphanVisits": {"total": 99}, "orphanVisits": {"total": 3}}})


def test_db_stats(signed, mocked):
    _mock_totals(mocked)
    data = api(signed, action="db-stats", format="json", signature=SECRET).get_json()
    assert data["db-stats"] == {"total_links": 42, "total_clicks": 99}


def test_stats(signed, mocked):
    _mock_totals(mocked)
    data = api(signed, action="stats", format="json", signature=SECRET, filter="last", limit="2").get_json()
    assert list(data["links"]) == ["link_1", "link_2"]
    assert data["stats"]["total_links"] == 42
    first = mocked.calls[0].request.url
    assert "orderBy=dateCreated-DESC" in first and "itemsPerPage=2" in first


# ---- URL prefix allow-list ----------------------------------------------------

@pytest.mark.parametrize("url, ok", [
    ("https://bin.example/?abc#key", True),
    ("HTTPS://BIN.EXAMPLE/?abc#key", True),
    ("https://bin.example/other", False),
    ("https://bin.example.evil.com/?abc", False),
    ("https://bin.example@evil.com/?abc", False),
    ("https://evil.com/?https://bin.example/?", False),
    ("http://bin.example/?abc", False),
])
def test_allowed_url_prefixes(make_client, mocked, url, ok):
    client = make_client(ALLOWED_URL_PREFIXES="https://bin.example/?")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    legacy = client.get("/?link=" + quote(url, safe=""))
    yourls = api(client, action="shorturl", format="json", url=url)
    assert (legacy.status_code == 200) is ok
    assert (yourls.status_code == 200) is ok
    if not ok:
        assert len(mocked.calls) == 0


@pytest.mark.parametrize("url, ok", [
    ("https://example.com/bin/?abc#key", True),
    ("https://example.com/bin/x/../?abc", False),
    ("https://example.com/bin/../admin/x", False),
    ("https://example.com/bin/%2e%2e/admin", False),
    ("https://example.com/bin/%2E%2E/admin", False),
    ("https://example.com/bin/./?a", False),
    ("https://example.com/bin/..\\admin", False),
    ("https://example.com/bin/x?q=../../y", True),     # only the path matters
])
def test_path_prefix_cannot_be_escaped(make_client, mocked, url, ok):
    # Regression: the prefix was compared as text, but browsers resolve dot
    # segments (and backslashes) when they follow Shlink's redirect.
    client = make_client(ALLOWED_URL_PREFIXES="https://example.com/bin/")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    assert (client.get("/?link=" + quote(url, safe="")).status_code == 200) is ok


@pytest.mark.parametrize("ts", ["1" + "0" * 400, "1_700_000_000", "+1700000000", "١٧٠٠٠٠٠٠٠٠", "-1"])
def test_odd_timestamps_are_rejected_not_500(signed, ts):
    # Regression: a 400-digit timestamp raised OverflowError -> HTML 500.
    sig = hashlib.sha256((ts + SECRET).encode()).hexdigest()
    resp = api(signed, action="version", format="json", timestamp=ts, signature=sig, hash="sha256")
    assert resp.status_code == 403
