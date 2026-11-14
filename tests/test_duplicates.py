"""Shortening the same URL twice must give a working short URL each time."""
import json
from urllib.parse import quote, urlencode

from conftest import SHLINK_ENDPOINT, shlink_short_url

PASTE = "https://bin.example/?abc123#key"


def bodies(mocked):
    return [json.loads(c.request.body) for c in mocked.calls if c.request.body]


def test_duplicate_url_reuses_existing_short_url(client, mocked):
    # Regression: every click on PrivateBin's "shorten" created a new short URL;
    # the deployed variant's deterministic-slug workaround fabricated links.
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("same1", PASTE))
    a = client.get("/?link=" + quote(PASTE, safe="")).get_data(as_text=True)
    b = client.get("/?link=" + quote(PASTE, safe="")).get_data(as_text=True)
    assert a == b == "https://s.example/same1"
    assert all(body["findIfExists"] is True for body in bodies(mocked))
    assert all("customSlug" not in body for body in bodies(mocked))


def test_exhausted_existing_short_url_is_replaced(make_client, mocked):
    client = make_client(SHLINK_MAX_VISITS="100")
    dead = {**shlink_short_url("dead1", PASTE), "meta": {"maxVisits": 100, "validSince": None, "validUntil": None},
            "visitsSummary": {"total": 100, "nonBots": 90, "bots": 10}}
    mocked.post(SHLINK_ENDPOINT, json=dead)
    mocked.get(SHLINK_ENDPOINT, json={"shortUrls": {"data": [dead]}})
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("fresh", PASTE))
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.get_data(as_text=True) == "https://s.example/fresh"
    first, second = bodies(mocked)
    assert first["findIfExists"] is True and "findIfExists" not in second
    assert second["maxVisits"] == 100


def test_expired_existing_short_url_is_replaced(client, mocked):
    expired = {**shlink_short_url("old", PASTE),
               "meta": {"maxVisits": None, "validSince": None, "validUntil": "2020-01-01T00:00:00+00:00"}}
    mocked.post(SHLINK_ENDPOINT, json=expired)
    mocked.get(SHLINK_ENDPOINT, json={"shortUrls": {"data": []}})
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("fresh", PASTE))
    assert client.get("/?link=" + quote(PASTE, safe="")).get_data(as_text=True) == "https://s.example/fresh"


def test_custom_keyword_does_not_use_find_if_exists(client, mocked):
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url("kw", PASTE))
    client.get("/yourls-api.php?" + urlencode({"action": "shorturl", "url": PASTE, "keyword": "kw"}))
    assert "findIfExists" not in bodies(mocked)[0]


def test_find_if_exists_can_be_disabled(make_client, mocked):
    client = make_client(SHLINK_FIND_IF_EXISTS="false")
    mocked.post(SHLINK_ENDPOINT, json=shlink_short_url())
    client.get("/?link=" + quote(PASTE, safe=""))
    assert "findIfExists" not in bodies(mocked)[0]


def _rec(code, long_url=PASTE, visits=0, max_visits=100, tags=("privatebin",), domain=None):
    return {**shlink_short_url(code, long_url), "tags": list(tags), "domain": domain,
            "meta": {"maxVisits": max_visits, "validSince": None, "validUntil": None},
            "visitsSummary": {"total": visits, "nonBots": visits, "bots": 0}}


def test_newer_live_duplicate_is_found_after_oldest_is_exhausted(make_client, mocked):
    # Regression: Shlink's findIfExists returns the oldest match. Once it was
    # used up, every request created yet another short URL (verified on 5.1.8).
    client = make_client(SHLINK_MAX_VISITS="100")
    mocked.post(SHLINK_ENDPOINT, json=_rec("old", visits=100))
    mocked.get(SHLINK_ENDPOINT, json={"shortUrls": {"data": [
        _rec("other", long_url=PASTE + "x"),          # substring match only
        _rec("tagged", tags=("else",)),               # different options
        _rec("limit", max_visits=5),
        _rec("dom", domain="s2.example"),
        _rec("live", visits=3),
        _rec("old", visits=100),
    ]}})
    resp = client.get("/?link=" + quote(PASTE, safe=""))
    assert resp.get_data(as_text=True) == "https://s.example/live"
    assert len(mocked.calls) == 2
    search = mocked.calls[1].request.url
    assert "orderBy=dateCreated-DESC" in search and "searchTerm=" in search
